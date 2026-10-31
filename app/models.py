from datetime import datetime, date
from decimal import Decimal

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index

from .extensions import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(40), nullable=False, default="member", index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    customer = db.relationship("Customer", back_populates="user", uselist=False)


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


class Customer(db.Model):
    __tablename__ = "customers"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), unique=True)
    company_name = db.Column(db.String(180), nullable=False, index=True)
    phone = db.Column(db.String(40))
    address = db.Column(db.Text)
    user = db.relationship("User", back_populates="customer")
    quotations = db.relationship("Quotation", back_populates="customer")
    subscriptions = db.relationship("Subscription", back_populates="customer")
    invoices = db.relationship("Invoice", back_populates="customer")


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False, index=True)
    product_type = db.Column(db.String(60), nullable=False, default="service", index=True)
    sales_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    cost_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    notes = db.Column(db.Text)
    variants = db.relationship("ProductVariant", back_populates="product", cascade="all, delete-orphan")
    plan_links = db.relationship("PlanProduct", back_populates="product")
    quotation_items = db.relationship("QuotationItem", back_populates="product")
    subscription_items = db.relationship("SubscriptionItem", back_populates="product")
    invoice_items = db.relationship("InvoiceItem", back_populates="product")
    __table_args__ = (CheckConstraint("sales_price >= 0 AND cost_price >= 0", name="ck_product_prices_nonnegative"),)


class ProductVariant(db.Model):
    __tablename__ = "product_variants"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    variant_name = db.Column(db.String(120), nullable=False)
    additional_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    product = db.relationship("Product", back_populates="variants")
    __table_args__ = (CheckConstraint("additional_price >= 0", name="ck_variant_price_nonnegative"),)


class Plan(db.Model):
    __tablename__ = "plans"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    billing_cycle = db.Column(db.String(40), nullable=False)
    price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    minimum_quantity = db.Column(db.Integer, nullable=False, default=1)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    auto_close = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    products = db.relationship("PlanProduct", back_populates="plan", cascade="all, delete-orphan")
    subscriptions = db.relationship("Subscription", back_populates="plan")
    __table_args__ = (CheckConstraint("price >= 0 AND minimum_quantity > 0", name="ck_plan_price_quantity"),)


class PlanProduct(db.Model):
    __tablename__ = "plan_products"
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    plan = db.relationship("Plan", back_populates="products")
    product = db.relationship("Product", back_populates="plan_links")
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_plan_product_quantity_positive"), Index("ix_plan_products_pair", "plan_id", "product_id", unique=True))


class Tax(db.Model):
    __tablename__ = "taxes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    rate = db.Column(db.Numeric(7, 4), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    __table_args__ = (CheckConstraint("rate >= 0 AND rate <= 100", name="ck_tax_rate_range"),)


class Discount(db.Model):
    __tablename__ = "discounts"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, index=True)
    discount_type = db.Column(db.String(30), nullable=False)
    value = db.Column(db.Numeric(12, 2), nullable=False)
    minimum_quantity = db.Column(db.Integer, nullable=False, default=1)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    usage_limit = db.Column(db.Integer)
    usage_count = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("value >= 0 AND minimum_quantity > 0 AND usage_count >= 0", name="ck_discount_values_nonnegative"),)


class Quotation(db.Model):
    __tablename__ = "quotations"
    id = db.Column(db.Integer, primary_key=True)
    quotation_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    template_name = db.Column(db.String(120))
    valid_until = db.Column(db.Date)
    status = db.Column(db.String(30), nullable=False, default="draft", index=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    customer = db.relationship("Customer", back_populates="quotations")
    items = db.relationship("QuotationItem", back_populates="quotation", cascade="all, delete-orphan")
    converted_subscription = db.relationship(
        "Subscription", back_populates="source_quotation", uselist=False, passive_deletes="all"
    )


class QuotationItem(db.Model):
    __tablename__ = "quotation_items"
    id = db.Column(db.Integer, primary_key=True)
    quotation_id = db.Column(db.Integer, db.ForeignKey("quotations.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    tax_rate = db.Column(db.Numeric(7, 4), nullable=False, default=Decimal("0.0000"))
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    quotation = db.relationship("Quotation", back_populates="items")
    product = db.relationship("Product", back_populates="quotation_items")
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price >= 0 AND tax_rate >= 0 AND amount >= 0", name="ck_quotation_item_values"),)


class QuotationTemplate(db.Model):
    """Reusable quotation defaults; items snapshot product pricing at template edit time."""
    __tablename__ = "quotation_templates"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    items = db.relationship("QuotationTemplateItem", back_populates="template", cascade="all, delete-orphan")


class QuotationTemplateItem(db.Model):
    __tablename__ = "quotation_template_items"
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("quotation_templates.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    tax_rate = db.Column(db.Numeric(7, 4), nullable=False, default=Decimal("0.0000"))
    template = db.relationship("QuotationTemplate", back_populates="items")
    product = db.relationship("Product")
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price >= 0 AND tax_rate >= 0", name="ck_quotation_template_item_values"),)


class Subscription(db.Model):
    __tablename__ = "subscriptions"
    id = db.Column(db.Integer, primary_key=True)
    subscription_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_quotation_id = db.Column(db.Integer, db.ForeignKey("quotations.id", ondelete="RESTRICT"), nullable=True)
    # Snapshot the agreed recurring plan price and tax so later catalog changes
    # cannot alter historical subscription pricing.
    plan_price = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    plan_tax_rate = db.Column(db.Numeric(7, 4), nullable=False, default=Decimal("0.0000"))
    plan_quantity = db.Column(db.Integer, nullable=False, default=1)
    start_date = db.Column(db.Date, nullable=False, default=date.today)
    expiry_date = db.Column(db.Date)
    payment_terms = db.Column(db.String(80))
    status = db.Column(db.String(30), nullable=False, default="draft", index=True)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    customer = db.relationship("Customer", back_populates="subscriptions")
    plan = db.relationship("Plan", back_populates="subscriptions")
    source_quotation = db.relationship("Quotation", back_populates="converted_subscription")
    items = db.relationship("SubscriptionItem", back_populates="subscription", cascade="all, delete-orphan")
    invoices = db.relationship("Invoice", back_populates="subscription")
    renewals = db.relationship("SubscriptionRenewal", back_populates="subscription", passive_deletes="all")
    __table_args__ = (CheckConstraint(
        "plan_price >= 0 AND plan_tax_rate >= 0 AND plan_tax_rate <= 100 AND plan_quantity > 0",
        name="ck_subscription_plan_pricing",
    ), Index("uq_subscriptions_source_quotation", "source_quotation_id", unique=True))


class SubscriptionRenewal(db.Model):
    """Immutable record of a successful renewal period."""
    __tablename__ = "subscription_renewals"
    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False, index=True)
    previous_expiry_date = db.Column(db.Date, nullable=False)
    new_expiry_date = db.Column(db.Date, nullable=False)
    renewed_at = db.Column(db.Date, nullable=False, default=date.today)
    subscription = db.relationship("Subscription", back_populates="renewals")
    __table_args__ = (
        CheckConstraint("new_expiry_date > previous_expiry_date", name="ck_subscription_renewal_dates"),
        Index("uq_subscription_renewal_period", "subscription_id", "previous_expiry_date", unique=True),
    )


class SubscriptionItem(db.Model):
    __tablename__ = "subscription_items"
    id = db.Column(db.Integer, primary_key=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    tax_rate = db.Column(db.Numeric(7, 4), nullable=False, default=Decimal("0.0000"))
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    subscription = db.relationship("Subscription", back_populates="items")
    product = db.relationship("Product", back_populates="subscription_items")
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price >= 0 AND tax_rate >= 0 AND amount >= 0", name="ck_subscription_item_values"),)


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), nullable=False, unique=True, index=True)
    subscription_id = db.Column(db.Integer, db.ForeignKey("subscriptions.id", ondelete="RESTRICT"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    invoice_date = db.Column(db.Date, nullable=False, default=date.today)
    due_date = db.Column(db.Date)
    subtotal = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax_amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    discount_amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    status = db.Column(db.String(30), nullable=False, default="draft", index=True)
    subscription = db.relationship("Subscription", back_populates="invoices")
    customer = db.relationship("Customer", back_populates="invoices")
    items = db.relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    payments = db.relationship("Payment", back_populates="invoice", cascade="all, delete-orphan")
    __table_args__ = (CheckConstraint("subtotal >= 0 AND tax_amount >= 0 AND discount_amount >= 0 AND total_amount >= 0", name="ck_invoice_totals_nonnegative"),)


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    invoice = db.relationship("Invoice", back_populates="items")
    product = db.relationship("Product", back_populates="invoice_items")
    __table_args__ = (CheckConstraint("quantity > 0 AND unit_price >= 0 AND amount >= 0", name="ck_invoice_item_values"),)


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True)
    payment_method = db.Column(db.String(50), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=date.today)
    note = db.Column(db.Text)
    invoice = db.relationship("Invoice", back_populates="payments")
    __table_args__ = (CheckConstraint("amount > 0", name="ck_payment_amount_positive"),)
