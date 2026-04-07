"""
One-shot migration script — run once after deploying the stage_models / extended_thinking changes.

Usage:
    python -m web.backend.migrate

Safe to run multiple times: ADD COLUMN is skipped if the column already exists.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "web.db"


def _add_column_if_missing(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        print(f"  Added column: {table}.{column}")
    else:
        print(f"  Already exists (skipped): {table}.{column}")


def main() -> None:
    print(f"Migrating {DB_PATH} ...")
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    _add_column_if_missing(cur, "run", "stage_models_json",     "TEXT")
    _add_column_if_missing(cur, "run", "extended_thinking",     "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(cur, "run", "auto_mode",             "INTEGER NOT NULL DEFAULT 1")
    _add_column_if_missing(cur, "run", "pause_point",           "TEXT")
    _add_column_if_missing(cur, "run", "pathway_choices_json",  "TEXT")
    _add_column_if_missing(cur, "run", "structure_next_step",   "TEXT")
    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
