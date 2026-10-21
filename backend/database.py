"""Database configuration.

Uses Postgres when DATABASE_URL is set (production / deploy), otherwise
falls back to a local SQLite file so the app runs with zero setup in VS Code.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

# DATABASE_URL example (Postgres): postgresql+psycopg2://user:pass@host:5432/dbname
# Serverless hosts (Vercel) have a read-only project folder; only /tmp is writable.
_DEFAULT_DB = "sqlite:////tmp/uveye.db" if os.getenv("VERCEL") else "sqlite:///./uveye.db"
DATABASE_URL = os.getenv("DATABASE_URL", _DEFAULT_DB)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
# FastAPI runs sync endpoints on a 40-thread pool. The SQLAlchemy default (5 + 10 connections)
# is smaller than that, so under load requests queue for a connection and then fail with
# "QueuePool limit reached". Size the pool to cover the worker threads.
POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "30"))
POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "10"))
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True,
                       pool_size=POOL_SIZE, max_overflow=MAX_OVERFLOW, pool_timeout=POOL_TIMEOUT)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
