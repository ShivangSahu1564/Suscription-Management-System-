from urllib.parse import urljoin, urlsplit
import re

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user, login_required
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import User
from . import bp


def _safe_next_url(target):
    if not target:
        return None
    ref = urlsplit(request.host_url)
    test = urlsplit(urljoin(request.host_url, target))
    if test.scheme in ("http", "https") and ref.netloc == test.netloc:
        return target
    return None


def _valid_password(password):
    return (
        len(password) > 8
        and re.search(r"[A-Z]", password)
        and re.search(r"[a-z]", password)
        and re.search(r"[^A-Za-z0-9]", password)
    )


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash("Name, email, and password are required.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
        elif not _valid_password(password):
            flash("Password must be more than 8 characters and include uppercase, lowercase, and a special character.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        else:
            user = User(name=name, email=email, password_hash=generate_password_hash(password), role="customer")
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("An account with that email already exists.", "danger")
            else:
                flash("Registration successful. Please sign in.", "success")
                return redirect(url_for("auth.login"))
    return render_template("register.html", page_title="Create account")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    next_url = _safe_next_url(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first() if email else None
        if user is None or not check_password_hash(user.password_hash, password):
            flash("Invalid email or password.", "danger")
        else:
            login_user(user)
            return redirect(next_url or url_for("index"))
    return render_template("login.html", page_title="Sign in", next_url=next_url)


@bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
