import unittest
from datetime import date, timedelta
from decimal import Decimal

from flask import g

from app import create_app
from app.extensions import db
from app.models import Customer, Invoice, Payment, Plan, Product, Subscription, SubscriptionItem, User
from app.billing.service import generate_invoice


class BillingRouteTestConfig:
    TESTING = True
    SECRET_KEY = "billing-route-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class BillingRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(BillingRouteTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.admin = User(name="Admin", email="admin-billing@example.test", password_hash="x", role="admin")
        self.staff = User(name="Staff", email="staff-billing@example.test", password_hash="x", role="staff")
        self.customer_user = User(name="Customer", email="customer-billing@example.test", password_hash="x", role="customer")
        self.other_user = User(name="Other", email="other-billing@example.test", password_hash="x", role="customer")
        db.session.add_all([self.admin, self.staff, self.customer_user, self.other_user])
        db.session.flush()
        self.customer = Customer(user_id=self.customer_user.id, company_name="Northwind Billing", address="1 Main Street")
        self.other_customer = Customer(user_id=self.other_user.id, company_name="Contoso Billing")
        self.product = Product(name="Cloud Support", sales_price=Decimal("100.00"), cost_price=Decimal("0.00"))
        self.plan = Plan(name="Monthly", billing_cycle="monthly", price=Decimal("100.00"))
        db.session.add_all([self.customer, self.other_customer, self.product, self.plan])
        db.session.flush()
        self.subscription = self.make_subscription(self.customer, "SUB-BILL-OWNED")
        self.other_subscription = self.make_subscription(self.other_customer, "SUB-BILL-OTHER")
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def make_subscription(self, customer, number):
        subscription = Subscription(
            subscription_number=number,
            customer=customer,
            plan=self.plan,
            start_date=date.today(),
            expiry_date=date.today() + timedelta(days=60),
            payment_terms="Net 30",
            status="active",
            total_amount=Decimal("100.00"),
        )
        subscription.items.append(SubscriptionItem(
            product=self.product, quantity=1, unit_price=Decimal("100.00"),
            tax_rate=Decimal("0.0000"), amount=Decimal("100.00"),
        ))
        db.session.add(subscription)
        db.session.flush()
        return subscription

    def login(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
            session["billing_csrf_token"] = "test-csrf-token"
        g.pop("_login_user", None)

    def create_invoice(self, subscription=None):
        return generate_invoice((subscription or self.subscription).id)

    def test_billing_dashboard_access_and_database_summary(self):
        invoice = self.create_invoice()
        invoice.status = "confirmed"
        invoice.due_date = date.today() - timedelta(days=1)
        db.session.add(Payment(invoice=invoice, payment_method="card", amount=Decimal("25.00")))
        db.session.commit()
        self.login(self.staff)
        response = self.client.get("/billing/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"TOTAL INVOICES", response.data)
        self.assertIn(b"75.00", response.data)
        self.assertIn(b"REVENUE RECEIVED", response.data)

    def test_invoice_listing_and_filters(self):
        invoice = self.create_invoice()
        self.login(self.admin)
        response = self.client.get("/billing/invoices")
        self.assertEqual(response.status_code, 200)
        self.assertIn(invoice.invoice_number.encode(), response.data)
        self.assertIn(b"Outstanding", response.data)
        filtered = self.client.get("/billing/invoices?status=draft&q=Northwind")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(invoice.invoice_number.encode(), filtered.data)
        self.assertEqual(self.client.get("/billing/invoices?status=paid").status_code, 200)

    def test_invoice_details_show_totals_and_customer(self):
        invoice = self.create_invoice()
        self.login(self.staff)
        response = self.client.get(f"/billing/invoices/{invoice.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(invoice.invoice_number.encode(), response.data)
        self.assertIn(b"Northwind Billing", response.data)
        self.assertIn(b"BILLING PERIOD", response.data)
        self.assertIn(b"Cloud Support", response.data)

    def test_invoice_generation_route_uses_service(self):
        self.login(self.staff)
        response = self.client.post(f"/billing/invoices/generate/{self.subscription.id}", data={"csrf_token": "test-csrf-token"})
        self.assertEqual(response.status_code, 302)
        invoice = Invoice.query.filter_by(subscription_id=self.subscription.id).one()
        self.assertEqual(invoice.status, "draft")
        self.assertIn(f"/billing/invoices/{invoice.id}", response.headers["Location"])

    def test_customer_can_access_only_owned_invoices(self):
        own = self.create_invoice(self.subscription)
        other = self.create_invoice(self.other_subscription)
        self.login(self.customer_user)
        self.assertEqual(self.client.get(f"/billing/invoices/{own.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/billing/invoices/{other.id}").status_code, 403)
        listed = self.client.get("/billing/invoices")
        self.assertIn(own.invoice_number.encode(), listed.data)
        self.assertNotIn(other.invoice_number.encode(), listed.data)
        self.assertEqual(self.client.get(f"/billing/invoices/{own.id}/print").status_code, 200)
        self.assertEqual(self.client.get(f"/billing/invoices/{other.id}/print").status_code, 403)

    def test_unauthenticated_and_unsupported_roles_are_denied(self):
        response = self.client.get("/billing/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])
        self.login(self.customer_user)
        self.assertEqual(self.client.post(f"/billing/invoices/generate/{self.subscription.id}", data={"csrf_token": "test-csrf-token"}).status_code, 403)
        unknown = User(name="Unknown", email="unknown-billing@example.test", password_hash="x", role="auditor")
        db.session.add(unknown)
        db.session.commit()
        self.login(unknown)
        self.assertEqual(self.client.get("/billing/invoices").status_code, 403)

    def test_admin_and_staff_can_manage_but_staff_does_not_gain_admin_access(self):
        invoice = self.create_invoice()
        self.login(self.staff)
        self.assertEqual(self.client.post(f"/billing/invoices/{invoice.id}/confirm", data={"csrf_token": "test-csrf-token"}).status_code, 302)
        self.assertEqual(invoice.status, "confirmed")
        self.assertEqual(self.client.get("/auth/admin/staff").status_code, 403)
        self.login(self.admin)
        response = self.client.post(f"/billing/invoices/{invoice.id}/cancel", data={"csrf_token": "test-csrf-token"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(invoice.status, "cancelled")

    def test_invoice_confirmation_only_changes_draft(self):
        invoice = self.create_invoice()
        self.login(self.admin)
        self.client.post(f"/billing/invoices/{invoice.id}/confirm", data={"csrf_token": "test-csrf-token"})
        self.assertEqual(invoice.status, "confirmed")
        response = self.client.post(f"/billing/invoices/{invoice.id}/confirm", data={"csrf_token": "test-csrf-token"}, follow_redirects=True)
        self.assertIn(b"Only draft invoices can be confirmed", response.data)
        self.assertEqual(invoice.status, "confirmed")

    def test_invoice_cancellation_requires_unpaid_draft_or_confirmed(self):
        invoice = self.create_invoice()
        self.login(self.staff)
        self.client.post(f"/billing/invoices/{invoice.id}/cancel", data={"csrf_token": "test-csrf-token"})
        self.assertEqual(invoice.status, "cancelled")
        another = self.create_invoice(self.other_subscription)
        db.session.add(Payment(invoice=another, payment_method="card", amount=Decimal("10.00")))
        db.session.commit()
        response = self.client.post(f"/billing/invoices/{another.id}/cancel", data={"csrf_token": "test-csrf-token"}, follow_redirects=True)
        self.assertIn(b"Only unpaid draft or confirmed invoices can be cancelled", response.data)
        self.assertEqual(another.status, "draft")

    def test_printable_invoice_uses_browser_print(self):
        invoice = self.create_invoice()
        self.login(self.customer_user)
        response = self.client.get(f"/billing/invoices/{invoice.id}/print")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Brightloop Software", response.data)
        self.assertIn(invoice.invoice_number.encode(), response.data)
        self.assertIn(b"window.print()", response.data)

    def test_management_routes_require_admin_or_staff(self):
        invoice = self.create_invoice()
        self.login(self.customer_user)
        self.assertEqual(self.client.post(f"/billing/invoices/{invoice.id}/confirm", data={"csrf_token": "test-csrf-token"}).status_code, 403)
        self.assertEqual(self.client.post(f"/billing/invoices/{invoice.id}/cancel", data={"csrf_token": "test-csrf-token"}).status_code, 403)
        self.assertEqual(invoice.status, "draft")


if __name__ == "__main__":
    unittest.main()
