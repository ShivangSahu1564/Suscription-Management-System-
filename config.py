import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'instance' / 'brightloop.db').as_posix()}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").strip().lower() in {"1", "true", "yes", "on"}
    MAIL_FROM = os.environ.get("MAIL_FROM")


class DevelopmentConfig(Config):
    DEBUG = True
    AUTO_INIT_DB = True


class ProductionConfig(Config):
    DEBUG = False
    AUTO_INIT_DB = False
