from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "merchant.db"
DB_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(DB_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(drop_first: bool = False) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if drop_first:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()