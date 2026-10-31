# Billing schema migration

The billing service requires invoice billing-period columns, invoice-line tax-rate snapshots, the discount-product association table, and a unique invoice-period index.

**Back up the SQLite database first.** Stop the application, then copy the configured database file (by default `instance/brightloop.db`) to a separate backup location. For example, in PowerShell:

```powershell
Copy-Item .\instance\brightloop.db .\instance\brightloop.db.backup
```

From the project root, run the migration explicitly against the existing database:

```powershell
python .\migrations\001_billing_invoice_periods.py .\instance\brightloop.db
```

The script does not run automatically. It adds missing columns and tables without dropping or resetting tables, and is safe to run again. Existing invoice period and tax snapshot values remain `NULL` because the prior schema does not contain enough information to reconstruct them reliably. The billing service refuses to generate another invoice for a subscription that has a legacy invoice without a period until that history is reconciled.

If `DATABASE_URL` points elsewhere, pass the path to that SQLite file instead. This script accepts a filesystem path, not a SQLAlchemy URI.
