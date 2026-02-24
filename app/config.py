from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str
    SYNC_DATABASE_URL: str | None = None

    # Security
    SECRET_KEY: str = "secret"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # File Storage
    MEDIA_ROOT: str = "/app/storage"

    # Internal FTP (Service Connection)
    FTP_ENABLED: bool = True
    FTP_HOST: str = "ftp"
    FTP_PORT: int = 21
    FTP_USER: str = "ftp_user"
    FTP_PASS: str = "ftp_pass"

    # Public Link Generation (For Scraper)
    LINK_BASE_PROTOCOL: str = "ftp"
    LINK_BASE_HOST: str = "localhost"
    LINK_BASE_PORT: int = 21
    LINK_BASE_USER: str | None = "ftp_user"
    LINK_BASE_PASS: str | None = "ftp_pass"

    # Scraper
    SCRAPER_CONCURRENCY: int = 20

    # Frontend
    DOMAIN_NAME: str = "kabana.local"

    class Config:
        env_file = ".env"
        extra = "ignore"

    def get_public_link(self, remote_path: str) -> str:
        """
        Helper method to generate download links based on Env settings
        """
        auth_part = ""
        if self.LINK_BASE_USER and self.LINK_BASE_PASS:
            auth_part = f"{self.LINK_BASE_USER}:{self.LINK_BASE_PASS}@"

        port_part = ""
        if self.LINK_BASE_PORT != 21:
            port_part = f":{self.LINK_BASE_PORT}"

        # Remove leading slash if present
        clean_path = remote_path.lstrip("/")

        return f"{self.LINK_BASE_PROTOCOL}://{auth_part}{self.LINK_BASE_HOST}{port_part}/{clean_path}"


settings = Settings()
