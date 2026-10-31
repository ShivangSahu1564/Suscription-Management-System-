<<<<<<< HEAD
# Brightloop Subscription Management System

Shared Flask foundation for a four-member team building a subscription management application.

## Setup (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python run.py
```

Development startup creates `instance/brightloop.db`. Open <http://127.0.0.1:5000>; health check: <http://127.0.0.1:5000/health>.

Set `SECRET_KEY` to a strong environment secret in deployment. The fallback key is for local development only. `DATABASE_URL` overrides the default SQLite URI. Production config disables automatic schema creation; use migrations for deployed schema changes.

`app/models.py` is the single source of shared models. Auth, catalog, subscriptions, and billing each have a separate blueprint.
=======
# Suscription-Management-System-
>>>>>>> d1431a88b5c00abceae7bd2786a36d81c11e9a6e
