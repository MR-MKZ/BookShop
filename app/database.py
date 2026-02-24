from typing import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

# Sync Database (for Alembic and sync tasks)
engine = None
SessionLocal = None
if settings.SYNC_DATABASE_URL:
    engine = create_engine(settings.SYNC_DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Async Database (for FastAPI app)
async_engine = create_async_engine(settings.DATABASE_URL, echo=False, future=True)
AsyncSessionLocal = sessionmaker(
    async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)

Base = declarative_base()


def get_db() -> Generator:
    """Dependency for synchronous database session"""
    if not SessionLocal:
        raise RuntimeError("SYNC_DATABASE_URL is not configured.")

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency for asynchronous database session"""
    async with AsyncSessionLocal() as session:
        yield session
