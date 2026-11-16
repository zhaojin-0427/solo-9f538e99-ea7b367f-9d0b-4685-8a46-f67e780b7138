"""SQLite persistence layer.

The database stores metadata plus the raw, immutable bytes of every accepted
revision (XBRL instances, XSD schemas, calculation linkbases) and every report,
so restarting the container loses nothing.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS packages (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    latest_rev  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id    TEXT NOT NULL REFERENCES packages(id),
    revision       INTEGER NOT NULL,
    xsd_raw        BLOB NOT NULL,
    calc_raw       BLOB NOT NULL,
    target_ns      TEXT NOT NULL,
    prefix         TEXT NOT NULL,
    concepts_json  TEXT NOT NULL,
    equations_json TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(package_id, revision)
);

CREATE TABLE IF NOT EXISTS instances (
    id          TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL REFERENCES packages(id),
    latest_rev  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_revisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id  TEXT NOT NULL REFERENCES instances(id),
    revision     INTEGER NOT NULL,
    instance_raw BLOB NOT NULL,
    entity       TEXT NOT NULL,
    period       TEXT NOT NULL,
    currency     TEXT NOT NULL,
    facts_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(instance_id, revision)
);

CREATE TABLE IF NOT EXISTS reports (
    id                  TEXT PRIMARY KEY,
    instance_id         TEXT NOT NULL,
    package_id          TEXT NOT NULL,
    instance_revision   INTEGER NOT NULL,
    package_revision    INTEGER NOT NULL,
    status              TEXT NOT NULL,            -- feasible | infeasible | pending
    is_correction       INTEGER NOT NULL,         -- 1 when an earlier report existed
    superseded          INTEGER NOT NULL DEFAULT 0,
    late                INTEGER NOT NULL DEFAULT 0,
    witness_json        TEXT,
    conflicts_json      TEXT,
    missing_json        TEXT,
    detail_json         TEXT NOT NULL,
    instance_snapshot   BLOB NOT NULL,
    xsd_snapshot        BLOB NOT NULL,
    calc_snapshot       BLOB NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_instance ON reports(instance_id, created_at);

CREATE TABLE IF NOT EXISTS intakes (
    id                    TEXT PRIMARY KEY,
    package_id            TEXT NOT NULL REFERENCES packages(id),
    package_revision      INTEGER NOT NULL,          -- package revision seen by the client at upload
    status                TEXT NOT NULL,             -- pending | accepted
    instance_raw          BLOB NOT NULL,             -- original, never overwritten bytes
    entity                TEXT NOT NULL,
    period                TEXT NOT NULL,
    currency              TEXT NOT NULL,
    facts_json            TEXT NOT NULL,
    unknown_concepts_json TEXT NOT NULL,
    last_revalidate_json  TEXT,                      -- latest revalidation attempt (any outcome)
    instance_id           TEXT REFERENCES instances(id),  -- set once, only on acceptance
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intakes_package ON intakes(package_id, created_at);
CREATE INDEX IF NOT EXISTS idx_intakes_status ON intakes(status);
"""


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA)
            _conn = conn
        return _conn


def init_db() -> None:
    get_conn()


def reset_conn() -> None:
    """Drop the cached connection (used by tests that swap the DB path)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
