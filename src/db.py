"""Application history store.

Uses PostgreSQL (``DATABASE_URL``) when configured - that is what the GitHub
Actions schedule uses - and falls back to a local SQLite file otherwise so the
bot, the self-check and the tests all work offline.

Rows record every evaluated job (applied and skipped) which is what makes the
run idempotent: a job is never applied to twice.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from .logging_setup import get_logger

logger = get_logger("db")

DEFAULT_SQLITE_PATH = Path(os.getenv("SQLITE_PATH", "data/jobs.db"))
RETRYABLE_REASONS = ("UNANSWERED_QUESTION", "NO_APPLY_BUTTON")

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS job_applications (
    job_id VARCHAR(64) PRIMARY KEY,
    company_name VARCHAR(255) NOT NULL,
    job_title VARCHAR(255) NOT NULL,
    status VARCHAR(50) NOT NULL,
    skip_reason VARCHAR(100),
    naukri_url TEXT,
    external_url TEXT,
    unanswered_question TEXT,
    match_score INTEGER,
    location VARCHAR(255),
    source_query VARCHAR(255),
    retry_count INTEGER DEFAULT 0,
    applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
)
"""

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_job_status ON job_applications(status)",
    "CREATE INDEX IF NOT EXISTS idx_job_applied_at ON job_applications(applied_at)",
)

EXTRA_COLUMNS = {
    "match_score": "INTEGER",
    "location": "VARCHAR(255)",
    "source_query": "VARCHAR(255)",
    "retry_count": "INTEGER DEFAULT 0",
}


class JobDatabase:
    """Thin, backend-agnostic persistence layer for job evaluations."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.environ.get("DATABASE_URL") or f"sqlite:///{DEFAULT_SQLITE_PATH}"
        self.backend = "sqlite" if self.db_url.startswith("sqlite") else "postgres"
        self.conn = None
        self._connect()
        self._init_table()

    # -------------------------------------------------------------- connect
    @property
    def placeholder(self) -> str:
        return "?" if self.backend == "sqlite" else "%s"

    def _connect(self) -> None:
        if self.backend == "sqlite":
            raw_path = self.db_url.replace("sqlite:///", "", 1).replace("sqlite://", "", 1)
            path = Path(raw_path or DEFAULT_SQLITE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            logger.info("Using SQLite application store at %s", path)
        else:
            import psycopg2  # imported lazily so SQLite users need no driver

            self.conn = psycopg2.connect(self.db_url, connect_timeout=15)
            logger.info("Connected to PostgreSQL application store at %s", self._safe_target())

    def _safe_target(self) -> str:
        try:
            parsed = urlparse(self.db_url)
            host = parsed.hostname or "localhost"
            database = (parsed.path or "/").lstrip("/")
            return f"{host}:{parsed.port or 5432}/{database}"
        except Exception:
            return "configured database"

    def _ensure_connection(self) -> None:
        if self.conn is None:
            self._connect()
            return
        if self.backend == "sqlite":
            return
        if getattr(self.conn, "closed", 0):
            logger.warning("Database connection was closed; reconnecting.")
            self._connect()

    @contextmanager
    def _cursor(self):
        self._ensure_connection()
        if self.backend == "postgres":
            from psycopg2.extras import RealDictCursor

            cur = self.conn.cursor(cursor_factory=RealDictCursor)
        else:
            cur = self.conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    def _as_dict(self, row) -> Dict[str, Any]:
        if row is None:
            return {}
        return dict(row) if self.backend == "postgres" else {k: row[k] for k in row.keys()}

    # ---------------------------------------------------------------- setup
    def _init_table(self) -> None:
        with self._cursor() as cur:
            cur.execute(TABLE_SQL)
            for statement in INDEX_SQL:
                cur.execute(statement)
            self._migrate(cur)
            self.conn.commit()
        logger.debug("Database schema ready (%s backend).", self.backend)

    def _migrate(self, cur) -> None:
        if self.backend == "sqlite":
            cur.execute("PRAGMA table_info(job_applications)")
            existing = {row[1] for row in cur.fetchall()}
            for column, ddl in EXTRA_COLUMNS.items():
                if column not in existing:
                    cur.execute(f"ALTER TABLE job_applications ADD COLUMN {column} {ddl}")
                    logger.info("Migrated SQLite schema: added column %s", column)
            return
        for column, ddl in EXTRA_COLUMNS.items():
            cur.execute(
                f"ALTER TABLE job_applications ADD COLUMN IF NOT EXISTS {column} {ddl}"
            )

    # ---------------------------------------------------------------- reads
    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                f"SELECT * FROM job_applications WHERE job_id = {self.placeholder}",
                (job_id,),
            )
            row = cur.fetchone()
            return self._as_dict(row) if row is not None else None

    def is_job_processed(
        self,
        job_id: str,
        retry_unanswered: bool = True,
        max_retries: int = 2,
    ) -> bool:
        """True when the job should be skipped (already seen / max retries hit)."""
        row = self.get_job(job_id)
        if row is None:
            return False
        if retry_unanswered and row.get("skip_reason") in RETRYABLE_REASONS:
            retries = int(row.get("retry_count") or 0)
            if retries < max_retries:
                logger.debug("Retrying previously skipped job %s (attempt %d)", job_id, retries + 1)
                return False
        return True

    def stats(self) -> Dict[str, int]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT status, COUNT(*) AS total FROM job_applications GROUP BY status"
            )
            rows = cur.fetchall()
        return {str(row["status"]).lower(): int(row["total"]) for row in rows}

    def recent(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT job_id, company_name, job_title, status, skip_reason, applied_at "
                f"FROM job_applications ORDER BY applied_at DESC LIMIT {self.placeholder}",
                (limit,),
            )
            rows = cur.fetchall()
        return [self._as_dict(row) for row in rows]

    def unanswered_questions(self) -> List[str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT unanswered_question FROM job_applications "
                "WHERE unanswered_question IS NOT NULL AND unanswered_question <> ''"
            )
            rows = cur.fetchall()
        return [str(row["unanswered_question"]) for row in rows]

    # --------------------------------------------------------------- writes
    def record_job(
        self,
        job_id: str,
        company: str,
        title: str,
        status: str,
        skip_reason: Optional[str] = None,
        naukri_url: Optional[str] = None,
        external_url: Optional[str] = None,
        unanswered_question: Optional[str] = None,
        match_score: Optional[int] = None,
        location: Optional[str] = None,
        source_query: Optional[str] = None,
    ) -> None:
        """Insert or update a job. Re-recording increments ``retry_count``."""
        values = (
            job_id,
            (company or "Unknown Company")[:255],
            (title or "Unknown Role")[:255],
            status,
            skip_reason,
            naukri_url,
            external_url,
            unanswered_question,
            match_score,
            (location or "")[:255] or None,
            (source_query or "")[:255] or None,
        )
        placeholders = ", ".join(self.placeholder for _ in values)
        sql = f"""
            INSERT INTO job_applications (
                job_id, company_name, job_title, status, skip_reason,
                naukri_url, external_url, unanswered_question, match_score,
                location, source_query
            ) VALUES ({placeholders})
            ON CONFLICT (job_id) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                job_title = EXCLUDED.job_title,
                status = EXCLUDED.status,
                skip_reason = EXCLUDED.skip_reason,
                naukri_url = EXCLUDED.naukri_url,
                external_url = EXCLUDED.external_url,
                unanswered_question = EXCLUDED.unanswered_question,
                match_score = EXCLUDED.match_score,
                location = EXCLUDED.location,
                source_query = EXCLUDED.source_query,
                retry_count = job_applications.retry_count + 1,
                applied_at = CURRENT_TIMESTAMP
        """
        with self._cursor() as cur:
            cur.execute(sql, values)
            self.conn.commit()

    # --------------------------------------------------------------- misc
    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.close()
        finally:
            self.conn = None

    def __enter__(self) -> "JobDatabase":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
