import unittest
from decimal import Decimal

from flask import g

from app import create_app
from app.extensions import db
from app.models import Discount, Plan, Product, ProductVariant, Tax, User


class CatalogConfig:
    TESTING = True
    SECRET_KEY = "catalog-tests"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    AUTO_INIT_DB = True


class CatalogManagementTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(CatalogConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.client = self.app.test_client()
        self.admin = User(name="Admin", email="catalog-admin@test", password_hash="x", role="admin")
        self.staff = User(name="Staff", email="catalog-staff@test", password_hash="x", role="staff")
        self.customer = User(name="Customer", email="catalog-customer@test", password_hash="x", role="customer")
        db.session.add_all([self.admin, self.staff, self.customer])
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

    def test_product_crud_activation_and_validation_permissions(self):
        self.login(self.admin)
        response = self.client.post("/catalog/products/create", data={"name": "Cloud", "product_type": "service", "sales_price": "100", "cost_price": "30"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        product = Product.query.one()
        self.assertEqual(product.sales_price, Decimal("100"))
        self.assertEqual(self.client.get(f"/catalog/products/{product.id}").status_code, 200)
        self.client.post(f"/catalog/products/{product.id}/edit", data={"name": "Cloud Plus", "sales_price": "120", "cost_price": "30"})
        self.assertEqual(product.name, "Cloud Plus")
        self.client.post(f"/catalog/products/{product.id}/toggle")
        self.assertFalse(product.is_active)
        response = self.client.post("/catalog/products/create", data={"name": "Bad", "sales_price": "-1", "cost_price": "0"})
        self.assertIn(b"cannot be negative", response.data)
        self.login(self.staff)
        self.assertEqual(self.client.get("/catalog/products").status_code, 200)
        self.assertEqual(self.client.get("/catalog/products/create").status_code, 403)
        self.login(self.customer)
        self.assertEqual(self.client.get("/catalog/products").status_code, 403)

    def test_variant_price_and_validation(self):
        product = Product(name="ERP", sales_price=Decimal("1000"), cost_price=Decimal("0"))
        db.session.add(product)
        db.session.commit()
        self.login(self.admin)
        self.client.post(f"/catalog/products/{product.id}/variants", data={"variant_name": "Odoo", "additional_price": "560"})
        variant = ProductVariant.query.one()
        self.assertEqual(Decimal(product.sales_price) + Decimal(variant.additional_price), Decimal("1560"))
        self.client.post(f"/catalog/variants/{variant.id}/edit", data={"variant_name": "Odoo Enterprise", "additional_price": "600"})
        self.assertEqual(variant.additional_price, Decimal("600"))
        response = self.client.post(f"/catalog/variants/{variant.id}/edit", data={"variant_name": "No", "additional_price": "-2"}, follow_redirects=True)
        self.assertIn(b"cannot be negative", response.data)

    def test_plan_validation_associations_and_activation(self):
        product = Product(name="Support", sales_price=Decimal("5"), cost_price=Decimal("1"))
        db.session.add(product)
        db.session.commit()
        self.login(self.admin)
        data = {"name": "Monthly", "billing_cycle": "monthly", "price": "80", "minimum_quantity": "2", "product_ids": str(product.id), "quantities": "3", "is_active": "1"}
        self.assertEqual(self.client.post("/catalog/plans/create", data=data, follow_redirects=True).status_code, 200)
        plan = Plan.query.one()
        self.assertEqual(plan.products[0].quantity, 3)
        self.assertEqual(self.client.get(f"/catalog/plans/{plan.id}/edit").status_code, 200)
        invalid = dict(data, billing_cycle="fortnightly")
        response = self.client.post("/catalog/plans/create", data=invalid)
        self.assertIn(b"Choose daily", response.data)
        self.client.post(f"/catalog/plans/{plan.id}/toggle")
        self.assertFalse(plan.is_active)

    def test_tax_and_discount_admin_permissions_and_validation(self):
        self.login(self.admin)
        self.client.post("/catalog/taxes", data={"name": "GST", "rate": "18"})
        tax = Tax.query.one()
        self.assertEqual(tax.rate, Decimal("18"))
        response = self.client.post("/catalog/taxes", data={"name": "Invalid", "rate": "101"}, follow_redirects=True)
        self.assertIn(b"between 0 and 100", response.data)
        self.client.post("/catalog/discounts/create", data={"name": "Summer", "discount_type": "percentage", "value": "15", "minimum_quantity": "2", "minimum_purchase_amount": "0", "is_active": "1"})
        discount = Discount.query.one()
        self.assertEqual(discount.value, Decimal("15"))
        self.assertEqual(discount.minimum_quantity, 2)
        self.login(self.staff)
        self.assertEqual(self.client.get("/catalog/taxes").status_code, 200)
        self.assertEqual(self.client.post("/catalog/taxes", data={"name": "Forbidden", "rate": "1"}).status_code, 403)
        self.assertEqual(self.client.get("/catalog/discounts/create").status_code, 403)


if __name__ == "__main__":
    unittest.main()
