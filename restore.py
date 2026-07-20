"""
RESTORE
=======
Restores creature_template, quest_template, item_template, and creature
(only relevant if you ran --include-clones for creatures) to the state
captured in the EARLIEST backup table executor.py created for each - i.e.
genuinely back to how things were before this tool ever touched them, not
just undoing the most recent run.

Usage:
  python restore.py                        # dry run - shows what it found and what it WOULD do
  python restore.py --list                  # just list every backup table found, no restore
  python restore.py --apply                  # actually restore (asks for typed confirmation)
  python restore.py --apply --only creature_template,quest_template   # restore only these tables
  python restore.py --apply --to creature_template=20260710_120000     # restore to a SPECIFIC
                                                                          # backup instead of earliest

Safety:
  - All table swaps happen in a single RENAME TABLE statement, which MySQL/
    MariaDB executes atomically (all pairs succeed or none do).
  - Your current (modified) tables are never dropped - they're renamed to
    <table>_pre_restore_<timestamp> so you can double check before deleting
    them yourself.
  - The idempotency state tables (level_scaler_*_state) are cleared as part
    of a restore, since after restoring, that content is no longer "already
    processed" and should be eligible for brain.py again.
"""
import argparse
import re
from collections import defaultdict

from config import CREATURE_STATE_TABLE, QUEST_STATE_TABLE, ITEM_STATE_TABLE
from db import get_connection, table_exists

RESTORABLE_TABLES = ["creature_template", "quest_template", "item_template", "creature"]
STATE_TABLES = [CREATURE_STATE_TABLE, QUEST_STATE_TABLE, ITEM_STATE_TABLE]

BACKUP_RE = re.compile(r"^(?P<table>.+)_backup_(?P<ts>\d{8}_\d{6})$")


def find_backups(conn):
    """Returns {source_table: [(timestamp_str, backup_table_name), ...]}, oldest first."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE '%\\_backup\\_%'"
        )
        rows = cur.fetchall()

    backups = defaultdict(list)
    for row in rows:
        name = row["TABLE_NAME"]
        m = BACKUP_RE.match(name)
        if m and m.group("table") in RESTORABLE_TABLES:
            backups[m.group("table")].append((m.group("ts"), name))

    for table in backups:
        backups[table].sort()  # zero-padded YYYYMMDD_HHMMSS sorts correctly as plain strings
    return backups


def build_plan(backups, only=None, to_overrides=None):
    to_overrides = to_overrides or {}
    plan = []
    for table, versions in backups.items():
        if only and table not in only:
            continue
        if table in to_overrides:
            target_ts = to_overrides[table]
            match = next((n for ts, n in versions if ts == target_ts), None)
            if not match:
                print(f"[ERROR] No backup found for {table} at timestamp {target_ts}, skipping.")
                continue
            plan.append((table, match, target_ts))
        else:
            ts, name = versions[0]  # earliest = pristine original
            plan.append((table, name, ts))
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually restore (default is dry-run/preview)")
    parser.add_argument("--list", action="store_true", help="Just list found backups and exit")
    parser.add_argument("--only", help="Comma-separated table names to restore (default: all found)")
    parser.add_argument("--to", action="append", default=[],
                         help="table=timestamp to restore a specific table to a specific backup "
                              "instead of the earliest one. Repeatable.")
    args = parser.parse_args()

    only = set(args.only.split(",")) if args.only else None
    to_overrides = {}
    for item in args.to:
        table, _, ts = item.partition("=")
        to_overrides[table] = ts

    with get_connection(autocommit=True) as conn:  # RENAME/DROP are DDL - always auto-commit anyway
        backups = find_backups(conn)

        if not backups:
            print("No backup tables found - nothing for restore.py to work with yet "
                  "(executor.py only creates these on an --apply run).")
            return

        print("Backup tables found:")
        for table, versions in backups.items():
            print(f"  {table}:")
            for ts, name in versions:
                marker = " <- EARLIEST (used by default)" if ts == versions[0][0] else ""
                print(f"    {ts}  ({name}){marker}")

        if args.list:
            return

        plan = build_plan(backups, only, to_overrides)
        if not plan:
            print("\nNothing to restore (check your --only / --to filters).")
            return

        print("\nPlanned restore:")
        for table, backup_name, ts in plan:
            print(f"  {table}  <-  {backup_name}  (captured {ts})")
        print(f"\nState tables that will be cleared (so this content is eligible for brain.py again): "
              f"{', '.join(STATE_TABLES)}")

        if not args.apply:
            print("\nDry run - nothing changed. Pass --apply to actually restore.")
            return

        confirm = input("\nType YES to confirm you want to overwrite the current tables with these backups: ")
        if confirm != "YES":
            print("Aborted.")
            return

        import time
        safety_ts = time.strftime("%Y%m%d_%H%M%S")
        rename_pairs = []
        for table, backup_name, ts in plan:
            safety_name = f"{table}_pre_restore_{safety_ts}"
            rename_pairs.append(f"`{table}` TO `{safety_name}`")
            rename_pairs.append(f"`{backup_name}` TO `{table}`")

        rename_stmt = "RENAME TABLE " + ", ".join(rename_pairs)
        print(f"\n[RESTORE] {rename_stmt}")
        with conn.cursor() as cur:
            cur.execute(rename_stmt)

        for state_table in STATE_TABLES:
            if table_exists(conn, state_table):
                print(f"[RESTORE] Clearing {state_table}")
                with conn.cursor() as cur:
                    cur.execute(f"DROP TABLE `{state_table}`")

        print("\nRESTORED. Your previous (modified) tables were kept as "
              f"<table>_pre_restore_{safety_ts} - verify everything looks right, then drop those "
              "yourself once you're confident you don't need them.")


if __name__ == "__main__":
    main()
