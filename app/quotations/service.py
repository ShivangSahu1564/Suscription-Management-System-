from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (
    Customer, Product, Quotation, QuotationItem, QuotationTemplate,
    QuotationTemplateItem,
)
from ..subscriptions.service import SubscriptionError, convert_quotation

CENT = Decimal("0.01")


class QuotationError(ValueError):
    pass


def money(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def quotation_number():
    return f"Q-{date.today():%Y%m%d}-{uuid4().hex[:8].upper()}"


def parse_lines(form):
    product_ids = form.getlist("product_id")
    quantities = form.getlist("quantity")
    tax_rates = form.getlist("tax_rate")
    if not product_ids or len(product_ids) != len(quantities) or len(product_ids) != len(tax_rates):
        raise QuotationError("Add at least one complete product line.")
    lines = []
    for product_id, raw_quantity, raw_tax in zip(product_ids, quantities, tax_rates):
        try:
            product = db.session.get(Product, int(product_id))
            quantity = int(raw_quantity)
            tax_rate = Decimal(raw_tax or "0")
        except (ValueError, TypeError, InvalidOperation):
            raise QuotationError("Product, quantity, or tax rate is invalid.")
        if not tax_rate.is_finite():
            raise QuotationError("Tax rate must be a finite number.")
        if product is None:
            raise QuotationError("A selected product no longer exists.")
        if quantity <= 0:
            raise QuotationError("Quantities must be greater than zero.")
        if tax_rate < 0 or tax_rate > 100:
            raise QuotationError("Tax rate must be between 0 and 100 percent.")
        try:
            unit_price = money(product.sales_price)
        except (InvalidOperation, TypeError, ValueError):
            raise QuotationError("The selected product has an invalid price.")
        if not unit_price.is_finite() or unit_price < 0:
            raise QuotationError("Product prices cannot be negative.")
        amount = money(unit_price * quantity * (Decimal("1") + tax_rate / Decimal("100")))
        lines.append({"product": product, "quantity": quantity, "unit_price": unit_price,
                      "tax_rate": tax_rate, "amount": amount})
    return lines


def create_quotation(form):
    try:
        customer_id = int(form.get("customer_id", ""))
        customer = db.session.get(Customer, customer_id)
    except (ValueError, TypeError):
        customer = None
    if customer is None:
        raise QuotationError("Select a valid customer.")
    lines = parse_lines(form)
    valid_until = parse_expiry(form.get("valid_until", ""))
    template_name = form.get("template_name", "").strip() or None
    status = "draft" if form.get("save_as_draft") else "quotation"
    quote = Quotation(quotation_number=quotation_number(), customer=customer,
                      template_name=template_name, valid_until=valid_until, status=status)
    quote.items = [QuotationItem(product=line["product"], quantity=line["quantity"],
                                 unit_price=line["unit_price"], tax_rate=line["tax_rate"],
                                 amount=line["amount"]) for line in lines]
    quote.total_amount = money(sum((line["amount"] for line in lines), Decimal("0")))
    db.session.add(quote)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise QuotationError("Could not save this quotation. Please try again.")
    return quote


def update_quotation(quote, form):
    if quote.status != "draft":
        raise QuotationError("Only draft quotations can be edited.")
    try:
        customer = db.session.get(Customer, int(form.get("customer_id", "")))
    except (ValueError, TypeError):
        customer = None
    if customer is None:
        raise QuotationError("Select a valid customer.")
    lines = parse_lines(form)
    quote.customer = customer
    quote.valid_until = parse_expiry(form.get("valid_until", ""))
    quote.template_name = form.get("template_name", "").strip() or None
    quote.items.clear()
    quote.items.extend(QuotationItem(product=line["product"], quantity=line["quantity"],
                                     unit_price=line["unit_price"], tax_rate=line["tax_rate"],
                                     amount=line["amount"]) for line in lines)
    quote.total_amount = money(sum((line["amount"] for line in lines), Decimal("0")))
    db.session.commit()
    return quote


def parse_expiry(raw):
    try:
        expiry = date.fromisoformat(raw)
    except (ValueError, TypeError):
        raise QuotationError("Enter a valid quotation expiry date.")
    if expiry < date.today():
        raise QuotationError("Quotation expiry must be today or later.")
    return expiry


def set_status(quote, status):
    if status == "confirmed":
        if quote.status not in {"quotation", "draft"}:
            raise QuotationError("This quotation cannot be confirmed in its current status.")
        if quote.valid_until and quote.valid_until < date.today():
            quote.status = "expired"
            db.session.commit()
            raise QuotationError("This quotation has expired.")
    elif status == "cancelled":
        if quote.status not in {"quotation", "draft"}:
            raise QuotationError("This quotation cannot be cancelled in its current status.")
    else:
        raise QuotationError("Unsupported quotation status change.")
    quote.status = status
    db.session.commit()


def convert_to_subscription(quote, plan):
    try:
        return convert_quotation(quote, plan)
    except SubscriptionError as exc:
        raise QuotationError(str(exc)) from exc


def save_template(name, form, template=None):
    name = name.strip()
    if not name:
        raise QuotationError("Template name is required.")
    lines = parse_lines(form)
    if template is None:
        template = QuotationTemplate(name=name)
        db.session.add(template)
    else:
        template.name = name
        template.items.clear()
    template.items.extend(QuotationTemplateItem(
        product_id=line["product"].id, quantity=line["quantity"],
        unit_price=line["unit_price"], tax_rate=line["tax_rate"],
    ) for line in lines)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise QuotationError("A template with that name already exists.")
    return template
