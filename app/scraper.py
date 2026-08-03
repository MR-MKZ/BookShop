import argparse
import asyncio
import hashlib
import logging
import os
import random
import re
import signal
import socket
import sys
from urllib.parse import quote, urlsplit, urlunsplit

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


def resolve_proxy_url(cli_proxy: str | None = None) -> str | None:
    """CLI --proxy wins, then SCRAPER_PROXY / standard HTTP(S)_PROXY env vars."""
    for candidate in (
        cli_proxy,
        os.getenv("SCRAPER_PROXY"),
        os.getenv("SCRAPER_HTTP_PROXY"),
        os.getenv("HTTPS_PROXY"),
        os.getenv("HTTP_PROXY"),
        os.getenv("ALL_PROXY"),
        os.getenv("https_proxy"),
        os.getenv("http_proxy"),
        os.getenv("all_proxy"),
    ):
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


def redact_proxy_url(proxy: str) -> str:
    """Hide password in logs."""
    parts = urlsplit(proxy)
    if not parts.hostname:
        return proxy
    auth = ""
    if parts.username or parts.password:
        user = parts.username or ""
        auth = f"{user}:***@"
    host = parts.hostname
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment)
    )


def is_socks_proxy(proxy: str) -> bool:
    return proxy.lower().startswith(("socks4://", "socks5://", "socks5h://"))


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
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9,fa;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

        self.proxy_url = resolve_proxy_url(getattr(args, "proxy", None))
        # HTTP(S) proxy passed per-request; SOCKS is baked into the connector
        self._request_proxy: str | None = None

        self.known_urls: set[str] = set()
        self.known_content_keys: set[str] = set()
        # Cover-token -> listing fields (ناشر/year) recovered from explore.php search
        self._listing_meta_cache: dict[str, dict] = {}
        self.db_pool = None
        self.run_id: int | None = None
        self.pages_total = 0
        self.pages_done = 0
        self.books_saved = 0
        self.books_skipped = 0

    def _build_connector(self) -> aiohttp.BaseConnector:
        proxy = self.proxy_url
        if proxy and is_socks_proxy(proxy):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError as exc:
                raise RuntimeError(
                    "SOCKS proxy needs aiohttp-socks — "
                    "pip install aiohttp-socks / rebuild scraper image"
                ) from exc
            self._request_proxy = None
            logger.info("Scraper proxy (SOCKS): %s", redact_proxy_url(proxy))
            return ProxyConnector.from_url(proxy, limit=0)

        self._request_proxy = proxy
        if proxy:
            logger.info("Scraper proxy (HTTP): %s", redact_proxy_url(proxy))
        else:
            logger.info("Scraper proxy: none (direct)")
        return aiohttp.TCPConnector(limit=0)

    def _get_kwargs(self) -> dict:
        kw: dict = {
            "headers": self.headers,
            "timeout": aiohttp.ClientTimeout(total=self.args.timeout),
        }
        if self._request_proxy:
            kw["proxy"] = self._request_proxy
        return kw

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
                        slug VARCHAR UNIQUE,
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
                    ALTER TABLE books ADD COLUMN IF NOT EXISTS slug VARCHAR;
                """)
                await conn.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS ix_books_slug ON books (slug);
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
        if getattr(self.args, "content_dedup", False):
            parts.append("content-dedup")
        if self.proxy_url:
            parts.append("proxy")
        start = self.args.start_page
        end = self.args.end_page or (self.args.start_page + pages_total - 1)
        parts.append(f"pages={start}-{end}")
        parts.append(f"workers={self.args.workers}")
        parts.append(f"concurrency={self.args.concurrency}")
        return " ".join(parts)

    @property
    def content_dedup_enabled(self) -> bool:
        return bool(getattr(self.args, "content_dedup", False))

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
        """Auto-detects the last page number from explore paging links."""
        logger.info("Detecting total pages...")
        url = f"{self.base_url}/explore/page/1/"
        html = None
        for attempt in range(max(1, self.args.retries)):
            html = await self.fetch(session, url)
            if html:
                break
            logger.warning(
                "Explore page fetch failed (attempt %s/%s): %s",
                attempt + 1,
                self.args.retries,
                url,
            )
            if attempt < self.args.retries - 1:
                await asyncio.sleep(2**attempt)

        if not html:
            raise RuntimeError(
                f"Cannot detect total pages — failed to fetch {url}. "
                "Check outbound HTTPS from this host to asbook.ir "
                "(firewall/DNS/proxy), then retry."
            )

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

        if last_page <= 1:
            logger.warning(
                "Paging links not found or only page 1 — check asbook HTML (.paging a)"
            )
        logger.info(f"Total pages detected: {last_page}")
        return last_page

    async def fetch(self, session: aiohttp.ClientSession, url: str) -> str | None:
        """High-speed fetcher (Text)."""
        async with self.semaphore:
            for attempt in range(self.args.retries):
                try:
                    async with session.get(url, **self._get_kwargs()) as response:
                        if response.status == 200:
                            return await response.text()
                        elif response.status == 404:
                            return None
                        else:
                            logger.warning(
                                "HTTP %s for %s (attempt %s/%s)",
                                response.status,
                                url,
                                attempt + 1,
                                self.args.retries,
                            )
                            wait_time = 0.5 if self.args.turbo else (2**attempt)
                            if attempt < self.args.retries - 1:
                                await asyncio.sleep(wait_time)
                except Exception as e:
                    logger.warning(
                        "Fetch error %s (attempt %s/%s): %s",
                        url,
                        attempt + 1,
                        self.args.retries,
                        e,
                    )
                    if attempt < self.args.retries - 1:
                        await asyncio.sleep(0.5)
            logger.error("Giving up fetching %s after %s attempts", url, self.args.retries)
            return None

    async def fetch_bytes(self, session: aiohttp.ClientSession, url: str) -> bytes | None:
        """High-speed fetcher (Bytes)."""
        async with self.semaphore:
            for attempt in range(self.args.retries):
                try:
                    async with session.get(url, **self._get_kwargs()) as response:
                        if response.status == 200:
                            return await response.read()
                        elif response.status == 404:
                            return None
                        else:
                            wait_time = 0.5 if self.args.turbo else (2**attempt)
                            if attempt < self.args.retries - 1:
                                await asyncio.sleep(wait_time)
                except Exception as e:
                    logger.warning(
                        "Fetch-bytes error %s (attempt %s/%s): %s",
                        url,
                        attempt + 1,
                        self.args.retries,
                        e,
                    )
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
        client = None
        try:
            from app.services.ftp_client import enter_ftp_base

            client = aioftp.Client()
            await client.connect(FTP_HOST, FTP_PORT)
            await client.login(FTP_USER, FTP_PASS)
            await enter_ftp_base(client)

            try:
                await client.make_directory(folder_name)
            except aioftp.StatusCodeError:
                pass

            await client.change_directory(folder_name)

            async with client.upload_stream(filename) as stream:
                await stream.write(image_data)

            logger.info(f"Uploaded cover for {folder_name}")
            return filename

        except Exception as e:
            logger.error(f"FTP Error for {folder_name}: {e}")
            return None
        finally:
            if client is not None:
                try:
                    await client.quit()
                except Exception:
                    pass

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

    def _normalize_author(self, value: str | None) -> str:
        author_n = self._normalize_text(value)
        if author_n in {
            "",
            "unknown",
            "unkown",
            "n a",
            "na",
            "none",
            "null",
            "ناشناس",
            "نامشخص",
            "نامعلوم",
        }:
            return ""
        return author_n

    def content_key(
        self,
        title_fa: str | None,
        title_en: str | None,
        author: str | None,
        isbn: str | None,
        publisher: str | None = None,
        year: str | None = None,
    ) -> str:
        """Fingerprint for near-duplicate books (same title/ISBN across different URLs).

        asbook often leaves detail-page ناشر empty while listing search shows the
        real publisher string (issue # / month for magazines). Include publisher
        and year when present. If identity is too weak (no ISBN, author, or
        publisher), return "" so URL uniqueness alone applies.
        """
        isbn_clean = re.sub(r"[\s\-]", "", (isbn or "").strip().lower())
        if isbn_clean and len(isbn_clean) >= 8:
            return f"isbn:{isbn_clean}"

        title = self._normalize_text(title_en) or self._normalize_text(title_fa)
        author_n = self._normalize_author(author)
        publisher_n = self._normalize_text(publisher)
        year_n = self._normalize_text(year)
        if not title:
            return ""

        if author_n:
            if publisher_n or year_n:
                return f"ta:{title}|{author_n}|{publisher_n}|{year_n}"
            return f"ta:{title}|{author_n}"

        if publisher_n:
            return f"tp:{title}|{publisher_n}|{year_n}"

        # e.g. magazine issues that share a title with empty detail metadata
        return ""

    @staticmethod
    def _cover_search_token(image_url: str | None) -> str:
        """Prefer cover md5 (unique); fall back to cover id."""
        if not image_url:
            return ""
        md5 = re.search(r"[?&]md5=([A-Fa-f0-9]+)", image_url, re.I)
        if md5:
            return md5.group(1)
        cover_id = re.search(r"[?&]id=(\d+)", image_url)
        if cover_id:
            return cover_id.group(1)
        return ""

    @staticmethod
    def _parse_listing_card(card) -> dict:
        meta: dict[str, str] = {}
        for p in card.select("p"):
            text = p.get_text(" ", strip=True)
            if ":" not in text:
                continue
            key, _, value = text.partition(":")
            key, value = key.strip(), value.strip()
            if key.startswith("ناشر"):
                meta["publisher"] = value
            elif key.startswith("سال"):
                meta["year"] = value
            elif key.startswith("نویسند"):
                meta["author"] = value
        return meta

    @staticmethod
    def _urls_match(book_url: str, href: str, base_url: str) -> bool:
        if not href:
            return False
        if not href.startswith("http"):
            href = f"{base_url.rstrip('/')}/{href.lstrip('/')}"
        return book_url.rstrip("/") == href.rstrip("/")

    async def lookup_listing_meta(
        self, session, book_url: str, image_url: str | None
    ) -> dict:
        """Recover ناشر / year from explore.php search (asbook detail bug workaround)."""
        token = self._cover_search_token(image_url)
        if not token:
            return {}
        if token in self._listing_meta_cache:
            return self._listing_meta_cache[token]

        search_url = f"{self.base_url}/explore.php?search={quote(token)}"
        html = await self.fetch(session, search_url)
        result: dict = {}
        if html:
            soup = BeautifulSoup(html, "lxml")
            cards = soup.select("div.main-srch")
            for card in cards:
                link = card.select_one('a[href*="/book/"]')
                href = link.get("href") if link else ""
                if self._urls_match(book_url, href, self.base_url):
                    result = self._parse_listing_card(card)
                    break
            if not result and len(cards) == 1:
                result = self._parse_listing_card(cards[0])

        self._listing_meta_cache[token] = result
        return result

    async def load_known_content_keys(self):
        """Preload known URLs (always) and optional content fingerprints."""
        if self.content_dedup_enabled:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT url, title, title_en, author, isbn, publisher, publish_year
                    FROM books
                    """
                )
            for row in rows:
                if row["url"]:
                    self.known_urls.add(row["url"])
                key = self.content_key(
                    row["title"],
                    row["title_en"],
                    row["author"],
                    row["isbn"],
                    row["publisher"],
                    row["publish_year"],
                )
                if key:
                    self.known_content_keys.add(key)
            logger.info(
                "Loaded %s URLs and %s content keys (content-dedup ON)",
                len(self.known_urls),
                len(self.known_content_keys),
            )
            return

        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT url FROM books WHERE url IS NOT NULL")
        for row in rows:
            self.known_urls.add(row["url"])
        logger.info(
            "Loaded %s URLs (content-dedup OFF — skip only by exact URL)",
            len(self.known_urls),
        )

    async def is_content_duplicate(self, data: dict) -> bool:
        if not self.content_dedup_enabled:
            return False
        key = self.content_key(
            data.get("title_fa"),
            data.get("title_en"),
            data.get("author"),
            data.get("isbn"),
            data.get("publisher"),
            data.get("year"),
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
            author = self._normalize_author(data.get("author"))
            publisher = self._normalize_text(data.get("publisher"))
            if title and (author or publisher):
                # Compare via content_key in Python so punctuation in ناشر
                # (issue # / month) matches preload normalization.
                rows = await conn.fetch(
                    """
                    SELECT title, title_en, author, isbn, publisher, publish_year
                    FROM books
                    WHERE lower(regexp_replace(coalesce(title_en, title, ''), '\\s+', ' ', 'g')) = $1
                    LIMIT 50
                    """,
                    title,
                )
                for row in rows:
                    row_key = self.content_key(
                        row["title"],
                        row["title_en"],
                        row["author"],
                        row["isbn"],
                        row["publisher"],
                        row["publish_year"],
                    )
                    if row_key and row_key == key:
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
                url = f"{self.base_url}/explore/page/{page_num}/"
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
                    # Round down to whole thousands so storefront prices stay clean.
                    sale_price = max(0.0, raw_price - random.randint(2000, 3000))
                    sale_price = (int(sale_price) // 1000) * 1000
                    original_price = sale_price + random.randint(30000, 40000)
                    original_price = (int(original_price) // 1000) * 1000

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
                    publisher = info.get("ناشر", "")
                    year = info.get("سال نشر", "")

                    # asbook bug: listing search shows ناشر (issue # / month for
                    # magazines) but the detail table often leaves it blank.
                    if not (publisher or "").strip():
                        listing = await self.lookup_listing_meta(
                            session, url, image_url
                        )
                        if listing:
                            publisher = listing.get("publisher", "") or publisher
                            year = year or listing.get("year", "")
                            if not (author or "").strip():
                                author = listing.get("author", "") or author

                    if self.content_dedup_enabled:
                        preview = {
                            "title_fa": title,
                            "title_en": title_en,
                            "author": author,
                            "isbn": isbn,
                            "publisher": publisher,
                            "year": year,
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
                        "publisher": publisher,
                        "isbn": isbn,
                        "year": year,
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

                    # Reserve content key only when fingerprint dedup is enabled
                    if self.content_dedup_enabled:
                        key = self.content_key(
                            title, title_en, author, isbn, publisher, year
                        )
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
        """Flush often so Ctrl+C / docker stop does not lose scraped rows."""
        batch = []
        flush_size = max(1, int(os.getenv("SCRAPER_FLUSH_SIZE", "25")))
        flush_timeout = max(1.0, float(os.getenv("SCRAPER_FLUSH_TIMEOUT", "5")))
        logger.info(
            "DB writer flush_size=%s flush_timeout=%ss", flush_size, flush_timeout
        )
        while True:
            try:
                data = await asyncio.wait_for(
                    self.db_queue.get(), timeout=flush_timeout
                )
            except asyncio.TimeoutError:
                if batch:
                    await self.save_batch(batch)
                    batch = []
                continue

            if data is None:
                self.db_queue.task_done()
                break

            batch.append(data)
            self.db_queue.task_done()

            if len(batch) >= flush_size:
                await self.save_batch(batch)
                batch = []

        if batch:
            await self.save_batch(batch)

    async def save_batch(self, batch):
        try:
            # Drop in-batch duplicates: by content fingerprint (optional) or URL only
            unique = []
            seen_keys: set[str] = set()
            seen_urls: set[str] = set()
            for d in batch:
                url = d.get("url") or ""
                if url and url in seen_urls:
                    self.books_skipped += 1
                    continue
                if self.content_dedup_enabled:
                    key = self.content_key(
                        d.get("title_fa"),
                        d.get("title_en"),
                        d.get("author"),
                        d.get("isbn"),
                        d.get("publisher"),
                        d.get("year"),
                    )
                    if key and key in seen_keys:
                        self.books_skipped += 1
                        continue
                    if key:
                        seen_keys.add(key)
                if url:
                    seen_urls.add(url)
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
                # Fill SEO slugs for newly inserted rows (English title + id)
                urls = [d["url"] for d in unique if d.get("url")]
                if urls:
                    rows = await conn.fetch(
                        """
                        SELECT id, title, title_en FROM books
                        WHERE url = ANY($1::text[])
                          AND (slug IS NULL OR slug = '')
                        """,
                        urls,
                    )
                    for row in rows:
                        slug = Book.build_slug(
                            row["title_en"], row["title"], book_id=row["id"]
                        )
                        await conn.execute(
                            "UPDATE books SET slug = $1 WHERE id = $2 AND (slug IS NULL OR slug = '')",
                            slug,
                            row["id"],
                        )
                self.books_saved += len(unique)
                logger.info(
                    "DB: flushed %s books (running total=%s)",
                    len(unique),
                    self.books_saved,
                )
                await self.update_run()
        except Exception as e:
            logger.error(f"DB Write Error: {e}")

    async def main(self):
        await self.init_db()
        await self.load_known_content_keys()
        error_msg = None
        stop_requested = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop():
            if not stop_requested.is_set():
                logger.warning(
                    "Stop signal received — flushing DB and shutting down workers..."
                )
                stop_requested.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                pass

        db_task = None
        try:
            connector = self._build_connector()

            async with aiohttp.ClientSession(connector=connector) as session:
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

                join_pages = asyncio.create_task(self.page_queue.join())
                stop_wait = asyncio.create_task(stop_requested.wait())
                done, pending = await asyncio.wait(
                    {join_pages, stop_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()

                self.stop_discovery.set()

                if stop_requested.is_set():
                    for t in dl_tasks + tasks:
                        t.cancel()
                    await asyncio.gather(*dl_tasks, *tasks, return_exceptions=True)
                    # Allow timeout-based writer to flush any queued rows
                    await asyncio.sleep(6)
                else:
                    await self.details_queue.join()
                    await asyncio.gather(*dl_tasks)
                    for t in tasks:
                        t.cancel()

                await self.db_queue.put(None)
                await db_task

            await self.finish_run("COMPLETED")
            logger.info(
                "Scraping finished. books_saved=%s early_stop=%s",
                self.books_saved,
                stop_requested.is_set(),
            )
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Scraping failed: {e}")
            if db_task and not db_task.done():
                try:
                    await self.db_queue.put(None)
                    await asyncio.wait_for(db_task, timeout=30)
                except Exception:
                    pass
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
    parser.add_argument(
        "--update",
        action="store_true",
        help="Smart update: stop when a listing page has only known books",
    )
    parser.add_argument(
        "--content-dedup",
        action="store_true",
        default=False,
        help=(
            "Skip books that match an existing title/ISBN/publisher fingerprint. "
            "Off by default: only exact asbook URL is skipped so every listing "
            "(e.g. magazine issues) is saved."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep running: after each pass, sleep and scrape again (use with --update)",
    )
    parser.add_argument(
        "--loop-interval",
        type=int,
        default=int(os.getenv("SCRAPER_LOOP_INTERVAL", "3600")),
        help="Seconds between loop passes (default: env SCRAPER_LOOP_INTERVAL or 3600)",
    )
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--proxy",
        default=None,
        help=(
            "HTTP(S) or SOCKS proxy for asbook fetches, e.g. "
            "http://user:pass@host:8080 or socks5://127.0.0.1:1080. "
            "Overrides SCRAPER_PROXY / HTTP_PROXY env."
        ),
    )

    args = parser.parse_args()

    if args.turbo:
        args.concurrency = 200
        args.workers = 100
        args.timeout = 15
        print(">>> TURBO MODE ENGAGED: Concurrency=200, Workers=100")

    if args.loop and not args.update:
        args.update = True
        logger.info("--loop enabled: turning on --update for incremental passes")

    async def _run() -> None:
        while True:
            scraper = BookScraper(args)
            try:
                await scraper.main()
            except Exception as e:
                logger.error(f"Scraper pass failed: {e}")
                if not args.loop:
                    raise
            if not args.loop:
                break
            interval = max(60, int(args.loop_interval or 3600))
            logger.info(
                "Update pass finished. Sleeping %ss before next scrape...",
                interval,
            )
            await asyncio.sleep(interval)

    asyncio.run(_run())
