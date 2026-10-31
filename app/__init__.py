from pathlib import Path

from flask import Flask, jsonify, render_template
from sqlalchemy import event, inspect, text

from config import DevelopmentConfig
from .extensions import csrf, db, login_manager


def create_app(config_object=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object or DevelopmentConfig)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    csrf.init_app(app)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        with app.app_context():
            engine = db.engine

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    # Import the shared model module once so SQLAlchemy has all tables registered.
    from . import models  # noqa: F401
    from .auth import bp as auth_bp
    from .catalog import bp as catalog_bp
    from .subscriptions import bp as subscriptions_bp
    from .billing import bp as billing_bp
    from .quotations import bp as quotations_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(catalog_bp, url_prefix="/catalog")
    app.register_blueprint(subscriptions_bp, url_prefix="/subscriptions")
    app.register_blueprint(billing_bp, url_prefix="/billing")
    app.register_blueprint(quotations_bp, url_prefix="/quotations")

    @app.get("/health")
    def health_check():
        return jsonify(status="ok", application="Brightloop Subscription Management System")

    @app.get("/")
    def index():
        return render_template("base.html", page_title="Dashboard")

    if app.config.get("AUTO_INIT_DB", False):
        with app.app_context():
            db.create_all()

    # Additive SQLite migration for databases created before subscription plan
    # pricing was snapshotted. Legacy records get a zero snapshot so historical
    # totals are not silently changed.
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        with app.app_context():
            if inspect(db.engine).has_table("subscriptions"):
                columns = {column["name"] for column in inspect(db.engine).get_columns("subscriptions")}
                additions = {
                    "plan_price": "NUMERIC(12, 2) NOT NULL DEFAULT 0.00",
                    "plan_tax_rate": "NUMERIC(7, 4) NOT NULL DEFAULT 0.0000",
                    "plan_quantity": "INTEGER NOT NULL DEFAULT 1",
                    "source_quotation_id": "INTEGER REFERENCES quotations(id) ON DELETE RESTRICT",
                }
                plan_price_added = "plan_price" not in columns
                with db.engine.begin() as connection:
                    for name, definition in additions.items():
                        if name not in columns:
                            connection.execute(text(f"ALTER TABLE subscriptions ADD COLUMN {name} {definition}"))
                    if plan_price_added:
                        # Drafts are still editable, so initialize their new
                        # agreed-price snapshot from the selected plan. Existing
                        # issued/confirmed records remain at zero to preserve
                        # the totals historically recorded for them.
                        connection.execute(text(
                            "UPDATE subscriptions SET plan_price = COALESCE("
                            "(SELECT price FROM plans WHERE plans.id = subscriptions.plan_id), 0) "
                            "WHERE status = 'draft'"
                        ))
                    connection.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_subscriptions_source_quotation "
                        "ON subscriptions (source_quotation_id)"
                    ))
                # create_all may be disabled in production, but this additive
                # lifecycle table still needs to be installed on SQLite.
                with db.engine.begin() as connection:
                    models.SubscriptionRenewal.__table__.create(bind=connection, checkfirst=True)

    return app
