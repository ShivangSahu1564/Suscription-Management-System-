"""Add optional payment references and idempotency keys.

Run manually with:
    python migrations/002_payment_submission_fields.py path\\to\\brightloop.db

This migration is additive, preserves payment rows, and is safe to repeat. It is
intentionally not imported or executed by the Flask application.
"""

import argparse
import sqlite3
from pathlib import Path


INDEX_NAME = "uq_payments_submission_key"


def migrate(database_path):
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "payments" not in tables:
                raise RuntimeError("Database is missing the expected payments table.")

            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(payments)")
            }
            if "transaction_reference" not in columns:
                connection.execute(
                    "ALTER TABLE payments ADD COLUMN transaction_reference VARCHAR(120)"
                )
            if "submission_key" not in columns:
                # NULL is intentional for legacy rows; SQLite unique indexes permit
                # multiple NULL values while enforcing uniqueness for supplied keys.
                connection.execute(
                    "ALTER TABLE payments ADD COLUMN submission_key VARCHAR(64)"
                )

            indexes = {
                row[1]: row for row in connection.execute("PRAGMA index_list(payments)")
            }
            existing = indexes.get(INDEX_NAME)
            if existing is not None:
                indexed_columns = [
                    row[2] for row in connection.execute(
                        f"PRAGMA index_info('{INDEX_NAME}')"
                    )
                ]
                if existing[2] != 1 or indexed_columns != ["submission_key"]:
                    raise RuntimeError(
                        f"Index {INDEX_NAME} exists with an unexpected definition."
                    )
            else:
                connection.execute(
                    f"CREATE UNIQUE INDEX {INDEX_NAME} ON payments (submission_key)"
                )
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to an existing SQLite database file")
    args = parser.parse_args()
    migrate(args.database)
    print(f"Payment schema migration applied to {Path(args.database).resolve()}")


if __name__ == "__main__":
    main()
