"""Локальная база проверок: дубликаты и краткая история."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PastCheck:
    sha256: str
    verdict: str
    profile_id: str | None
    filename: str
    created_at: str


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    expected TEXT,
                    profile_id TEXT,
                    verdict TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_submissions_sha256 ON submissions(sha256)"
            )

    def last_by_hash(self, sha256: str) -> PastCheck | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sha256, verdict, profile_id, filename, created_at
                FROM submissions
                WHERE sha256 = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (sha256,),
            ).fetchone()
        if row is None:
            return None
        return PastCheck(
            sha256=row["sha256"],
            verdict=row["verdict"],
            profile_id=row["profile_id"],
            filename=row["filename"],
            created_at=row["created_at"],
        )

    def record(
        self,
        *,
        sha256: str,
        user_id: int,
        filename: str,
        expected: str | None,
        profile_id: str | None,
        verdict: str,
    ) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO submissions
                    (sha256, user_id, filename, expected, profile_id, verdict, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sha256, user_id, filename, expected, profile_id, verdict, stamp),
            )
