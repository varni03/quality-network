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
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
