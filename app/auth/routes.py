from urllib.parse import urljoin, urlsplit
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets

from flask import current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user, login_required
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import PasswordResetToken, User
from .email import PasswordResetEmailError, send_password_reset_email
from . import admin_required, bp


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


@bp.route("/admin/staff", methods=["GET", "POST"])
@admin_required
def admin_staff():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not name or not email:
            flash("Name and email are required.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "danger")
        elif not _valid_password(password):
            flash("Password must be more than 8 characters and include uppercase, lowercase, and a special character.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        else:
            staff_user = User(
                name=name, email=email,
                password_hash=generate_password_hash(password), role="staff",
            )
            db.session.add(staff_user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("An account with that email already exists.", "danger")
            else:
                flash("Staff account created successfully.", "success")
                return redirect(url_for("auth.admin_staff"))

    staff_users = User.query.filter_by(role="staff").order_by(User.name, User.email).all()
    return render_template("admin_staff.html", page_title="Staff management", staff_users=staff_users)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


RESET_TOKEN_TTL_MINUTES = 30
GENERIC_RESET_MESSAGE = "If an account exists for this email, a password reset link has been sent."


def _reset_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_reset_token(token):
    if not token:
        return None
    now = _utcnow()
    return PasswordResetToken.query.filter_by(token_hash=_reset_token_hash(token), used_at=None).filter(
        PasswordResetToken.expires_at > now
    ).first()


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = User.query.filter_by(email=email).first() if email else None
        if user is not None:
            now = _utcnow()
            PasswordResetToken.query.filter_by(user_id=user.id, used_at=None).update(
                {PasswordResetToken.used_at: now}, synchronize_session=False
            )
            raw_token = secrets.token_urlsafe(32)
            reset_token = PasswordResetToken(
                user_id=user.id, token_hash=_reset_token_hash(raw_token),
                expires_at=now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES),
            )
            db.session.add(reset_token)
            db.session.commit()
            reset_url = url_for("auth.reset_password", token=raw_token, _external=True)
            try:
                send_password_reset_email(user, reset_url, RESET_TOKEN_TTL_MINUTES)
            except PasswordResetEmailError:
                current_app.logger.warning("Password reset email delivery is unavailable or failed.")
        flash(GENERIC_RESET_MESSAGE, "info")
        return redirect(url_for("auth.forgot_password"))
    return render_template("forgot_password.html", page_title="Forgot password")


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    reset_token = _valid_reset_token(token)
    if reset_token is None:
        flash("This password reset link is invalid or has expired.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not _valid_password(password):
            flash("Password must be more than 8 characters and include uppercase, lowercase, and a special character.", "danger")
        elif password != confirm_password:
            flash("Passwords do not match.", "danger")
        else:
            now = _utcnow()
            consumed = db.session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.id == reset_token.id,
                    PasswordResetToken.used_at.is_(None),
                    PasswordResetToken.expires_at > now,
                )
                .values(used_at=now)
            )
            if consumed.rowcount != 1:
                db.session.rollback()
                flash("This password reset link is invalid or has expired.", "danger")
                return redirect(url_for("auth.forgot_password"))

            user = db.session.get(User, reset_token.user_id)
            if user is None:
                db.session.rollback()
                flash("This password reset link is invalid or has expired.", "danger")
                return redirect(url_for("auth.forgot_password"))
            user.password_hash = generate_password_hash(password)
            db.session.execute(
                update(PasswordResetToken)
                .where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.id != reset_token.id,
                    PasswordResetToken.used_at.is_(None),
                )
                .values(used_at=now)
            )
            db.session.commit()
            flash("Your password has been reset. Please sign in.", "success")
            return redirect(url_for("auth.login"))

    return render_template("reset_password.html", page_title="Reset password")
