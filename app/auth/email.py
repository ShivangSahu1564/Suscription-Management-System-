import smtplib
from email.message import EmailMessage

from flask import current_app


class PasswordResetEmailError(Exception):
    """Raised when reset email delivery cannot be configured or completed."""


def send_password_reset_email(user, reset_url, expires_minutes=30):
    config = current_app.config
    host = config.get("SMTP_HOST")
    sender = config.get("MAIL_FROM")
    if not host or not sender:
        raise PasswordResetEmailError("SMTP_HOST and MAIL_FROM must be configured to send email.")

    message = EmailMessage()
    message["Subject"] = "Reset your Brightloop password"
    message["From"] = sender
    message["To"] = user.email
    message.set_content(
        f"Hello {user.name},\n\n"
        "We received a request to reset your Brightloop password. "
        f"Use this link within {expires_minutes} minutes:\n\n"
        f"{reset_url}\n\n"
        "If you did not request a password reset, you can ignore this email.\n"
    )

    try:
        with smtplib.SMTP(host, config.get("SMTP_PORT", 587), timeout=10) as smtp:
            if config.get("SMTP_USE_TLS", True):
                smtp.starttls()
            username = config.get("SMTP_USERNAME")
            password = config.get("SMTP_PASSWORD")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise PasswordResetEmailError("Password reset email could not be sent.") from exc
