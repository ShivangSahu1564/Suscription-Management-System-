"""Add billing period, line-tax snapshot, and product-discount schema.

Run manually with: python migrations/001_billing_invoice_periods.py path\to\brightloop.db
This script is intentionally not imported or run by the Flask application.
"""

import argparse
import sqlite3
from pathlib import Path


def migrate(database_path):
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")

    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            required = {"invoices", "invoice_items", "discounts", "products"}
            missing = required - tables
            if missing:
                raise RuntimeError(f"Database is missing expected tables: {', '.join(sorted(missing))}")

            invoice_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoices)")}
            for name in ("billing_period_start", "billing_period_end"):
                if name not in invoice_columns:
                    connection.execute(f"ALTER TABLE invoices ADD COLUMN {name} DATE")

            item_columns = {row[1] for row in connection.execute("PRAGMA table_info(invoice_items)")}
            if "tax_rate" not in item_columns:
                # Legacy rates are unknown; NULL preserves that fact rather than inventing 0%.
                connection.execute("ALTER TABLE invoice_items ADD COLUMN tax_rate NUMERIC(7, 4)")

            connection.execute(
                """CREATE TABLE IF NOT EXISTS discount_products (
                    discount_id INTEGER NOT NULL REFERENCES discounts(id) ON DELETE CASCADE,
                    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                    PRIMARY KEY (discount_id, product_id)
                )"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_subscription_billing_period
                   ON invoices (subscription_id, billing_period_start, billing_period_end)"""
            )
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", help="Path to an existing SQLite database file")
    args = parser.parse_args()
    migrate(args.database)
    print(f"Billing schema migration applied to {Path(args.database).resolve()}")


if __name__ == "__main__":
    main()
