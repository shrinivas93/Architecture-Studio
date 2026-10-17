"""Faker integration: fill a SQLite database with realistic per-service test data."""

from __future__ import annotations

import os
import sqlite3

from faker import Faker

from model import Service, SystemDesign

SCHEMAS = {
    "db": "(id INTEGER PRIMARY KEY, name TEXT, email TEXT, city TEXT, signup_date TEXT)",
    "api": "(id INTEGER PRIMARY KEY, request_id TEXT, ip_address TEXT, user_agent TEXT, status_code INTEGER)",
    "queue": "(id INTEGER PRIMARY KEY, message_id TEXT, event_type TEXT, enqueued_at TEXT)",
    "worker": "(id INTEGER PRIMARY KEY, job_id TEXT, payload TEXT, duration_ms INTEGER)",
    "cache": "(id INTEGER PRIMARY KEY, job_id TEXT, payload TEXT, duration_ms INTEGER)",
}

HTTP_STATUS_CODES = [200, 201, 204, 301, 400, 401, 403, 404, 429, 500, 503]


def table_name(service_name: str) -> str:
    """SQLite table name for a service: ``svc_<name>`` with ``-`` replaced by ``_``."""
    return "svc_" + service_name.replace("-", "_")


def _row(kind: str, fake: Faker, numeric: Faker) -> tuple:
    if kind == "db":
        return (
            fake.name(),
            fake.email(),
            fake.city(),
            fake.date_time_this_year().isoformat(),
        )
    if kind == "api":
        return (
            fake.uuid4(),
            fake.ipv4(),
            fake.user_agent(),
            HTTP_STATUS_CODES[numeric.random_int(0, len(HTTP_STATUS_CODES) - 1)],
        )
    if kind == "queue":
        return (
            fake.uuid4(),
            f"{fake.word()}.{fake.word()}",
            fake.date_time_this_year().isoformat(),
        )
    # worker or cache
    return (
        fake.uuid4(),
        fake.sentence(),
        numeric.random_int(1, 5000),
    )


def _columns(kind: str) -> list[str]:
    schema = SCHEMAS[kind].strip("()")
    return [part.strip().split()[0] for part in schema.split(",")][1:]  # skip id


def _fill_table(conn: sqlite3.Connection, service: Service, rows: int, fake: Faker, numeric: Faker) -> None:
    tbl = table_name(service.name)
    cols = _columns(service.kind)
    conn.execute(f'DROP TABLE IF EXISTS "{tbl}"')
    conn.execute(f'CREATE TABLE "{tbl}" {SCHEMAS[service.kind]}')
    placeholders = ", ".join("?" for _ in cols)
    conn.executemany(
        f'INSERT INTO "{tbl}" ({", ".join(cols)}) VALUES ({placeholders})',
        [_row(service.kind, fake, numeric) for _ in range(rows)],
    )


def generate_test_data(
    design: SystemDesign,
    db_path: str,
    rows_per_service: int = 50,
    locale: str = "en_IN",
) -> str:
    """Create one ``svc_*`` table per service in ``db_path`` and return the path."""
    rows_per_service = int(rows_per_service)
    if rows_per_service < 0:
        raise ValueError("rows_per_service must be >= 0")
    directory = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(directory, exist_ok=True)

    fake = Faker(locale)
    numeric = Faker()
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            for service in design.services:
                _fill_table(conn, service, rows_per_service, fake, numeric)
    finally:
        conn.close()
    return db_path


def list_tables(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'svc\\_%' ESCAPE '\\' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def preview_table(db_path: str, table: str, limit: int = 5) -> tuple[list[str], list[tuple]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" LIMIT ?', (limit,))
        headers = [d[0] for d in cur.description]
        return headers, cur.fetchall()
    finally:
        conn.close()
