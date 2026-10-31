import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

from app import create_app
from app.extensions import db
from app.models import PasswordResetToken, User


class PasswordResetConfig:
    TESTING = True
    SECRET_KEY = "password-reset-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(PasswordResetConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.user = User(name="Reset User", email="reset@example.test",
                         password_hash=generate_password_hash("OldPassword!9"), role="customer")
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def request_reset(self, email="RESET@example.test"):
        return self.client.post("/auth/forgot-password", data={"email": email}, follow_redirects=True)

    def reset_link(self, mocked_sender):
        reset_url = mocked_sender.call_args.args[1]
        return reset_url, urlsplit(reset_url).path.rsplit("/", 1)[-1]

    def create_token(self, user=None, token="known-reset-token", expires_at=None, used_at=None):
        user = user or self.user
        reset_token = PasswordResetToken(
            user_id=user.id, token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            expires_at=expires_at or utcnow() + timedelta(minutes=30), used_at=used_at,
        )
        db.session.add(reset_token)
        db.session.commit()
        return reset_token

    def test_forgot_password_page_loads(self):
        response = self.client.get("/auth/forgot-password")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter the email address", response.data)

    def test_existing_email_sends_reset_link_and_returns_generic_message(self):
        with patch("app.auth.routes.send_password_reset_email") as sender:
            response = self.request_reset()
        self.assertIn(b"If an account exists for this email, a password reset link has been sent.", response.data)
        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0].id, self.user.id)
        reset_url, token = self.reset_link(sender)
        self.assertTrue(reset_url.startswith("http://"))
        self.assertTrue(token)
        self.assertIn("/auth/reset-password/", reset_url)
        stored_token = PasswordResetToken.query.one()
        self.assertNotEqual(stored_token.token_hash, token)
        self.assertEqual(stored_token.token_hash, hashlib.sha256(token.encode("utf-8")).hexdigest())
        self.assertEqual(len(token), 43)
        self.assertGreater(stored_token.expires_at, utcnow())

    def test_missing_smtp_configuration_fails_gracefully(self):
        response = self.request_reset()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"If an account exists for this email, a password reset link has been sent.", response.data)
        self.assertEqual(PasswordResetToken.query.count(), 1)

    def test_unknown_email_has_same_generic_response_and_sends_no_email(self):
        with patch("app.auth.routes.send_password_reset_email") as sender:
            response = self.request_reset("missing@example.test")
        self.assertIn(b"If an account exists for this email, a password reset link has been sent.", response.data)
        self.assertNotIn(b"Email does not exist", response.data)
        sender.assert_not_called()
        self.assertEqual(PasswordResetToken.query.count(), 0)

    def test_valid_token_opens_reset_page(self):
        self.create_token()
        response = self.client.get("/auth/reset-password/known-reset-token")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a new password", response.data)

    def test_expired_used_and_random_tokens_are_rejected(self):
        self.create_token(token="expired-token", expires_at=utcnow() - timedelta(seconds=1))
        self.create_token(token="used-token", used_at=utcnow())
        for token in ("expired-token", "used-token", "random-token"):
            with self.subTest(token=token):
                response = self.client.get(f"/auth/reset-password/{token}", follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"invalid or has expired", response.data)
                self.assertNotIn(b"Choose a new password", response.data)

    def test_weak_password_and_mismatch_are_rejected_without_consuming_token(self):
        reset_token = self.create_token()
        weak = self.client.post("/auth/reset-password/known-reset-token", data={
            "password": "weak", "confirm_password": "weak"})
        self.assertEqual(weak.status_code, 200)
        self.assertIn(b"Password must be more than 8 characters", weak.data)
        mismatch = self.client.post("/auth/reset-password/known-reset-token", data={
            "password": "StrongPass!9", "confirm_password": "OtherPass!9"})
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn(b"Passwords do not match", mismatch.data)
        self.assertIsNone(db.session.get(PasswordResetToken, reset_token.id).used_at)
        self.assertTrue(check_password_hash(self.user.password_hash, "OldPassword!9"))

    def test_valid_reset_updates_hash_allows_new_password_and_consumes_token(self):
        reset_token = self.create_token()
        response = self.client.post("/auth/reset-password/known-reset-token", data={
            "password": "NewPassword!9", "confirm_password": "NewPassword!9"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/login"))
        self.assertTrue(check_password_hash(self.user.password_hash, "NewPassword!9"))
        self.assertFalse(check_password_hash(self.user.password_hash, "OldPassword!9"))
        stored = db.session.get(PasswordResetToken, reset_token.id)
        self.assertIsNotNone(stored.used_at)
        self.assertEqual(self.client.get("/auth/reset-password/known-reset-token", follow_redirects=True).status_code, 200)
        old_login = self.client.post("/auth/login", data={"email": self.user.email, "password": "OldPassword!9"})
        self.assertEqual(old_login.status_code, 200)
        new_login = self.client.post("/auth/login", data={"email": self.user.email, "password": "NewPassword!9"})
        self.assertEqual(new_login.status_code, 302)

    def test_new_reset_request_invalidates_previous_token(self):
        with patch("app.auth.routes.send_password_reset_email") as sender:
            self.request_reset()
            _, old_token = self.reset_link(sender)
            self.request_reset()
            _, new_token = self.reset_link(sender)
        self.assertNotEqual(old_token, new_token)
        tokens = PasswordResetToken.query.order_by(PasswordResetToken.id).all()
        self.assertEqual(len(tokens), 2)
        self.assertIsNotNone(tokens[0].used_at)
        self.assertIsNone(tokens[1].used_at)
        old_response = self.client.get(f"/auth/reset-password/{old_token}", follow_redirects=True)
        self.assertNotIn(b"Choose a new password", old_response.data)
        self.assertEqual(self.client.get(f"/auth/reset-password/{new_token}").status_code, 200)

    def test_password_reset_preserves_each_user_role(self):
        for role in ("admin", "staff", "customer"):
            with self.subTest(role=role):
                user = User(name=role, email=f"{role}@example.test",
                            password_hash=generate_password_hash("OldPassword!9"), role=role)
                db.session.add(user)
                db.session.commit()
                token = f"token-{role}"
                self.create_token(user=user, token=token)
                self.client.post(f"/auth/reset-password/{token}", data={
                    "password": "NewPassword!9", "confirm_password": "NewPassword!9"})
                self.assertEqual(db.session.get(User, user.id).role, role)


if __name__ == "__main__":
    unittest.main()
