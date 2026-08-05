from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Runtime: "dev" | "prod"
    ENVIRONMENT: str = "dev"

    # Database
    DATABASE_URL: str
    SYNC_DATABASE_URL: str | None = None

    # Security — must be set explicitly in production
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days
    # None = derive from BASE_URL (https → Secure cookies)
    COOKIE_SECURE: bool | None = None

    # File Storage
    MEDIA_ROOT: str = "/app/storage"

    # Internal FTP (backend only; clients use /media/proxy/*)
    FTP_ENABLED: bool = True
    FTP_HOST: str = "ftp"
    FTP_PORT: int = 21
    FTP_USER: str = "ftp_user"
    FTP_PASS: str = "ftp_pass"
    FTP_BASE_DIR: str = ""

    # Scraper
    SCRAPER_CONCURRENCY: int = 20
    SCRAPER_LOOP_INTERVAL: int = 3600
    # How many books to buffer before writing to Postgres (also flushed on timeout)
    SCRAPER_FLUSH_SIZE: int = 25
    SCRAPER_FLUSH_TIMEOUT: float = 5.0

    # First admin bootstrap (created on boot if no admin exists)
    ADMIN_PHONE: str = ""
    ADMIN_PASSWORD: str = ""
    ADMIN_FIRST_NAME: str = "مدیر"
    ADMIN_LAST_NAME: str = "سیستم"
    ADMIN_EMAIL: str = ""

    # Frontend
    DOMAIN_NAME: str = "kabana.local"
    KABANA_LICENSE: str = ""
    BASE_URL: str = "http://localhost:8000"

    # Zibal (merchant=zibal is official sandbox)
    ZIBAL_MERCHANT: str = "zibal"
    ZIBAL_CALLBACK_URL: str = "http://localhost:8000/payment/callback/zibal"

    # Torob Pay (credit / installment CPG) — register only when ENABLED + credentials set
    TOROBPAY_ENABLED: bool = False
    TOROBPAY_API_URL: str = "https://cpg.torobpay.com"
    TOROBPAY_CLIENT_ID: str = ""
    TOROBPAY_CLIENT_SECRET: str = ""
    TOROBPAY_USERNAME: str = ""
    TOROBPAY_PASSWORD: str = ""

    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_present(cls, v: str) -> str:
        key = (v or "").strip()
        if not key:
            raise ValueError("SECRET_KEY is required")
        return key

    def model_post_init(self, __context) -> None:
        if not self.is_production:
            return
        key = self.SECRET_KEY.strip()
        weak = {
            "secret",
            "change_this_to_a_secure_random_string",
            "change_this_to_a_very_secure_random_string_in_production",
            "change_me",
        }
        if key in weak or len(key) < 32:
            raise ValueError(
                "In production, SECRET_KEY must be a strong random string "
                "(at least 32 characters). Generate with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"prod", "production"}


settings = Settings()
