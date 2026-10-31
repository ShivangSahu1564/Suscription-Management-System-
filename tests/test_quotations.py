import unittest
from datetime import date, timedelta
from decimal import Decimal
from flask import g
from werkzeug.datastructures import MultiDict

from app import create_app
from app.extensions import db
from app.models import Customer, Plan, Product, Quotation, QuotationTemplate, User


class TestConfig:
    TESTING = True
    SECRET_KEY = "quotation-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class QuotationFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.staff = User(name="Staff", email="staff@example.test", password_hash="x", role="staff")
        self.customer_user = User(name="Customer", email="customer@example.test", password_hash="x", role="customer")
        db.session.add_all([self.staff, self.customer_user])
        db.session.flush()
        self.customer = Customer(user_id=self.customer_user.id, company_name="Acme Ltd")
        self.other_customer = Customer(company_name="Other Ltd")
        self.product = Product(name="Support", sales_price=Decimal("50.00"), cost_price=Decimal("10.00"))
        self.plan = Plan(name="Monthly", billing_cycle="monthly", price=Decimal("50.00"))
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
        # The fixture keeps an app context open, so clear Flask-Login's cached user.
        g.pop("_login_user", None)

    def form(self, customer_id=None, quantity="2"):
        return {
            "customer_id": str(customer_id or self.customer.id),
            "valid_until": (date.today() + timedelta(days=20)).isoformat(),
            "product_id": str(self.product.id), "quantity": quantity, "tax_rate": "10",
        }

    def create_quote(self, draft=False):
        self.login(self.staff)
        data = self.form()
        if draft:
            data["save_as_draft"] = "1"
        response = self.client.post("/quotations/create", data=data, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        quote = db.session.scalar(db.select(Quotation).order_by(Quotation.id.desc()))
        return quote

    def test_create_calculation_edit_confirmation_and_print(self):
        quote = self.create_quote(draft=True)
        self.assertEqual(quote.status, "draft")
        self.assertEqual(quote.total_amount, Decimal("110.00"))
        self.assertEqual(quote.items[0].amount, Decimal("110.00"))
        edited = self.form(quantity="3")
        response = self.client.post(f"/quotations/{quote.id}/edit", data=edited, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(quote.total_amount, Decimal("165.00"))
        response = self.client.post(f"/quotations/{quote.id}/confirm", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(quote.status, "confirmed")
        print_response = self.client.get(f"/quotations/{quote.id}")
        self.assertEqual(print_response.status_code, 200)
        self.assertIn(b"window.print()", print_response.data)
        conversion = self.client.post(f"/quotations/{quote.id}/convert", data={"plan_id": self.plan.id}, follow_redirects=True)
        self.assertEqual(conversion.status_code, 200)
        self.assertIn(b"converted to subscription", conversion.data)

    def test_customer_can_only_view_owned_quotes(self):
        owned = self.create_quote()
        self.login(self.staff)
        other_form = self.form(customer_id=self.other_customer.id)
        response = self.client.post("/quotations/create", data=other_form, follow_redirects=True)
        other = db.session.scalar(db.select(Quotation).order_by(Quotation.id.desc()))
        self.assertEqual(response.status_code, 200)
        self.login(self.customer_user)
        self.assertEqual(self.client.get(f"/quotations/{owned.id}").status_code, 200)
        self.assertEqual(self.client.get(f"/quotations/{other.id}").status_code, 403)
        listing = self.client.get("/quotations/")
        self.assertIn(owned.quotation_number.encode(), listing.data)
        self.assertNotIn(other.quotation_number.encode(), listing.data)
        self.assertEqual(self.client.get("/quotations/create").status_code, 403)

        member = User(name="Member", email="member-quote@example.test", password_hash="x", role="member")
        db.session.add(member)
        db.session.flush()
        db.session.add(Customer(user_id=member.id, company_name="Linked member"))
        db.session.commit()
        self.login(member)
        self.assertEqual(self.client.get(f"/quotations/{owned.id}").status_code, 403)
        self.assertEqual(self.client.get("/quotations/").status_code, 403)

    def test_admin_can_manage_quotations(self):
        admin = User(name="Admin", email="admin-quote@example.test", password_hash="x", role="admin")
        db.session.add(admin)
        db.session.commit()
        self.login(admin)
        self.assertEqual(self.client.get("/quotations/create").status_code, 200)
        response = self.client.post("/quotations/create", data=self.form(), follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Quotation.query.count(), 1)

    def test_invalid_customer_and_negative_quantity_are_rejected(self):
        self.login(self.staff)
        invalid_customer = self.form(customer_id=999999)
        response = self.client.post("/quotations/create", data=invalid_customer)
        self.assertIn(b"Select a valid customer", response.data)
        invalid_quantity = self.form(quantity="-1")
        response = self.client.post("/quotations/create", data=invalid_quantity)
        self.assertIn(b"Quantities must be greater than zero", response.data)
        invalid_product = self.form()
        invalid_product["product_id"] = "999999"
        response = self.client.post("/quotations/create", data=invalid_product)
        self.assertIn(b"A selected product no longer exists", response.data)
        invalid_tax = self.form()
        invalid_tax["tax_rate"] = "NaN"
        response = self.client.post("/quotations/create", data=invalid_tax)
        self.assertIn(b"Tax rate must be a finite number", response.data)
        self.assertEqual(db.session.query(Quotation).count(), 0)

    def test_multiple_products_tax_total_and_unique_quotation_numbers(self):
        self.login(self.staff)
        second_product = Product(name="Monitoring", sales_price=Decimal("12.34"), cost_price=Decimal("2.00"))
        db.session.add(second_product)
        db.session.commit()
        data = MultiDict([
            ("customer_id", str(self.customer.id)),
            ("valid_until", (date.today() + timedelta(days=20)).isoformat()),
            ("product_id", str(self.product.id)), ("quantity", "1"), ("tax_rate", "5"),
            ("product_id", str(second_product.id)), ("quantity", "2"), ("tax_rate", "10"),
        ])
        response = self.client.post("/quotations/create", data=data, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        first = Quotation.query.one()
        self.assertEqual(len(first.items), 2)
        self.assertEqual(first.items[0].amount, Decimal("52.50"))
        self.assertEqual(first.items[1].amount, Decimal("27.15"))
        self.assertEqual(first.total_amount, Decimal("79.65"))
        self.assertEqual(first.customer.id, self.customer.id)
        self.assertEqual(first.items[1].product.id, second_product.id)
        self.client.post("/quotations/create", data=self.form(), follow_redirects=True)
        second = Quotation.query.order_by(Quotation.id.desc()).first()
        self.assertNotEqual(first.quotation_number, second.quotation_number)

    def test_template_is_saved_and_reused_on_creation_page(self):
        self.login(self.staff)
        response = self.client.post("/quotations/templates/create", data={
            "name": "Support pack", "product_id": str(self.product.id),
            "quantity": "2", "tax_rate": "5",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        template = QuotationTemplate.query.filter_by(name="Support pack").one()
        response = self.client.post(f"/quotations/templates/{template.id}/edit", data={
            "name": "Support pack", "product_id": str(self.product.id),
            "quantity": "4", "tax_rate": "5",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(template.items[0].quantity, 4)
        page = self.client.get("/quotations/create")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Support pack", page.data)
        self.assertIn(b"product_id", page.data)


if __name__ == "__main__":
    unittest.main()
