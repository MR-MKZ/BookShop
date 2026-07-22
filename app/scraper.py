import argparse
import asyncio
import hashlib
import logging
import os
import random
import re
import socket
import sys

import aioftp
import aiohttp
import asyncpg
from bs4 import BeautifulSoup

from app.models import Book

# --- DEFAULTS ---
DEFAULT_BASE_URL = "https://asbook.ir"
DEFAULT_DB_NAME = "books_data.db"
DEFAULT_CONCURRENCY = 50

# --- CONFIGURATION FROM ENV OR DEFAULTS ---
DB_USER = os.getenv("POSTGRES_USER", "kabana_user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "kabana_pass")
DB_NAME = os.getenv("POSTGRES_DB", "kabana_db")
DB_HOST = os.getenv("DB_HOST", "db")

FTP_HOST = os.getenv("FTP_HOST", "ftp")
FTP_PORT = int(os.getenv("FTP_PORT", 21))
FTP_USER = os.getenv("FTP_USER", "ftp_user")
FTP_PASS = os.getenv("FTP_PASS", "ftp_pass")

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class BookScraper:
    def __init__(self, args):
        self.args = args
        self.base_url = args.url.rstrip("/")

        # Global limit on active HTTP requests
        self.semaphore = asyncio.Semaphore(args.concurrency)

        # Queues for the pipeline
        self.page_queue = asyncio.Queue()  # Stage 1: Page Numbers
        self.details_queue = asyncio.Queue()  # Stage 2: URLs to fetch
        self.db_queue = asyncio.Queue()  # Stage 3: Data to save

        self.stop_discovery = asyncio.Event()  # Signal when all pages are scanned

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        }

        self.known_urls: set[str] = set()
        self.known_content_keys: set[str] = set()
        self.db_pool = None
        self.run_id: int | None = None
        self.pages_total = 0
        self.pages_done = 0
        self.books_saved = 0
        self.books_skipped = 0

    async def init_db(self):
        """Initialize Postgres Connection Pool."""
        try:
            self.db_pool = await asyncpg.create_pool(
                user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST
            )
            # Ensure books table exists (simplified for scraper purposes, though models.py manages this)
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS books (
                        id SERIAL PRIMARY KEY,
                        url VARCHAR UNIQUE,
                        title VARCHAR,
                        title_en VARCHAR,
                        author VARCHAR,
                        publisher VARCHAR,
                        isbn VARCHAR,
                        publish_year VARCHAR,
                        language VARCHAR,
                        pages VARCHAR,
                        file_format VARCHAR,
                        file_size VARCHAR,
                        edition VARCHAR,
                        price NUMERIC,
                        original_price NUMERIC,
                        availability VARCHAR,
                        amazon_link VARCHAR,
                        image_url VARCHAR,
                        description TEXT,
                        folder_name VARCHAR UNIQUE,
                        cover_filename VARCHAR DEFAULT 'cover.jpg',
                        has_pdf BOOLEAN DEFAULT FALSE,
                        is_active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        updated_at TIMESTAMP WITH TIME ZONE
                    );
                """)
                await conn.execute("""
                    DO $$ BEGIN
                        CREATE TYPE scraperrunstatus AS ENUM ('RUNNING', 'COMPLETED', 'FAILED');
                    EXCEPTION
                        WHEN duplicate_object THEN null;
                    END $$;
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS scraper_runs (
                        id SERIAL PRIMARY KEY,
                        status scraperrunstatus NOT NULL DEFAULT 'RUNNING',
                        mode VARCHAR,
                        pages_total INTEGER DEFAULT 0,
                        pages_done INTEGER DEFAULT 0,
                        books_saved INTEGER DEFAULT 0,
                        books_skipped INTEGER DEFAULT 0,
                        error_message TEXT,
                        pid INTEGER,
                        hostname VARCHAR,
                        started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                        finished_at TIMESTAMP WITH TIME ZONE,
                        updated_at TIMESTAMP WITH TIME ZONE
                    );
                """)
        except Exception as e:
            logger.error(f"Failed to connect to Database: {e}")
            sys.exit(1)

    def _mode_summary(self, pages_total: int) -> str:
        parts = []
        if self.args.update:
            parts.append("update")
        if self.args.turbo:
            parts.append("turbo")
        start = self.args.start_page
        end = self.args.end_page or (self.args.start_page + pages_total - 1)
        parts.append(f"pages={start}-{end}")
        parts.append(f"workers={self.args.workers}")
        parts.append(f"concurrency={self.args.concurrency}")
        return " ".join(parts)

    async def start_run(self, pages_total: int):
        self.pages_total = pages_total
        mode = self._mode_summary(pages_total)
        async with self.db_pool.acquire() as conn:
            self.run_id = await conn.fetchval(
                """
                INSERT INTO scraper_runs
                    (status, mode, pages_total, pages_done, books_saved, books_skipped, pid, hostname)
                VALUES ('RUNNING', $1, $2, 0, 0, 0, $3, $4)
                RETURNING id
                """,
                mode,
                pages_total,
                os.getpid(),
                socket.gethostname(),
            )
        logger.info(f"Scraper run #{self.run_id} started")

    async def update_run(self):
        if not self.run_id:
            return
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE scraper_runs
                    SET pages_done = $2,
                        books_saved = $3,
                        books_skipped = $4,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    self.run_id,
                    self.pages_done,
                    self.books_saved,
                    self.books_skipped,
                )
        except Exception as e:
            logger.error(f"Failed to update scraper run status: {e}")

    async def finish_run(self, status: str = "COMPLETED", error: str | None = None):
        if not self.run_id:
            return
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE scraper_runs
                    SET status = $2::scraperrunstatus,
                        pages_done = $3,
                        books_saved = $4,
                        books_skipped = $5,
                        error_message = $6,
                        finished_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    self.run_id,
                    status,
                    self.pages_done,
                    self.books_saved,
                    self.books_skipped,
                    error,
                )
        except Exception as e:
            logger.error(f"Failed to finish scraper run status: {e}")

    async def get_total_pages(self, session: aiohttp.ClientSession) -> int:
        """Auto-detects the last page number."""
        logger.info("Detecting total pages...")
        url = f"{self.base_url}/explore/page/1"
        html = await self.fetch(session, url)
        if not html:
            return 1

        soup = BeautifulSoup(html, "lxml")
        paging_links = soup.select(".paging a")
        last_page = 1

        for link in paging_links:
            href = link.get("href", "")
            match = re.search(r"/page/(\d+)", href)
            if match:
                num = int(match.group(1))
                if num > last_page:
                    last_page = num

            text_nums = re.findall(r"\d+", link.text)
            if text_nums:
                num = int(text_nums[-1])
                if num > last_page:
                    last_page = num

        logger.info(f"Total pages detected: {last_page}")
        return last_page

    async def fetch(self, session: aiohttp.ClientSession, url: str) -> str | None:
        """High-speed fetcher (Text)."""
        async with self.semaphore:
            for attempt in range(self.args.retries):
                try:
                    async with session.get(
                        url, headers=self.headers, timeout=self.args.timeout
                    ) as response:
                        if response.status == 200:
                            return await response.text()
                        elif response.status == 404:
                            return None
                        else:
                            wait_time = 0.5 if self.args.turbo else (2**attempt)
                            if attempt < self.args.retries - 1:
                                await asyncio.sleep(wait_time)
                except Exception:
                    if attempt < self.args.retries - 1:
                        await asyncio.sleep(0.5)
            return None

    async def fetch_bytes(self, session: aiohttp.ClientSession, url: str) -> bytes | None:
        """High-speed fetcher (Bytes)."""
        async with self.semaphore:
            for attempt in range(self.args.retries):
                try:
                    async with session.get(
                        url, headers=self.headers, timeout=self.args.timeout
                    ) as response:
                        if response.status == 200:
                            return await response.read()
                        elif response.status == 404:
                            return None
                        else:
                            wait_time = 0.5 if self.args.turbo else (2**attempt)
                            if attempt < self.args.retries - 1:
                                await asyncio.sleep(wait_time)
                except Exception:
                    if attempt < self.args.retries - 1:
                        await asyncio.sleep(0.5)
            return None

    async def upload_image_to_ftp(self, session, img_url, folder_name):
        """Download image and stream directly to FTP. Returns cover filename or None."""
        if not img_url:
            return None

        image_data = await self.fetch_bytes(session, img_url)
        if not image_data:
            return None

        # Detect extension
        ext = "jpg"
        if img_url.lower().endswith(".png"):
            ext = "png"
        elif img_url.lower().endswith(".webp"):
            ext = "webp"

        filename = f"cover.{ext}"

        ftp_client = None
        try:
            client = aioftp.Client()
            await client.connect(FTP_HOST, FTP_PORT)
            await client.login(FTP_USER, FTP_PASS)
            ftp_client = client

            try:
                await ftp_client.make_directory(folder_name)
            except aioftp.StatusCodeError:
                pass

            await ftp_client.change_directory(folder_name)

            async with ftp_client.upload_stream(filename) as stream:
                await stream.write(image_data)

            logger.info(f"Uploaded cover for {folder_name}")
            return filename

        except Exception as e:
            logger.error(f"FTP Error for {folder_name}: {e}")
            return None
        finally:
            if ftp_client:
                await ftp_client.quit()

    async def check_exists(self, url: str) -> bool:
        """Fast existence check by URL."""
        if url in self.known_urls:
            return True

        async with self.db_pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM books WHERE url = $1", url)
            if exists:
                self.known_urls.add(url)
                return True
            return False

    @staticmethod
    def _normalize_text(value: str | None) -> str:
        text = (value or "").strip().lower()
        text = re.sub(r"[\s\u200c\u200f\u202a-\u202e]+", " ", text)
        text = re.sub(r"[^\w\u0600-\u06ff\s]+", "", text, flags=re.UNICODE)
        return text.strip()

    def content_key(
        self,
        title_fa: str | None,
        title_en: str | None,
        author: str | None,
        isbn: str | None,
    ) -> str:
        """Fingerprint for near-duplicate books (same title/ISBN across different URLs)."""
        isbn_clean = re.sub(r"[\s\-]", "", (isbn or "").strip().lower())
        if isbn_clean and len(isbn_clean) >= 8:
            return f"isbn:{isbn_clean}"

        title = self._normalize_text(title_en) or self._normalize_text(title_fa)
        author_n = self._normalize_text(author)
        if not title:
            return ""
        return f"ta:{title}|{author_n}"

    async def load_known_content_keys(self):
        """Preload ISBN / title fingerprints so we skip asbook duplicates."""
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT url, title, title_en, author, isbn FROM books"
            )
        for row in rows:
            if row["url"]:
                self.known_urls.add(row["url"])
            key = self.content_key(
                row["title"], row["title_en"], row["author"], row["isbn"]
            )
            if key:
                self.known_content_keys.add(key)
        logger.info(
            f"Loaded {len(self.known_urls)} URLs and {len(self.known_content_keys)} content keys"
        )

    async def is_content_duplicate(self, data: dict) -> bool:
        key = self.content_key(
            data.get("title_fa"),
            data.get("title_en"),
            data.get("author"),
            data.get("isbn"),
        )
        if not key:
            return False
        if key in self.known_content_keys:
            return True
        # DB race / missed preload
        async with self.db_pool.acquire() as conn:
            isbn_clean = re.sub(r"[\s\-]", "", (data.get("isbn") or "").strip())
            if isbn_clean and len(isbn_clean) >= 8:
                exists = await conn.fetchval(
                    """
                    SELECT 1 FROM books
                    WHERE REPLACE(REPLACE(LOWER(COALESCE(isbn, '')), '-', ''), ' ', '') = $1
                    LIMIT 1
                    """,
                    isbn_clean.lower(),
                )
                if exists:
                    self.known_content_keys.add(key)
                    return True
            title = self._normalize_text(data.get("title_en")) or self._normalize_text(
                data.get("title_fa")
            )
            author = self._normalize_text(data.get("author"))
            if title:
                exists = await conn.fetchval(
                    """
                    SELECT 1 FROM books
                    WHERE lower(regexp_replace(coalesce(title_en, title, ''), '\\s+', ' ', 'g')) = $1
                      AND lower(regexp_replace(coalesce(author, ''), '\\s+', ' ', 'g')) = $2
                    LIMIT 1
                    """,
                    title,
                    author,
                )
                if exists:
                    self.known_content_keys.add(key)
                    return True
        return False

    # WORKER 1: EXPLORER (Page -> Book URLs)
    async def explorer_worker(self, worker_id, session):
        while True:
            try:
                page_num = self.page_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                url = f"{self.base_url}/explore/page/{page_num}"
                html = await self.fetch(session, url)

                if not html:
                    self.page_queue.task_done()
                    continue

                soup = BeautifulSoup(html, "lxml")
                items = soup.select("div.expitem")

                new_books_count = 0
                existing_count = 0

                for item in items:
                    link_tag = item.select_one("div.img-overlay a")

                    if link_tag and link_tag.get("href"):
                        href = link_tag.get("href")
                        if not href.startswith("http"):
                            href = f"{self.base_url}/{href.lstrip('/')}"

                        if await self.check_exists(href):
                            existing_count += 1
                            self.books_skipped += 1
                            continue

                        meta = {"url": href}
                        await self.details_queue.put(meta)
                        new_books_count += 1

                # Smart Update Logic
                if self.args.update and existing_count > 0 and new_books_count == 0:
                    logger.info(
                        f"Explorer-{worker_id}: Page {page_num} only has existing books. Triggering stop."
                    )
                    self.stop_discovery.set()
                    while not self.page_queue.empty():
                        try:
                            self.page_queue.get_nowait()
                            self.page_queue.task_done()
                        except:
                            break

                if page_num % 100 == 0:
                    logger.info(
                        f"Explorer-{worker_id}: Processed Page {page_num} ({new_books_count} new)"
                    )

            except Exception as e:
                logger.error(f"Explorer-{worker_id} error: {e}")
            finally:
                self.pages_done += 1
                if self.pages_done % 25 == 0:
                    await self.update_run()
                self.page_queue.task_done()

    # WORKER 2: DOWNLOADER (URL -> Details)
    async def downloader_worker(self, worker_id, session):
        while True:
            if self.stop_discovery.is_set() and self.details_queue.empty():
                break

            try:
                book_meta = await asyncio.wait_for(
                    self.details_queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                continue

            try:
                url = book_meta["url"]
                html = await self.fetch(session, url)
                if html:
                    if "کتاب مورد از دسترس خارج گردید" in html:
                        # Handle removed book (mark as inactive or skip)
                        logger.info(f"Skipped removed book: {url}")
                        # Removed redundant task_done here, finally block handles it
                        continue

                    soup = BeautifulSoup(html, "lxml")

                    # Data Extraction Logic
                    img_tag = soup.select_one("div.article img.cover")
                    image_url = img_tag.get("src") if img_tag else ""

                    info = {}
                    table_rows = soup.select("div.article table tbody tr")
                    for row in table_rows:
                        th = row.select_one("th")
                        td = row.select_one("td")
                        if th and td:
                            key = th.text.strip()
                            if td.select_one("h2"):
                                value = td.select_one("h2").text.strip()
                            else:
                                value = td.text.strip()
                            info[key] = value

                    def get_meta(name):
                        tag = soup.find("meta", attrs={"name": name})
                        return tag.get("content") if tag else ""

                    price_str = get_meta("productprice")
                    try:
                        raw_price = float(re.sub(r"[^\d.]", "", price_str))
                    except Exception:
                        raw_price = 0.0

                    # Sale price 2–3k below source; strikethrough original 30–40k above sale
                    sale_price = max(0.0, raw_price - random.randint(2000, 3000))
                    original_price = sale_price + random.randint(30000, 40000)

                    availability = get_meta("availability")

                    amz_tag = soup.select_one('div.avl a[href*="amazon.com"]')
                    amazon_link = amz_tag.get("href") if amz_tag else ""

                    desc_tag = soup.select_one("#fadesc .desc") or soup.select_one(
                        "#fadesc"
                    )
                    description = desc_tag.text.strip() if desc_tag else ""

                    title = info.get("عنوان فارسی", "Unknown")
                    title_en = info.get("عنوان اصلی", "")
                    author = info.get("نویسنده", "")
                    isbn = info.get("ISBN", "")

                    preview = {
                        "title_fa": title,
                        "title_en": title_en,
                        "author": author,
                        "isbn": isbn,
                    }
                    if await self.is_content_duplicate(preview):
                        self.books_skipped += 1
                        logger.info(f"Skipped duplicate content: {title[:80]}")
                        continue

                    folder_name = Book.storage_folder_from_isbn_or_url(isbn, url)

                    cover_filename = "cover.jpg"
                    if image_url:
                        if not image_url.startswith("http"):
                            image_url = self.base_url + image_url
                        uploaded = await self.upload_image_to_ftp(
                            session, image_url, folder_name
                        )
                        if uploaded:
                            cover_filename = uploaded

                    data = {
                        "url": url,
                        "title_fa": title,
                        "title_en": title_en,
                        "author": author,
                        "publisher": info.get("ناشر", ""),
                        "isbn": isbn,
                        "year": info.get("سال نشر", ""),
                        "language": info.get("زبان", ""),
                        "pages": info.get("تعداد صفحات", ""),
                        "format": info.get("فرمت کتاب", ""),
                        "size": info.get("حجم فایل", ""),
                        "edition": info.get("ویرایش", ""),
                        "price": sale_price,
                        "original_price": original_price,
                        "availability": availability,
                        "amazon_link": amazon_link,
                        "image_url": image_url,
                        "description": description,
                        "folder_name": folder_name,
                        "cover_filename": cover_filename,
                    }

                    # Reserve key before queueing to reduce concurrent dupes
                    key = self.content_key(title, title_en, author, isbn)
                    if key:
                        self.known_content_keys.add(key)
                    self.known_urls.add(url)

                    await self.db_queue.put(data)

            except Exception as e:
                logger.error(
                    f"Downloader-{worker_id} error on {book_meta.get('url')}: {e}"
                )
            finally:
                self.details_queue.task_done()

    # WORKER 3: DB WRITER
    async def db_writer(self):
        batch = []
        while True:
            data = await self.db_queue.get()
            if data is None:
                break

            batch.append(data)
            self.db_queue.task_done()

            if len(batch) >= 200:
                await self.save_batch(batch)
                batch = []

        if batch:
            await self.save_batch(batch)

    async def save_batch(self, batch):
        try:
            # Drop content duplicates inside the batch itself
            unique = []
            seen_keys: set[str] = set()
            for d in batch:
                key = self.content_key(
                    d.get("title_fa"), d.get("title_en"), d.get("author"), d.get("isbn")
                )
                if key and key in seen_keys:
                    self.books_skipped += 1
                    continue
                if key:
                    seen_keys.add(key)
                unique.append(d)

            async with self.db_pool.acquire() as conn:
                await conn.executemany(
                    """
                    INSERT INTO books
                    (url, title, title_en, author, publisher, isbn,
                    publish_year, language, pages, file_format, file_size,
                    edition, price, original_price, availability, amazon_link,
                    image_url, description, folder_name, cover_filename, has_pdf)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                    $12, $13, $14, $15, $16, $17, $18, $19, $20, FALSE)
                    ON CONFLICT (url) DO NOTHING
                    """,
                    [
                        (
                            d["url"],
                            d["title_fa"],
                            d["title_en"],
                            d["author"],
                            d["publisher"],
                            d["isbn"],
                            d["year"],
                            d["language"],
                            d["pages"],
                            d["format"],
                            d["size"],
                            d["edition"],
                            d["price"],
                            d["original_price"],
                            d["availability"],
                            d["amazon_link"],
                            d["image_url"],
                            d["description"],
                            d["folder_name"],
                            d.get("cover_filename", "cover.jpg"),
                        )
                        for d in unique
                    ],
                )
                self.books_saved += len(unique)
                logger.info(f"DB: Saved {len(unique)} books")
                await self.update_run()
        except Exception as e:
            logger.error(f"DB Write Error: {e}")

    async def main(self):
        await self.init_db()
        await self.load_known_content_keys()
        error_msg = None
        try:
            conn = aiohttp.TCPConnector(limit=0)

            async with aiohttp.ClientSession(connector=conn) as session:
                start = self.args.start_page
                end = self.args.end_page or await self.get_total_pages(session)

                logger.info(f"Target: Pages {start}-{end}")
                await self.start_run(end - start + 1)

                for i in range(start, end + 1):
                    self.page_queue.put_nowait(i)

                tasks = []
                db_task = asyncio.create_task(self.db_writer())

                for i in range(10):
                    tasks.append(asyncio.create_task(self.explorer_worker(i, session)))

                dl_tasks = []
                for i in range(self.args.workers):
                    dl_tasks.append(
                        asyncio.create_task(self.downloader_worker(i, session))
                    )

                await self.page_queue.join()
                self.stop_discovery.set()

                await self.details_queue.join()
                await asyncio.gather(*dl_tasks)

                await self.db_queue.put(None)
                await db_task

                for t in tasks:
                    t.cancel()

            await self.finish_run("COMPLETED")
            logger.info("Scraping Completed Successfully.")
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Scraping failed: {e}")
            await self.finish_run("FAILED", error=error_msg)
            raise
        finally:
            if self.db_pool:
                await self.db_pool.close()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="High Performance Pipeline Scraper")
    parser.add_argument("--url", default=DEFAULT_BASE_URL)
    parser.add_argument("--turbo", action="store_true", help="Enable aggressive settings")
    parser.add_argument("--update", action="store_true", help="Smart update mode")
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)

    args = parser.parse_args()

    if args.turbo:
        args.concurrency = 200
        args.workers = 100
        args.timeout = 15
        print(">>> TURBO MODE ENGAGED: Concurrency=200, Workers=100")

    scraper = BookScraper(args)
    asyncio.run(scraper.main())
