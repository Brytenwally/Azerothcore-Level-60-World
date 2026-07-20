"""
Connection + schema-introspection helpers.

AzerothCore's column names have drifted slightly across revisions
(e.g. `minlevel` vs `MinLevel`, `id` vs `id1`, `RewardXP` vs `RewXP`).
Rather than hardcode one revision's names and break silently on another,
every column we touch is resolved against INFORMATION_SCHEMA first.
"""
import contextlib
import sys

import pymysql
import pymysql.cursors

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME


@contextlib.contextmanager
def get_connection(autocommit=False):
    try:
        conn = pymysql.connect(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=autocommit,
            charset="utf8mb4",
        )
    except pymysql.err.OperationalError as e:
        print(f"[FATAL] Could not connect to {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}: {e}",
              file=sys.stderr)
        sys.exit(1)
    try:
        yield conn
    finally:
        conn.close()


def table_columns(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (DB_NAME, table_name),
        )
        return {row["COLUMN_NAME"] for row in cur.fetchall()}


def table_exists(conn, table_name):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
            (DB_NAME, table_name),
        )
        return cur.fetchone()["n"] > 0


def resolve_column(available_columns, candidates, required=True, context=""):  # noqa: reused across modules
    """Return the first candidate name that actually exists in the table."""
    for c in candidates:
        if c in available_columns:
            return c
    if required:
        raise RuntimeError(
            f"None of the expected columns {candidates} found{' for ' + context if context else ''}. "
            f"Available columns: {sorted(available_columns)}"
        )
    return None


def ensure_state_table(conn, table_name):
    """Creates a small tracking table (if missing) recording which entries
    this tool has already modified, so re-running brain.py doesn't re-shift
    already-shifted rows."""
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS `{table_name}` ("
            f"entry_id BIGINT NOT NULL PRIMARY KEY, "
            f"detail VARCHAR(255) NULL, "
            f"applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            f") ENGINE=InnoDB"
        )


def get_processed_ids(conn, table_name):
    """Returns the set of entry_ids already recorded in a state table. Empty
    set (not an error) if the table doesn't exist yet."""
    if not table_exists(conn, table_name):
        return set()
    with conn.cursor() as cur:
        cur.execute(f"SELECT entry_id FROM `{table_name}`")
        return {row["entry_id"] for row in cur.fetchall()}


def mark_processed(conn, table_name, entry_id, detail=""):
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO `{table_name}` (entry_id, detail) VALUES (%s, %s) "
            f"ON DUPLICATE KEY UPDATE detail = VALUES(detail), applied_at = CURRENT_TIMESTAMP",
            (entry_id, detail[:255] if detail else None),
        )
