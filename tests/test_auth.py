import unittest

from werkzeug.security import check_password_hash

from app import create_app
from app.extensions import db
from app.models import User


class AuthTestConfig:
    TESTING = True
    SECRET_KEY = "auth-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(AuthTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def register(self, **overrides):
        data = {"name": "Test User", "email": "TEST@example.test",
                "password": "StrongPass!9", "confirm_password": "StrongPass!9"}
        data.update(overrides)
        return self.client.post("/auth/register", data=data, follow_redirects=True)

    def test_successful_registration_defaults_to_customer(self):
        response = self.register(role="admin")
        self.assertEqual(response.status_code, 200)
        user = User.query.one()
        self.assertEqual(user.email, "test@example.test")
        self.assertEqual(user.role, "customer")

    def test_duplicate_email_is_rejected_case_insensitively(self):
        self.register()
        response = self.register(email=" test@EXAMPLE.test ")
        self.assertIn(b"already exists", response.data)
        self.assertEqual(User.query.count(), 1)

    def test_weak_password_is_rejected(self):
        response = self.register(password="weak", confirm_password="weak")
        self.assertIn(b"Password must be more than 8 characters", response.data)
        self.assertEqual(User.query.count(), 0)

    def test_password_mismatch_is_rejected(self):
        response = self.register(confirm_password="DifferentPass!9")
        self.assertIn(b"Passwords do not match", response.data)
        self.assertEqual(User.query.count(), 0)

    def test_password_is_stored_as_hash(self):
        self.register()
        user = User.query.one()
        self.assertNotEqual(user.password_hash, "StrongPass!9")
        self.assertTrue(check_password_hash(user.password_hash, "StrongPass!9"))

    def test_successful_login_normalizes_email(self):
        self.register()
        response = self.client.post("/auth/login", data={"email": " TEST@Example.Test ",
                                        "password": "StrongPass!9"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")

    def test_invalid_login_is_rejected(self):
        self.register()
        response = self.client.post("/auth/login", data={"email": "test@example.test",
                                        "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid email or password", response.data)

    def test_logout_requires_login_and_clears_session(self):
        self.assertEqual(self.client.get("/auth/logout").status_code, 302)
        self.register()
        self.client.post("/auth/login", data={"email": "test@example.test", "password": "StrongPass!9"})
        response = self.client.get("/auth/logout")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/auth/login"))
        self.assertEqual(self.client.get("/auth/logout").status_code, 302)

    def test_external_next_url_is_ignored(self):
        self.register()
        response = self.client.post("/auth/login?next=https://evil.example",
                                    data={"email": "test@example.test", "password": "StrongPass!9"})
        self.assertEqual(response.headers["Location"], "/")


if __name__ == "__main__":
    unittest.main()
