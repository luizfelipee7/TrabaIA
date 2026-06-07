from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


BASE_DIR = Path(__file__).resolve().parents[1]
DATABASE_URL = f"sqlite:///{BASE_DIR / 'clinic_inventory.db'}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_db_and_tables() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
