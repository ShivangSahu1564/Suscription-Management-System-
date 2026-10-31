from pathlib import Path

from flask import Flask, jsonify, render_template
from sqlalchemy import event

from config import DevelopmentConfig
from .extensions import db, login_manager


def create_app(config_object=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object or DevelopmentConfig)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
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

    return app
