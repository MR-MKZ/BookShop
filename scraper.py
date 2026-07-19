import argparse
import asyncio
import logging
import re
import sys

import aiohttp
import aiosqlite
from bs4 import BeautifulSoup

# LEGACY: SQLite scraper kept for reference only.
# Production scraper lives at app/scraper.py (PostgreSQL + FTP).

# --- DEFAULTS ---
DEFAULT_BASE_URL = "https://asbook.ir"
DEFAULT_DB_NAME = "books_data.db"
DEFAULT_CONCURRENCY = 50

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.FileHandler("scraper.log"), logging.StreamHandler(sys.stdout)],
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

    async def init_db(self):
        """Initialize database with expanded schema for detailed book info."""
        async with aiosqlite.connect(self.args.db) as db:
            # Dropping table if it exists to ensure new schema is applied (CAUTION in production)
            # For this exercise, we assume we want the new structure.
            # If you want to keep old data, you'd need a migration script.
            # await db.execute("DROP TABLE IF EXISTS books")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS books (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    title_fa TEXT,
                    title_en TEXT,
                    author TEXT,
                    publisher TEXT,
                    isbn TEXT,
                    publish_year TEXT,
                    language TEXT,
                    pages TEXT,
                    file_format TEXT,
                    file_size TEXT,
                    edition TEXT,
                    price TEXT,
                    availability TEXT,
                    amazon_link TEXT,
                    image_url TEXT,
                    description TEXT,
                    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_url ON books(url)")
            await db.commit()

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
        """High-speed fetcher."""
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

    async def check_exists(self, url: str, db) -> bool:
        """Fast existence check."""
        if url in self.known_urls:
            return True
        async with db.execute("SELECT 1 FROM books WHERE url = ?", (url,)) as cursor:
            exists = await cursor.fetchone() is not None
            if exists:
                self.known_urls.add(url)
            return exists

    # WORKER 1: EXPLORER (Page -> Book URLs)
    async def explorer_worker(self, worker_id, session):
        async with aiosqlite.connect(self.args.db) as db:
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

                            if await self.check_exists(href, db):
                                existing_count += 1
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
                        soup = BeautifulSoup(html, "lxml")
                        h1 = soup.select_one("h1")
                        title = h1.text.strip() if h1 else "Removed Book"

                        data = {
                            "url": url,
                            "title_fa": title,
                            "title_en": "",
                            "author": "",
                            "publisher": "",
                            "isbn": "",
                            "year": "",
                            "language": "",
                            "pages": "",
                            "format": "",
                            "size": "",
                            "edition": "",
                            "price": "",
                            "availability": "REMOVED",
                            "amazon_link": "",
                            "image_url": "",
                            "description": "Book removed from website",
                        }
                        await self.db_queue.put(data)
                        logger.info(f"Skipped removed book: {url}")
                        self.details_queue.task_done()
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

                    price = get_meta("productprice")
                    availability = get_meta("availability")

                    amz_tag = soup.select_one('div.avl a[href*="amazon.com"]')
                    amazon_link = amz_tag.get("href") if amz_tag else ""

                    desc_tag = soup.select_one("#fadesc .desc") or soup.select_one(
                        "#fadesc"
                    )
                    description = desc_tag.text.strip() if desc_tag else ""

                    data = {
                        "url": url,
                        "title_fa": info.get("عنوان فارسی", ""),
                        "title_en": info.get("عنوان اصلی", ""),
                        "author": info.get("نویسنده", ""),
                        "publisher": info.get("ناشر", ""),
                        "isbn": info.get("ISBN", ""),
                        "year": info.get("سال نشر", ""),
                        "language": info.get("زبان", ""),
                        "pages": info.get("تعداد صفحات", ""),
                        "format": info.get("فرمت کتاب", ""),
                        "size": info.get("حجم فایل", ""),
                        "edition": info.get("ویرایش", ""),
                        "price": price,
                        "availability": availability,
                        "amazon_link": amazon_link,
                        "image_url": image_url,
                        "description": description,
                    }

                    await self.db_queue.put(data)

            except Exception as e:
                logger.error(
                    f"Downloader-{worker_id} error on {book_meta.get('url')}: {e}"
                )
            finally:
                self.details_queue.task_done()

    # WORKER 3: DB WRITER
    async def db_writer(self):
        async with aiosqlite.connect(self.args.db) as db:
            batch = []
            while True:
                data = await self.db_queue.get()
                if data is None:
                    break

                batch.append(data)
                self.db_queue.task_done()

                if len(batch) >= 200:
                    try:
                        await db.executemany(
                            """
                            INSERT OR IGNORE INTO books 
                            (url, title_fa, title_en, author, publisher, isbn, 
                            publish_year, language, pages, file_format, file_size, 
                            edition, price, availability, amazon_link, image_url, description) 
                            VALUES (:url, :title_fa, :title_en, :author, :publisher, :isbn, 
                            :year, :language, :pages, :format, :size, 
                            :edition, :price, :availability, :amazon_link, :image_url, :description)
                        """,
                            batch,
                        )
                        await db.commit()
                        logger.info(f"DB: Saved {len(batch)} books")
                        batch = []
                    except Exception as e:
                        logger.error(f"DB Write Error: {e}")

            if batch:
                await db.executemany(
                    """
                            INSERT OR IGNORE INTO books 
                            (url, title_fa, title_en, author, publisher, isbn, 
                            publish_year, language, pages, file_format, file_size, 
                            edition, price, availability, amazon_link, image_url, description) 
                            VALUES (:url, :title_fa, :title_en, :author, :publisher, :isbn, 
                            :year, :language, :pages, :format, :size, 
                            :edition, :price, :availability, :amazon_link, :image_url, :description)
                        """,
                    batch,
                )
                await db.commit()

    async def main(self):
        await self.init_db()

        conn = aiohttp.TCPConnector(limit=0)

        async with aiohttp.ClientSession(connector=conn) as session:
            start = self.args.start_page
            end = self.args.end_page or await self.get_total_pages(session)

            logger.info(f"Target: Pages {start}-{end}")
            for i in range(start, end + 1):
                self.page_queue.put_nowait(i)

            tasks = []
            db_task = asyncio.create_task(self.db_writer())

            for i in range(10):
                tasks.append(asyncio.create_task(self.explorer_worker(i, session)))

            dl_tasks = []
            for i in range(self.args.workers):
                dl_tasks.append(asyncio.create_task(self.downloader_worker(i, session)))

            await self.page_queue.join()
            self.stop_discovery.set()

            await self.details_queue.join()
            await asyncio.gather(*dl_tasks)

            await self.db_queue.put(None)
            await db_task

            for t in tasks:
                t.cancel()

        logger.info("Scraping Completed Successfully.")


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="High Performance Pipeline Scraper")
    parser.add_argument("--url", default=DEFAULT_BASE_URL)
    parser.add_argument("--db", default=DEFAULT_DB_NAME)
    parser.add_argument(
        "--turbo", action="store_true", help="Enable aggressive settings"
    )
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
    try:
        asyncio.run(scraper.main())
    except KeyboardInterrupt:
        print("\nScraper stopped.")
