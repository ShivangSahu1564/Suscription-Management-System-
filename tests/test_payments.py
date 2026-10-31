import re
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from app import create_app
from app.billing.service import BillingError, calculate_balance_due, calculate_paid_amount, record_payment
from app.extensions import db
from app.models import Customer, Invoice, Payment, Plan, Product, Subscription, SubscriptionItem, User


class PaymentTestConfig:
    TESTING = True
    SECRET_KEY = "payment-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class PaymentManagementTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(PaymentTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.admin = User(name="Admin", email="pay-admin@example.test", password_hash="x", role="admin")
        self.staff = User(name="Staff", email="pay-staff@example.test", password_hash="x", role="staff")
        self.customer_user = User(name="Customer", email="pay-customer@example.test", password_hash="x", role="customer")
        self.other_user = User(name="Other", email="pay-other@example.test", password_hash="x", role="customer")
        db.session.add_all([self.admin, self.staff, self.customer_user, self.other_user])
        db.session.flush()
        self.customer = Customer(user_id=self.customer_user.id, company_name="Payment Customer")
        self.other_customer = Customer(user_id=self.other_user.id, company_name="Other Payment Customer")
        self.product = Product(name="Support", sales_price=Decimal("100.00"), cost_price=Decimal("0.00"))
        self.plan = Plan(name="Monthly", billing_cycle="monthly", price=Decimal("100.00"))
        db.session.add_all([self.customer, self.other_customer, self.product, self.plan])
        db.session.flush()
        self.subscription = self.make_subscription(self.customer, "SUB-PAY-OWN")
        self.other_subscription = self.make_subscription(self.other_customer, "SUB-PAY-OTHER")
        self.invoice = self.make_invoice(self.subscription, "INV-PAY-OWN")
        self.other_invoice = self.make_invoice(self.other_subscription, "INV-PAY-OTHER")
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def make_subscription(self, customer, number):
        subscription = Subscription(
            subscription_number=number, customer=customer, plan=self.plan,
            start_date=date.today(), status="active", total_amount=Decimal("100.00"),
        )
        subscription.items.append(SubscriptionItem(
            product=self.product, quantity=1, unit_price=Decimal("100.00"),
            tax_rate=Decimal("0.0000"), amount=Decimal("100.00"),
        ))
        db.session.add(subscription)
        db.session.flush()
        return subscription

    def make_invoice(self, subscription, number):
        invoice = Invoice(
            invoice_number=number, subscription=subscription, customer=subscription.customer,
            invoice_date=date.today(), due_date=date.today() - timedelta(days=1),
            subtotal=Decimal("100.00"), tax_amount=Decimal("0.00"),
            discount_amount=Decimal("0.00"), total_amount=Decimal("100.00"), status="confirmed",
        )
        db.session.add(invoice)
        db.session.flush()
        return invoice

    def login(self, user):
        with self.client.session_transaction() as session:
            session["_user_id"] = str(user.id)
            session["_fresh"] = True
            session["billing_csrf_token"] = "payment-test-csrf"

    def pay(self, invoice=None, amount="25.00", method="cash", key=None, **kwargs):
        return record_payment(
            invoice_id=(invoice or self.invoice).id, amount=amount, method=method,
            payment_date=kwargs.pop("payment_date", date.today()),
            transaction_reference=kwargs.pop("transaction_reference", None),
            note=kwargs.pop("note", None), submission_key=key or f"submit-{Payment.query.count()}-{method}-{amount}",
        )

    def csrf_and_submission_key(self, invoice_id):
        response = self.client.get(f"/billing/invoices/{invoice_id}")
        csrf = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        submission = re.search(rb'name="submission_key" value="([^"]+)"', response.data)
        return csrf.group(1).decode(), submission.group(1).decode()

    def test_supported_payment_methods_are_recorded(self):
        for method in ("cash", "upi", "card", "bank_transfer"):
            with self.subTest(method=method):
                invoice = self.make_invoice(self.subscription, f"INV-{method}")
                db.session.commit()
                payment = self.pay(invoice=invoice, method=method, key=f"key-{method}")
                self.assertEqual(payment.payment_method, method)
                self.assertEqual(payment.amount, Decimal("25.00"))

    def test_partial_payment_keeps_invoice_confirmed_and_balance_correct(self):
        self.pay(amount="30.00", key="partial-one")
        self.assertEqual(calculate_paid_amount(self.invoice.id), Decimal("30.00"))
        self.assertEqual(calculate_balance_due(self.invoice.id), Decimal("70.00"))
        self.assertEqual(self.invoice.status, "confirmed")

    def test_full_payment_marks_invoice_paid_only_after_payment_saved(self):
        self.assertEqual(self.invoice.status, "confirmed")
        self.pay(amount="100.00", key="full-one")
        self.assertEqual(self.invoice.status, "paid")
        self.assertEqual(calculate_balance_due(self.invoice.id), Decimal("0.00"))
        self.assertEqual(Payment.query.filter_by(invoice_id=self.invoice.id).count(), 1)

    def test_multiple_partial_payments_complete_invoice(self):
        self.pay(amount="40.00", key="multi-one")
        self.pay(amount="60.00", method="upi", key="multi-two")
        self.assertEqual(Payment.query.filter_by(invoice_id=self.invoice.id).count(), 2)
        self.assertEqual(calculate_paid_amount(self.invoice.id), Decimal("100.00"))
        self.assertEqual(self.invoice.status, "paid")

    def test_rejects_negative_zero_invalid_and_fractional_cent_amounts(self):
        for amount in ("-1.00", "0", "nope", "1.001", "NaN", "Infinity"):
            with self.subTest(amount=amount), self.assertRaises(BillingError):
                self.pay(amount=amount, key=f"bad-{amount}")
        self.assertEqual(Payment.query.count(), 0)

    def test_rejects_overpayment(self):
        with self.assertRaisesRegex(BillingError, "cannot exceed"):
            self.pay(amount="100.01", key="overpay")

    def test_rejects_draft_and_cancelled_invoices(self):
        self.invoice.status = "draft"
        db.session.commit()
        with self.assertRaisesRegex(BillingError, "Confirm"):
            self.pay(key="draft-pay")
        self.invoice.status = "cancelled"
        db.session.commit()
        with self.assertRaisesRegex(BillingError, "cancelled"):
            self.pay(key="cancelled-pay")

    def test_duplicate_submission_key_is_rejected(self):
        self.pay(amount="10.00", key="duplicate-key")
        with self.assertRaisesRegex(BillingError, "already been processed"):
            self.pay(amount="10.00", key="duplicate-key")
        self.assertEqual(Payment.query.count(), 1)

    def test_transaction_rolls_back_payment_and_status_on_commit_failure(self):
        with patch.object(db.session, "commit", side_effect=RuntimeError("db failure")):
            with self.assertRaisesRegex(RuntimeError, "db failure"):
                self.pay(amount="10.00", key="rollback-key")
        self.assertEqual(Payment.query.count(), 0)
        self.assertEqual(self.invoice.status, "confirmed")

    def test_overdue_ignores_cancelled_and_fully_paid_invoices(self):
        from app.billing.service import is_invoice_overdue
        self.assertTrue(is_invoice_overdue(self.invoice))
        self.pay(amount="100.00", key="overdue-full")
        self.assertFalse(is_invoice_overdue(self.invoice))
        self.invoice.status = "cancelled"
        db.session.commit()
        self.assertFalse(is_invoice_overdue(self.invoice))

    def test_staff_can_record_payment_with_csrf_and_receipt(self):
        self.login(self.staff)
        csrf, key = self.csrf_and_submission_key(self.invoice.id)
        response = self.client.post(f"/billing/invoices/{self.invoice.id}/payments", data={
            "csrf_token": csrf, "submission_key": key, "amount": "25.00",
            "payment_method": "upi", "payment_date": date.today().isoformat(),
            "transaction_reference": "UPI-123", "note": "Received", 
        })
        self.assertEqual(response.status_code, 302)
        payment = Payment.query.one()
        self.assertEqual(payment.transaction_reference, "UPI-123")
        self.assertIn(f"/billing/payments/{payment.id}", response.headers["Location"])
        receipt = self.client.get(f"/billing/payments/{payment.id}/receipt")
        self.assertEqual(receipt.status_code, 200)
        self.assertIn(b"window.print()", receipt.data)

    def test_admin_can_record_payment(self):
        self.login(self.admin)
        csrf, key = self.csrf_and_submission_key(self.invoice.id)
        response = self.client.post(f"/billing/invoices/{self.invoice.id}/payments", data={
            "csrf_token": csrf, "submission_key": key, "amount": "15.00",
            "payment_method": "card", "payment_date": date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.query.count(), 1)

    def test_invoice_generation_to_payment_workflow(self):
        from app.billing.service import generate_invoice
        subscription = self.make_subscription(self.customer, "SUB-PAY-INTEGRATION")
        db.session.commit()
        invoice = generate_invoice(subscription.id)
        self.assertEqual(invoice.status, "draft")
        invoice.status = "confirmed"
        db.session.commit()
        payment = self.pay(invoice=invoice, amount="100.00", key="generated-invoice-paid")
        self.assertEqual(payment.invoice_id, invoice.id)
        self.assertEqual(invoice.status, "paid")
        self.assertEqual(calculate_balance_due(invoice.id), Decimal("0.00"))

    def test_dashboard_custom_range_excludes_out_of_range_payments(self):
        self.pay(amount="8.00", payment_date=date.today() - timedelta(days=2), key="old-payment")
        self.pay(amount="12.00", payment_date=date.today(), key="today-payment")
        self.login(self.admin)
        response = self.client.get(
            f"/billing/?period=custom&start={date.today()}&end={date.today()}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"12.00", response.data)
        self.assertNotIn(b"20.00", response.data)

    def test_invoice_payment_status_filters_include_expected_states(self):
        self.login(self.admin)
        self.pay(amount="20.00", key="partial-filter")
        response = self.client.get("/billing/invoices?status=partially_paid")
        self.assertIn(b"INV-PAY-OWN", response.data)
        self.invoice.status = "cancelled"
        db.session.commit()
        cancelled = self.client.get("/billing/invoices?status=cancelled")
        self.assertIn(b"INV-PAY-OWN", cancelled.data)

    def test_payment_post_without_csrf_is_rejected(self):
        self.login(self.staff)
        response = self.client.post(f"/billing/invoices/{self.invoice.id}/payments", data={
            "submission_key": "no-csrf", "amount": "10.00", "payment_method": "cash",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Payment.query.count(), 0)

    def test_customer_cannot_record_manual_payment(self):
        self.login(self.customer_user)
        self.assertEqual(self.client.post(
            f"/billing/invoices/{self.invoice.id}/payments",
            data={"csrf_token": "payment-test-csrf"},
        ).status_code, 403)

    def test_customer_can_view_own_but_not_other_payment(self):
        own = self.pay(amount="10.00", key="own-payment")
        other = self.pay(invoice=self.other_invoice, amount="10.00", key="other-payment")
        self.login(self.customer_user)
        self.assertEqual(self.client.get(f"/billing/payments/{own.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/billing/payments/{own.id}/receipt").status_code, 200)
        self.assertEqual(self.client.get(f"/billing/payments/{other.id}").status_code, 403)
        self.assertEqual(self.client.get(f"/billing/payments/{other.id}/receipt").status_code, 403)
        listing = self.client.get("/billing/payments")
        self.assertIn(b"INV-PAY-OWN", listing.data)
        self.assertNotIn(b"INV-PAY-OTHER", listing.data)

    def test_payment_history_routes_load(self):
        self.pay(amount="10.00", key="history-route")
        self.login(self.admin)
        self.assertEqual(self.client.get("/billing/payments").status_code, 200)
        self.assertEqual(self.client.get(f"/billing/invoices/{self.invoice.id}/payments").status_code, 200)

    def test_dashboard_uses_real_payment_and_period_filter_data(self):
        self.pay(amount="12.50", method="cash", key="dashboard-cash")
        self.login(self.admin)
        response = self.client.get("/billing/?period=today")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"12.50", response.data)
        self.assertIn(b"Cash", response.data)
        self.assertIn(b"PAYMENTS RECEIVED", response.data)
        self.assertEqual(self.client.get("/billing/?period=custom&start=invalid&end=invalid").status_code, 200)

    def test_unauthorized_payment_pages_redirect_to_login(self):
        self.assertEqual(self.client.get("/billing/payments").status_code, 302)
        self.assertEqual(self.client.get(f"/billing/payments/999/receipt").status_code, 302)


if __name__ == "__main__":
    unittest.main()
