"""Add discount minimum-purchase and active fields without replacing rows.

Run manually with:
    python migrations/003_discount_eligibility_fields.py path\\to\\brightloop.db

This additive migration is not executed by the application.
"""

import argparse
import sqlite3
from pathlib import Path


INDEX_NAME = "ix_discounts_is_active"


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
            if "discounts" not in tables:
                raise RuntimeError("Database is missing the expected discounts table.")

            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(discounts)")
            }
            if "minimum_purchase_amount" not in columns:
                connection.execute(
                    "ALTER TABLE discounts ADD COLUMN minimum_purchase_amount "
                    "NUMERIC(12, 2) NOT NULL DEFAULT 0.00 "
                    "CHECK (minimum_purchase_amount >= 0)"
                )
            if "is_active" not in columns:
                connection.execute(
                    "ALTER TABLE discounts ADD COLUMN is_active BOOLEAN NOT NULL "
                    "DEFAULT 1 CHECK (is_active IN (0, 1))"
                )

            indexes = {
                row[1]: row for row in connection.execute("PRAGMA index_list(discounts)")
            }
            existing = indexes.get(INDEX_NAME)
            if existing is not None:
                indexed_columns = [
                    row[2] for row in connection.execute(
                        f"PRAGMA index_info('{INDEX_NAME}')"
                    )
                ]
                if indexed_columns != ["is_active"]:
                    raise RuntimeError(f"Index {INDEX_NAME} exists with an unexpected definition.")
            else:
                connection.execute(
                    f"CREATE INDEX {INDEX_NAME} ON discounts (is_active)"
                )
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to an existing SQLite database file")
    args = parser.parse_args()
    migrate(args.database)
    print(f"Discount eligibility migration applied to {Path(args.database).resolve()}")


if __name__ == "__main__":
    main()
