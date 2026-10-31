import unittest
from datetime import date, timedelta
from decimal import Decimal

from flask import g
from sqlalchemy.exc import IntegrityError

from app import create_app
from app.extensions import db
from app.models import Customer, Plan, Product, Quotation, QuotationItem, Subscription, SubscriptionItem, User
from app.subscriptions.service import add_cycle


class SubscriptionTestConfig:
    TESTING = True
    SECRET_KEY = "subscription-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class SubscriptionFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(SubscriptionTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.staff = User(name="Staff", email="staff-sub@example.test", password_hash="x", role="staff")
        self.customer_user = User(name="Customer", email="customer-sub@example.test", password_hash="x", role="customer")
        db.session.add_all([self.staff, self.customer_user])
        db.session.flush()
        self.customer = Customer(user_id=self.customer_user.id, company_name="Northwind Ltd")
        self.other_customer = Customer(company_name="Other Ltd")
        self.product = Product(name="Cloud support", sales_price=Decimal("100.00"), cost_price=Decimal("25.00"))
        self.plan = Plan(name="Monthly support", billing_cycle="monthly", price=Decimal("80.00"))
        db.session.add_all([self.customer, self.other_customer, self.product, self.plan])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def login(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
        g.pop("_login_user", None)

    def form(self, **overrides):
        data = {
            "customer_id": str(self.customer.id), "plan_id": str(self.plan.id),
            "start_date": date.today().isoformat(),
            "expiry_date": (date.today() + timedelta(days=60)).isoformat(),
            "payment_terms": "Net 30", "product_id": str(self.product.id),
            "quantity": "2", "tax_rate": "10",
        }
        data.update(overrides)
        return data

    def add_subscription(self, status="active", expiry=None):
        subscription = Subscription(
            subscription_number=f"SUB-TEST-{Subscription.query.count()+1}",
            customer=self.customer, plan=self.plan, start_date=date.today() - timedelta(days=5),
            expiry_date=expiry or date.today() + timedelta(days=30), payment_terms="Net 30",
            status=status, total_amount=Decimal("220.00"),
        )
        subscription.items.append(SubscriptionItem(
            product=self.product, quantity=2, unit_price=Decimal("100.00"),
            tax_rate=Decimal("10"), amount=Decimal("220.00"),
        ))
        db.session.add(subscription)
        db.session.commit()
        return subscription

    def test_creation_calculates_money_and_edits_draft(self):
        self.login(self.staff)
        self.assertEqual(self.client.get("/subscriptions/create").status_code, 200)
        data = self.form(save_as_draft="1")
        response = self.client.post("/subscriptions/create", data=data, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        subscription = Subscription.query.one()
        self.assertTrue(subscription.subscription_number.startswith("SUB-"))
        self.assertEqual(subscription.status, "draft")
        self.assertEqual(subscription.items[0].unit_price, Decimal("100.00"))
        self.assertEqual(subscription.items[0].amount, Decimal("220.00"))
        self.assertEqual(subscription.total_amount, Decimal("220.00"))
        self.assertEqual(self.client.get(f"/subscriptions/{subscription.id}/edit").status_code, 200)
        response = self.client.post(f"/subscriptions/{subscription.id}/edit", data=self.form(quantity="3"), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(subscription.total_amount, Decimal("330.00"))
        self.assertEqual(subscription.items[0].quantity, 3)
        self.client.post("/subscriptions/create", data=self.form(), follow_redirects=True)
        numbers = [s.subscription_number for s in Subscription.query.order_by(Subscription.id).all()]
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_confirm_activate_pause_resume_and_close_transitions(self):
        self.login(self.staff)
        self.client.post("/subscriptions/create", data=self.form(), follow_redirects=True)
        subscription = Subscription.query.one()
        self.assertEqual(subscription.status, "quotation")
        self.client.post(f"/subscriptions/{subscription.id}/confirm")
        self.assertEqual(subscription.status, "confirmed")
        self.client.post(f"/subscriptions/{subscription.id}/activate")
        self.assertEqual(subscription.status, "active")
        self.client.post(f"/subscriptions/{subscription.id}/pause")
        self.assertEqual(subscription.status, "paused")
        self.client.post(f"/subscriptions/{subscription.id}/resume")
        self.assertEqual(subscription.status, "active")
        self.client.post(f"/subscriptions/{subscription.id}/close")
        self.assertEqual(subscription.status, "closed")
        # Closing again is rejected and leaves the terminal state intact.
        response = self.client.post(f"/subscriptions/{subscription.id}/close", follow_redirects=True)
        self.assertIn(b"status transition is not allowed", response.data)

    def test_confirmed_quotation_converts_once_and_preserves_agreed_amounts(self):
        quote = Quotation(quotation_number="Q-SUB-1", customer=self.customer,
                          valid_until=date.today() + timedelta(days=15), status="confirmed",
                          total_amount=Decimal("84.00"))
        quote.items.append(QuotationItem(product=self.product, quantity=2,
                                         unit_price=Decimal("40.00"), tax_rate=Decimal("5"),
                                         amount=Decimal("84.00")))
        db.session.add(quote)
        db.session.commit()
        self.login(self.staff)
        response = self.client.post(f"/quotations/{quote.id}/convert", data={"plan_id": self.plan.id}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        subscription = Subscription.query.one()
        self.assertEqual(subscription.status, "confirmed")
        self.assertEqual(quote.status, "converted")
        self.assertEqual(subscription.customer_id, quote.customer_id)
        self.assertEqual(subscription.items[0].unit_price, Decimal("40.00"))
        self.assertEqual(subscription.items[0].amount, Decimal("84.00"))
        self.assertEqual(subscription.total_amount, Decimal("84.00"))
        self.client.post(f"/subscriptions/{subscription.id}/activate")
        self.assertEqual(subscription.status, "active")
        response = self.client.post(f"/quotations/{quote.id}/convert", data={"plan_id": self.plan.id}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Subscription.query.count(), 1)
        self.assertIn(b"already been converted", response.data)

    def test_monthly_renewal_and_expiry_handling(self):
        self.login(self.staff)
        subscription = self.add_subscription(status="active", expiry=date.today() + timedelta(days=10))
        old_start = subscription.start_date
        old_expiry = subscription.expiry_date
        self.client.post(f"/subscriptions/{subscription.id}/renew")
        self.assertEqual(subscription.start_date, old_start)
        self.assertEqual(subscription.expiry_date, add_cycle(old_expiry, "monthly"))
        expired = self.add_subscription(status="active", expiry=date.today() - timedelta(days=1))
        response = self.client.get(f"/subscriptions/{expired.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(expired.status, "closed")

    def test_daily_weekly_and_yearly_billing_cycles(self):
        from app.subscriptions.service import add_cycle
        anchor = date(2024, 2, 29)
        self.assertEqual(add_cycle(anchor, "daily"), date(2024, 3, 1))
        self.assertEqual(add_cycle(anchor, "weekly"), date(2024, 3, 7))
        self.assertEqual(add_cycle(anchor, "yearly"), date(2025, 2, 28))

    def test_customer_ownership_and_billing_data(self):
        owned = self.add_subscription()
        other = Subscription(subscription_number="SUB-OTHER", customer=self.other_customer,
                             plan=self.plan, start_date=date.today(), expiry_date=date.today()+timedelta(days=30),
                             status="active", total_amount=Decimal("1.00"))
        other.items.append(SubscriptionItem(
            product=self.product, quantity=1, unit_price=Decimal("1"), tax_rate=Decimal("0"), amount=Decimal("1")))
        db.session.add(other)
        db.session.commit()
        self.login(self.customer_user)
        self.assertEqual(self.client.get(f"/subscriptions/{owned.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/subscriptions/{other.id}").status_code, 403)
        self.assertEqual(self.client.get("/subscriptions/create").status_code, 403)
        response = self.client.get(f"/subscriptions/{owned.id}/billing-data")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["customer_id"], owned.customer_id)
        self.assertEqual(payload["plan"]["billing_cycle"], "monthly")
        self.assertEqual(payload["items"][0]["product_id"], self.product.id)
        self.assertIn("start_date", payload)
        self.assertIn("expiry_date", payload)
        self.assertIn("total_amount", payload)
        self.assertIn("status", payload)

        non_customer = User(name="Member", email="member-sub@example.test", password_hash="x", role="member")
        db.session.add(non_customer)
        db.session.flush()
        db.session.add(Customer(user_id=non_customer.id, company_name="Member linked customer"))
        db.session.commit()
        self.login(non_customer)
        self.assertEqual(self.client.get(f"/subscriptions/{owned.id}").status_code, 403)

    def test_admin_can_manage_and_sqlite_enforces_foreign_keys(self):
        admin = User(name="Admin", email="admin-sub@example.test", password_hash="x", role="admin")
        db.session.add(admin)
        db.session.commit()
        self.login(admin)
        self.assertEqual(self.client.get("/subscriptions/create").status_code, 200)
        self.assertEqual(self.client.get("/quotations/create").status_code, 200)
        response = self.client.post("/quotations/create", data={
            "customer_id": str(self.customer.id),
            "valid_until": (date.today() + timedelta(days=10)).isoformat(),
            "product_id": str(self.product.id), "quantity": "1", "tax_rate": "0",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.execute(db.text("PRAGMA foreign_keys")).scalar(), 1)
        broken = Subscription(
            subscription_number="SUB-BAD-FK", customer_id=987654, plan_id=self.plan.id,
            start_date=date.today(), expiry_date=date.today()+timedelta(days=3), status="draft",
        )
        db.session.add(broken)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_invalid_ids_quantities_and_dates(self):
        self.login(self.staff)
        invalid = self.form(customer_id="99999")
        response = self.client.post("/subscriptions/create", data=invalid)
        self.assertIn(b"Select a valid customer", response.data)
        invalid = self.form(product_id="99999")
        response = self.client.post("/subscriptions/create", data=invalid)
        self.assertIn(b"A selected product does not exist", response.data)
        invalid = self.form(quantity="-2")
        response = self.client.post("/subscriptions/create", data=invalid)
        self.assertIn(b"Quantities must be greater than zero", response.data)
        invalid = self.form(tax_rate="NaN")
        response = self.client.post("/subscriptions/create", data=invalid)
        self.assertIn(b"Tax rate must be a finite number", response.data)
        invalid = self.form(expiry_date=date.today().isoformat())
        response = self.client.post("/subscriptions/create", data=invalid)
        self.assertIn(b"Expiry date must be after the start date", response.data)
        self.assertEqual(Subscription.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
