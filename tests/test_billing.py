import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from app import create_app
from app.billing.service import (
    BillingError, calculate_balance_due, calculate_invoice_totals,
    calculate_paid_amount, generate_invoice, is_invoice_overdue,
)
from app.extensions import db
from app.models import (
    Customer, Discount, Invoice, Payment, Plan, Product, Subscription,
    SubscriptionItem,
)


class BillingTestConfig:
    TESTING = True
    SECRET_KEY = "billing-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class BillingServiceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(BillingTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.customer = Customer(company_name="Billing Customer")
        self.product = Product(name="Service", sales_price=Decimal("100.00"), cost_price=Decimal("0.00"))
        self.plan = Plan(name="Billing Plan", billing_cycle="daily", price=Decimal("100.00"))
        db.session.add_all([self.customer, self.product, self.plan])
        db.session.flush()
        self.subscription = self.make_subscription()
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        self.ctx.pop()

    def make_subscription(self, cycle="daily", status="active", start=None):
        self.plan.billing_cycle = cycle
        subscription = Subscription(
            subscription_number=f"SUB-BILL-{Subscription.query.count() + 1}",
            customer=self.customer,
            plan=self.plan,
            start_date=start or date.today(),
            expiry_date=date.today() + timedelta(days=90),
            payment_terms="Net 30",
            status=status,
            total_amount=Decimal("220.00"),
        )
        subscription.items.append(SubscriptionItem(
            product=self.product,
            quantity=2,
            unit_price=Decimal("100.00"),
            tax_rate=Decimal("10.0000"),
            amount=Decimal("220.00"),
        ))
        db.session.add(subscription)
        db.session.flush()
        return subscription

    def test_successful_invoice_generation_and_totals(self):
        discount = Discount(
            name="Ten off", discount_type="fixed", value=Decimal("10.00"),
            minimum_quantity=2, start_date=date.today(), end_date=date.today(),
            usage_limit=1, usage_count=0, products=[self.product],
        )
        db.session.add(discount)
        db.session.commit()

        invoice = generate_invoice(self.subscription.id)

        self.assertEqual(invoice.customer_id, self.customer.id)
        self.assertEqual(invoice.status, "draft")
        self.assertEqual(invoice.billing_period_start, date.today())
        self.assertEqual(invoice.billing_period_end, date.today())
        self.assertEqual(invoice.due_date, date.today() + timedelta(days=30))
        self.assertEqual(invoice.invoice_number.startswith("INV-"), True)
        self.assertEqual(len(invoice.items), 1)
        self.assertEqual(invoice.items[0].description, self.product.name)
        self.assertEqual(invoice.items[0].quantity, 2)
        self.assertEqual(invoice.items[0].unit_price, Decimal("100.00"))
        self.assertEqual(invoice.items[0].tax_rate, Decimal("10.0000"))
        self.assertEqual(invoice.items[0].amount, Decimal("190.00"))
        self.assertEqual(invoice.subtotal, Decimal("200.00"))
        self.assertEqual(invoice.discount_amount, Decimal("10.00"))
        self.assertEqual(invoice.tax_amount, Decimal("19.00"))
        self.assertEqual(invoice.total_amount, Decimal("209.00"))
        self.assertEqual(discount.usage_count, 1)

    def test_inactive_subscription_is_rejected(self):
        self.subscription.status = "paused"
        db.session.commit()
        with self.assertRaisesRegex(BillingError, "Only active"):
            generate_invoice(self.subscription.id)
        self.assertEqual(Invoice.query.count(), 0)

    def test_duplicate_generation_does_not_duplicate_period(self):
        invoice = generate_invoice(self.subscription.id)
        with self.assertRaisesRegex(BillingError, "period has not started"):
            generate_invoice(self.subscription.id)
        self.assertEqual(Invoice.query.filter_by(subscription_id=self.subscription.id).count(), 1)
        self.assertEqual(invoice.billing_period_start, date.today())

    def test_daily_billing_period(self):
        invoice = generate_invoice(self.subscription.id)
        self.assertEqual(invoice.billing_period_end, invoice.billing_period_start)

    def test_weekly_billing_period(self):
        self.plan.billing_cycle = "weekly"
        db.session.commit()
        invoice = generate_invoice(self.subscription.id)
        self.assertEqual(invoice.billing_period_end, invoice.billing_period_start + timedelta(days=6))

    def test_monthly_billing_period(self):
        self.plan.billing_cycle = "monthly"
        db.session.commit()
        invoice = generate_invoice(self.subscription.id)
        from app.billing.service import _add_cycle
        self.assertEqual(invoice.billing_period_end, _add_cycle(invoice.billing_period_start, "monthly") - timedelta(days=1))

    def test_yearly_billing_period(self):
        self.plan.billing_cycle = "yearly"
        db.session.commit()
        invoice = generate_invoice(self.subscription.id)
        from app.billing.service import _add_cycle
        self.assertEqual(invoice.billing_period_end, _add_cycle(invoice.billing_period_start, "yearly") - timedelta(days=1))

    def test_percentage_discount_is_applied_before_tax(self):
        db.session.add(Discount(
            name="Ten percent", discount_type="percentage", value=Decimal("10.00"),
            minimum_quantity=1, products=[self.product],
        ))
        db.session.commit()
        invoice = generate_invoice(self.subscription.id)
        self.assertEqual(invoice.discount_amount, Decimal("20.00"))
        self.assertEqual(invoice.tax_amount, Decimal("18.00"))
        self.assertEqual(invoice.total_amount, Decimal("198.00"))

    def test_ineligible_expired_or_product_mismatched_discounts_are_not_applied(self):
        other = Product(name="Other", sales_price=Decimal("5.00"), cost_price=Decimal("0.00"))
        db.session.add(other)
        db.session.flush()
        db.session.add_all([
            Discount(name="Expired", discount_type="fixed", value=Decimal("5.00"),
                     end_date=date.today() - timedelta(days=1), products=[self.product]),
            Discount(name="Wrong product", discount_type="fixed", value=Decimal("5.00"),
                     products=[other]),
            Discount(name="Quantity threshold", discount_type="fixed", value=Decimal("5.00"),
                     minimum_quantity=3, products=[self.product]),
        ])
        db.session.commit()
        invoice = generate_invoice(self.subscription.id)
        self.assertEqual(invoice.discount_amount, Decimal("0.00"))
        self.assertEqual(invoice.tax_amount, Decimal("20.00"))
        self.assertEqual(invoice.total_amount, Decimal("220.00"))

    def test_discount_requires_active_and_minimum_purchase_eligibility(self):
        inactive = Discount(
            name="Inactive", discount_type="fixed", value=Decimal("10.00"),
            is_active=False, products=[self.product],
        )
        above_threshold = Discount(
            name="High minimum", discount_type="fixed", value=Decimal("10.00"),
            minimum_purchase_amount=Decimal("250.00"), products=[self.product],
        )
        active_eligible = Discount(
            name="Eligible", discount_type="fixed", value=Decimal("5.00"),
            minimum_purchase_amount=Decimal("200.00"), products=[self.product],
        )
        db.session.add_all([inactive, above_threshold, active_eligible])
        db.session.commit()

        invoice = generate_invoice(self.subscription.id)

        self.assertEqual(invoice.discount_amount, Decimal("5.00"))
        self.assertEqual(active_eligible.usage_count, 1)
        self.assertEqual(inactive.usage_count, 0)
        self.assertEqual(above_threshold.usage_count, 0)

    def test_calculate_invoice_totals_from_invoice_lines(self):
        invoice = generate_invoice(self.subscription.id)
        totals = calculate_invoice_totals(invoice)
        self.assertEqual(totals["subtotal"], Decimal("200.00"))
        self.assertEqual(totals["tax_amount"], Decimal("20.00"))
        self.assertEqual(totals["total_amount"], Decimal("220.00"))

    def test_payment_balance_calculation_supports_partial_and_full_payment(self):
        invoice = generate_invoice(self.subscription.id)
        db.session.add_all([
            Payment(invoice=invoice, payment_method="card", amount=Decimal("50.00")),
            Payment(invoice=invoice, payment_method="bank", amount=Decimal("70.00")),
        ])
        db.session.commit()
        self.assertEqual(calculate_paid_amount(invoice.id), Decimal("120.00"))
        self.assertEqual(calculate_balance_due(invoice.id), Decimal("100.00"))
        db.session.add(Payment(invoice=invoice, payment_method="cash", amount=Decimal("100.00")))
        db.session.commit()
        self.assertEqual(calculate_balance_due(invoice.id), Decimal("0.00"))

    def test_overdue_requires_past_due_date_and_positive_balance(self):
        invoice = generate_invoice(self.subscription.id)
        invoice.due_date = date.today() - timedelta(days=1)
        db.session.commit()
        self.assertTrue(is_invoice_overdue(invoice))
        db.session.add(Payment(invoice=invoice, payment_method="card", amount=invoice.total_amount))
        db.session.commit()
        self.assertFalse(is_invoice_overdue(invoice))

    def test_balance_never_goes_negative(self):
        invoice = generate_invoice(self.subscription.id)
        db.session.add(Payment(invoice=invoice, payment_method="card", amount=invoice.total_amount + Decimal("5.00")))
        db.session.commit()
        self.assertEqual(calculate_balance_due(invoice.id), Decimal("0.00"))

    def test_transaction_rolls_back_invoice_and_discount_usage_on_failure(self):
        discount = Discount(name="Failure discount", discount_type="fixed", value=Decimal("5.00"),
                            products=[self.product])
        db.session.add(discount)
        db.session.commit()
        with patch.object(db.session, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                generate_invoice(self.subscription.id)
        self.assertEqual(Invoice.query.count(), 0)
        self.assertEqual(db.session.get(Discount, discount.id).usage_count, 0)


if __name__ == "__main__":
    unittest.main()
