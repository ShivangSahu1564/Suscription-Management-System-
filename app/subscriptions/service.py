from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Customer, Plan, Product, Quotation, Subscription, SubscriptionItem

CENT = Decimal("0.01")
BILLING_CYCLES = {"daily", "weekly", "monthly", "yearly"}
SUBSCRIPTION_STATUSES = {"draft", "quotation", "confirmed", "active", "closed", "paused"}


class SubscriptionError(ValueError):
    pass


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def subscription_number():
    return f"SUB-{date.today():%Y%m%d}-{uuid4().hex[:8].upper()}"


def _parse_lines(form):
    product_ids = form.getlist("product_id")
    quantities = form.getlist("quantity")
    tax_rates = form.getlist("tax_rate")
    if not product_ids or len(product_ids) != len(quantities) or len(product_ids) != len(tax_rates):
        raise SubscriptionError("Add at least one complete product line.")
    result = []
    for raw_id, raw_quantity, raw_tax in zip(product_ids, quantities, tax_rates):
        try:
            product = db.session.get(Product, int(raw_id))
            quantity = int(raw_quantity)
            tax_rate = Decimal(raw_tax or "0")
        except (ValueError, TypeError, InvalidOperation):
            raise SubscriptionError("Product, quantity, or tax rate is invalid.")
        if not tax_rate.is_finite():
            raise SubscriptionError("Tax rate must be a finite number.")
        if product is None:
            raise SubscriptionError("A selected product does not exist.")
        if quantity <= 0:
            raise SubscriptionError("Quantities must be greater than zero.")
        if tax_rate < 0 or tax_rate > 100:
            raise SubscriptionError("Tax rate must be between 0 and 100 percent.")
        try:
            price = money(product.sales_price)
        except (InvalidOperation, TypeError, ValueError):
            raise SubscriptionError("The selected product has an invalid price.")
        if not price.is_finite() or price < 0:
            raise SubscriptionError("Product prices cannot be negative.")
        amount = money(price * quantity * (Decimal("1") + tax_rate / Decimal("100")))
        result.append({"product": product, "quantity": quantity, "unit_price": price,
                       "tax_rate": tax_rate, "amount": amount})
    return result


def _parse_dates(raw_start, raw_expiry):
    try:
        start = date.fromisoformat(raw_start or "")
        expiry = date.fromisoformat(raw_expiry or "")
    except (ValueError, TypeError):
        raise SubscriptionError("Enter valid subscription dates.")
    if expiry <= start:
        raise SubscriptionError("Expiry date must be after the start date.")
    return start, expiry


def _validate_plan(plan):
    if plan is None or not plan.is_active:
        raise SubscriptionError("Select an active subscription plan.")
    cycle = (plan.billing_cycle or "").casefold()
    if cycle not in BILLING_CYCLES:
        raise SubscriptionError("The plan billing cycle must be daily, weekly, monthly, or yearly.")
    return cycle


def _total(lines):
    return money(sum((line["amount"] for line in lines), Decimal("0.00")))


def create_subscription(form):
    try:
        customer = db.session.get(Customer, int(form.get("customer_id", "")))
        plan = db.session.get(Plan, int(form.get("plan_id", "")))
    except (ValueError, TypeError):
        customer = plan = None
    if customer is None:
        raise SubscriptionError("Select a valid customer.")
    _validate_plan(plan)
    lines = _parse_lines(form)
    start, expiry = _parse_dates(form.get("start_date"), form.get("expiry_date"))
    payment_terms = (form.get("payment_terms") or "").strip()
    if len(payment_terms) > 80:
        raise SubscriptionError("Payment terms must be 80 characters or fewer.")
    status = "draft" if form.get("save_as_draft") else "quotation"
    subscription = Subscription(
        subscription_number=subscription_number(), customer=customer, plan=plan,
        start_date=start, expiry_date=expiry, payment_terms=payment_terms or None,
        status=status, total_amount=_total(lines),
    )
    subscription.items = [SubscriptionItem(
        product=line["product"], quantity=line["quantity"], unit_price=line["unit_price"],
        tax_rate=line["tax_rate"], amount=line["amount"],
    ) for line in lines]
    db.session.add(subscription)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise SubscriptionError("Could not save the subscription. Please try again.")
    return subscription


def update_subscription(subscription, form):
    if subscription.status != "draft":
        raise SubscriptionError("Only draft subscriptions can be edited.")
    try:
        customer = db.session.get(Customer, int(form.get("customer_id", "")))
        plan = db.session.get(Plan, int(form.get("plan_id", "")))
    except (ValueError, TypeError):
        customer = plan = None
    if customer is None:
        raise SubscriptionError("Select a valid customer.")
    _validate_plan(plan)
    lines = _parse_lines(form)
    start, expiry = _parse_dates(form.get("start_date"), form.get("expiry_date"))
    terms = (form.get("payment_terms") or "").strip()
    if len(terms) > 80:
        raise SubscriptionError("Payment terms must be 80 characters or fewer.")
    subscription.customer = customer
    subscription.plan = plan
    subscription.start_date = start
    subscription.expiry_date = expiry
    subscription.payment_terms = terms or None
    subscription.items.clear()
    subscription.items.extend(SubscriptionItem(
        product=line["product"], quantity=line["quantity"], unit_price=line["unit_price"],
        tax_rate=line["tax_rate"], amount=line["amount"],
    ) for line in lines)
    subscription.total_amount = _total(lines)
    db.session.commit()
    return subscription


def transition(subscription, action):
    allowed = {
        "quote": {"draft"},
        "confirm": {"quotation"},
        "activate": {"confirmed"},
        "pause": {"active"},
        "resume": {"paused"},
        "close": {"draft", "quotation", "confirmed", "active", "paused"},
    }
    target = {"quote": "quotation", "confirm": "confirmed", "activate": "active", "pause": "paused",
              "resume": "active", "close": "closed"}.get(action)
    if target is None or subscription.status not in allowed[action]:
        raise SubscriptionError("That status transition is not allowed.")
    if action == "activate" and subscription.start_date > date.today():
        raise SubscriptionError("A subscription cannot be activated before its start date.")
    if action in {"activate", "resume"} and subscription.expiry_date and subscription.expiry_date < date.today():
        subscription.status = "closed"
        db.session.commit()
        raise SubscriptionError("This subscription has expired and is now closed.")
    subscription.status = target
    db.session.commit()
    return subscription


def add_cycle(start, cycle):
    if cycle == "daily":
        return start + timedelta(days=1)
    if cycle == "weekly":
        return start + timedelta(weeks=1)
    if cycle == "monthly":
        year = start.year + (start.month // 12)
        month = start.month % 12 + 1
        return start.replace(year=year, month=month, day=min(start.day, monthrange(year, month)[1]))
    if cycle == "yearly":
        try:
            return start.replace(year=start.year + 1)
        except ValueError:  # Leap day renews to the last day of February.
            return start.replace(year=start.year + 1, day=28)
    raise SubscriptionError("Unsupported billing cycle.")


def renew(subscription):
    cycle = _validate_plan(subscription.plan)
    if subscription.status not in {"active", "paused", "closed"}:
        raise SubscriptionError("Only active, paused, or closed subscriptions can be renewed.")
    renewal_anchor = subscription.expiry_date if subscription.expiry_date and subscription.expiry_date >= date.today() else date.today()
    subscription.expiry_date = add_cycle(renewal_anchor, cycle)
    subscription.status = "active"
    db.session.commit()
    return subscription


def close_expired(subscription, today=None):
    today = today or date.today()
    if subscription.status in {"active", "paused", "confirmed"} and subscription.expiry_date and subscription.expiry_date < today:
        subscription.status = "closed"
        db.session.commit()
        return True
    return False


def convert_quotation(quotation, plan):
    if quotation.status == "converted":
        raise SubscriptionError("This quotation has already been converted.")
    if quotation.status != "confirmed":
        raise SubscriptionError("Only a confirmed quotation can be converted.")
    cycle = _validate_plan(plan)
    del cycle
    if not quotation.items:
        raise SubscriptionError("The quotation has no items to convert.")
    today = date.today()
    expiry = add_cycle(today, (plan.billing_cycle or "").casefold())
    subscription = Subscription(
        subscription_number=subscription_number(), customer_id=quotation.customer_id,
        plan_id=plan.id, start_date=today, expiry_date=expiry,
        payment_terms="As quoted", status="confirmed", total_amount=money(quotation.total_amount),
    )
    subscription.items = [SubscriptionItem(
        product_id=item.product_id, quantity=item.quantity, unit_price=money(item.unit_price),
        tax_rate=Decimal(item.tax_rate), amount=money(item.amount),
    ) for item in quotation.items]
    # The conditional update and inserts share one transaction. A second concurrent
    # conversion cannot match the confirmed status after the first commits.
    result = db.session.execute(update(Quotation).where(
        Quotation.id == quotation.id, Quotation.status == "confirmed",
    ).values(status="converted"))
    if result.rowcount != 1:
        db.session.rollback()
        raise SubscriptionError("This quotation has already been converted or is no longer confirmed.")
    db.session.add(subscription)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise SubscriptionError("Could not convert this quotation. Please try again.")
    return subscription


def billing_data(subscription):
    """Stable data shape for invoice/billing code; does not create an invoice."""
    return {
        "subscription_id": subscription.id,
        "subscription_number": subscription.subscription_number,
        "customer_id": subscription.customer_id,
        "plan": {"id": subscription.plan_id, "name": subscription.plan.name,
                 "billing_cycle": subscription.plan.billing_cycle},
        "items": [{"product_id": item.product_id, "product_name": item.product.name,
                   "quantity": item.quantity, "unit_price": str(item.unit_price),
                   "tax_rate": str(item.tax_rate), "amount": str(item.amount)}
                  for item in subscription.items],
        "start_date": subscription.start_date.isoformat() if subscription.start_date else None,
        "expiry_date": subscription.expiry_date.isoformat() if subscription.expiry_date else None,
        "total_amount": str(subscription.total_amount),
        "status": subscription.status,
    }
