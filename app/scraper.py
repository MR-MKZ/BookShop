import argparse
import asyncio
import logging
import os
import re
import sys

import aioftp
import aiohttp
import asyncpg
from bs4 import BeautifulSoup

# --- CONFIGURATION FROM ENV OR DEFAULTS ---
# تنظیمات از متغیرهای محیطی داکر خوانده می‌شوند
DB_USER = os.getenv("POSTGRES_USER", "kabana_user")
DB_PASS = os.getenv("POSTGRES_PASSWORD", "kabana_pass")
DB_NAME = os.getenv("POSTGRES_DB", "kabana_db")
DB_HOST = os.getenv("DB_HOST", "db")  # نام سرویس دیتابیس در داکر

FTP_HOST = os.getenv("FTP_HOST", "ftp")
FTP_PORT = int(os.getenv("FTP_PORT", 21))
FTP_USER = os.getenv("FTP_USER", "ftp_user")
FTP_PASS = os.getenv("FTP_PASS", "ftp_pass")

DEFAULT_BASE_URL = "https://asbook.ir"
DEFAULT_CONCURRENCY = 20

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
        self.semaphore = asyncio.Semaphore(args.concurrency)

        # Queues
        self.page_queue = asyncio.Queue()
        self.details_queue = asyncio.Queue()
        self.db_queue = asyncio.Queue()

        self.db_pool = None

    async def init_db(self):
        """Initialize Postgres Connection Pool"""
        try:
            self.db_pool = await asyncpg.create_pool(
                user=DB_USER, password=DB_PASS, database=DB_NAME, host=DB_HOST
            )
            # اطمینان از وجود جدول (اگر Alembic اجرا نشده باشد)
            # هرچند بهتر است این کار توسط Alembic انجام شود
            async with self.db_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS books (
                        id SERIAL PRIMARY KEY,
                        title VARCHAR,
                        author VARCHAR,
                        publisher VARCHAR,
                        isbn VARCHAR,
                        description TEXT,
                        price FLOAT,
                        folder_name VARCHAR UNIQUE,
                        file_format VARCHAR DEFAULT 'pdf',
                        is_active BOOLEAN DEFAULT TRUE,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    );
                """)
        except Exception as e:
            logger.error(f"Failed to connect to Database: {e}")
            sys.exit(1)

    async def get_ftp_client(self):
        """Create and connect an FTP client"""
        client = aioftp.Client()
        await client.connect(FTP_HOST, FTP_PORT)
        await client.login(FTP_USER, FTP_PASS)
        return client

    def sanitize_filename(self, name):
        """Clean string to be used as folder name"""
        # حذف کاراکترهای غیرمجاز و جایگزینی فاصله با خط تیره
        clean = re.sub(r'[\\/*?:"<>|]', "", name)
        clean = clean.replace(" ", "_").strip()
        return clean

    async def fetch(self, session, url):
        async with self.semaphore:
            try:
                async with session.get(url, timeout=self.args.timeout) as response:
                    if response.status == 200:
                        return await response.read()  # Read as bytes for images
                    else:
                        logger.warning(f"Status {response.status} for {url}")
                        return None
            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
                return None

    async def worker_page_discovery(self, session):
        """Worker to find book links from list pages"""
        while True:
            page_num = await self.page_queue.get()
            url = f"{self.base_url}/page/{page_num}"  # بسته به ساختار سایت مقصد تغییر دهید

            logger.info(f"Scanning Page: {page_num}")
            html_bytes = await self.fetch(session, url)

            if html_bytes:
                soup = BeautifulSoup(html_bytes, "html.parser")
                # --- SELECTOR LOGIC (ADJUST BASED ON TARGET SITE) ---
                # فرض بر این است که لینک کتاب‌ها در تگ a داخل کلاس article یا مشابه است
                links = soup.select("article h2 a")
                if not links:
                    # Fallback selector example
                    links = soup.select(".post-title a")

                for link in links:
                    book_url = link.get("href")
                    if book_url:
                        if not book_url.startswith("http"):
                            book_url = self.base_url + book_url
                        await self.details_queue.put(book_url)

            self.page_queue.task_done()

    async def upload_image_to_ftp(self, session, img_url, folder_name):
        """Download image and stream directly to FTP"""
        if not img_url:
            return False

        image_data = await self.fetch(session, img_url)
        if not image_data:
            return False

        # تشخیص پسوند فایل
        ext = "jpg"
        if img_url.lower().endswith(".png"):
            ext = "png"
        elif img_url.lower().endswith(".webp"):
            ext = "webp"

        filename = f"cover.{ext}"

        ftp_client = None
        try:
            ftp_client = await self.get_ftp_client()

            # ساخت دایرکتوری اگر وجود نداشته باشد
            try:
                await ftp_client.make_directory(folder_name)
            except aioftp.StatusCodeError:
                pass  # احتمالا دایرکتوری وجود دارد

            # تغییر مسیر به دایرکتوری کتاب
            await ftp_client.change_directory(folder_name)

            # آپلود فایل
            async with ftp_client.upload_stream(filename) as stream:
                await stream.write(image_data)

            logger.info(f"Uploaded cover for {folder_name}")
            return True

        except Exception as e:
            logger.error(f"FTP Error for {folder_name}: {e}")
            return False
        finally:
            if ftp_client:
                await ftp_client.quit()

    async def worker_details_extraction(self, session):
        """Worker to parse book details and handle files"""
        while True:
            url = await self.details_queue.get()
            try:
                html_bytes = await self.fetch(session, url)
                if html_bytes:
                    soup = BeautifulSoup(html_bytes, "html.parser")

                    # --- EXTRACTION LOGIC (ADJUST SELECTORS) ---
                    title_tag = soup.select_one("h1.entry-title") or soup.select_one(
                        "h1"
                    )
                    title = title_tag.text.strip() if title_tag else "Unknown"

                    # تولید نام پوشه یکتا
                    folder_name = self.sanitize_filename(title)

                    # استخراج اطلاعات دیگر
                    author = "Unknown"
                    # مثال: پیدا کردن نویسنده
                    auth_tag = soup.find(text=re.compile(r"نویسنده"))
                    if auth_tag and auth_tag.parent:
                        author = auth_tag.parent.text.replace("نویسنده:", "").strip()

                    # استخراج قیمت
                    price = 0.0
                    price_tag = soup.select_one(".price")
                    if price_tag:
                        try:
                            price_text = re.sub(r"[^\d]", "", price_tag.text)
                            price = float(price_text)
                        except:
                            pass

                    description = ""
                    desc_tag = soup.select_one(".entry-content")
                    if desc_tag:
                        description = desc_tag.text.strip()

                    # --- IMAGE PROCESSING ---
                    img_tag = soup.select_one(".post-thumbnail img") or soup.select_one(
                        "article img"
                    )
                    img_src = img_tag["src"] if img_tag else None

                    # دانلود و آپلود عکس به FTP
                    if img_src:
                        await self.upload_image_to_ftp(session, img_src, folder_name)

                    book_data = {
                        "title": title,
                        "author": author,
                        "publisher": "Unknown",  # نیاز به سلکتور دارد
                        "isbn": "",  # نیاز به سلکتور دارد
                        "description": description,
                        "price": price,
                        "folder_name": folder_name,
                        "file_format": "pdf",
                        "is_active": True,
                    }

                    await self.db_queue.put(book_data)

            except Exception as e:
                logger.error(f"Error processing {url}: {e}")
            finally:
                self.details_queue.task_done()

    async def worker_db_saver(self):
        """Worker to save batch data to Postgres"""
        while True:
            book_data = await self.db_queue.get()
            if book_data is None:  # Signal to stop
                break

            try:
                async with self.db_pool.acquire() as conn:
                    # استفاده از upsert (اگر folder_name تکراری بود کاری نکن یا آپدیت کن)
                    await conn.execute(
                        """
                        INSERT INTO books (title, author, publisher, isbn, description, price, folder_name, file_format, is_active)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (folder_name) DO NOTHING
                    """,
                        book_data["title"],
                        book_data["author"],
                        book_data["publisher"],
                        book_data["isbn"],
                        book_data["description"],
                        book_data["price"],
                        book_data["folder_name"],
                        book_data["file_format"],
                        book_data["is_active"],
                    )

                    logger.info(f"Saved to DB: {book_data['title']}")
            except Exception as e:
                logger.error(f"DB Error: {e}")
            finally:
                self.db_queue.task_done()

    async def run(self):
        await self.init_db()

        async with aiohttp.ClientSession(headers=self.headers) as session:
            # 1. Fill Page Queue
            start = self.args.start_page
            end = (
                self.args.end_page if self.args.end_page else start + 5
            )  # Default 5 pages
            for i in range(start, end + 1):
                self.page_queue.put_nowait(i)

            # 2. Start Workers
            tasks = []
            # Page Discovery Workers
            for _ in range(5):
                tasks.append(asyncio.create_task(self.worker_page_discovery(session)))

            # Details & Download Workers
            for _ in range(self.args.workers):
                tasks.append(
                    asyncio.create_task(self.worker_details_extraction(session))
                )

            # DB Saver Worker
            db_task = asyncio.create_task(self.worker_db_saver())

            # 3. Wait for queues to empty
            await self.page_queue.join()
            logger.info("Page discovery finished.")

            await self.details_queue.join()
            logger.info("Details extraction finished.")

            # Stop DB worker
            await self.db_queue.put(None)
            await db_task

            # Cancel other workers
            for t in tasks:
                t.cancel()

        await self.db_pool.close()
        logger.info("Scraping Completed Successfully.")

    @property
    def headers(self):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    parser = argparse.ArgumentParser(description="Kabana Project Scraper")
    parser.add_argument("--url", default=DEFAULT_BASE_URL)
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=30)

    args = parser.parse_args()

    scraper = BookScraper(args)
    asyncio.run(scraper.run())
