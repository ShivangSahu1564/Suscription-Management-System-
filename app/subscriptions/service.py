from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Customer, Plan, Product, Quotation, Subscription, SubscriptionItem, SubscriptionRenewal

CENT = Decimal("0.01")
BILLING_CYCLES = {"daily", "weekly", "monthly", "yearly"}
SUBSCRIPTION_STATUSES = {"draft", "quotation", "confirmed", "active", "closed", "paused"}


class SubscriptionError(ValueError):
    pass


def money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def subscription_number():
    return f"SUB-{date.today():%Y%m%d}-{uuid4().hex[:8].upper()}"


def _parse_lines(form, existing_items=()):
    product_ids = form.getlist("product_id")
    quantities = form.getlist("quantity")
    tax_rates = form.getlist("tax_rate")
    prices = form.getlist("unit_price")
    item_ids = form.getlist("item_id")
    if len(product_ids) != len(quantities) or len(product_ids) != len(tax_rates):
        raise SubscriptionError("Each additional product line must be complete.")
    if prices and len(prices) != len(product_ids):
        raise SubscriptionError("Each product line must have a unit price.")
    existing_by_id = {item.id: item for item in existing_items if item.id is not None}
    unused_existing = list(existing_items)
    result = []
    for line_index, (raw_id, raw_quantity, raw_tax) in enumerate(zip(product_ids, quantities, tax_rates)):
        if not raw_id:
            if raw_quantity or raw_tax:
                raise SubscriptionError("Complete or remove each additional product line.")
            continue
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
        existing = None
        if item_ids and len(item_ids) == len(product_ids) and item_ids[line_index].strip():
            try:
                existing = existing_by_id.get(int(item_ids[line_index]))
            except ValueError:
                raise SubscriptionError("An edited subscription item is invalid.")
            if existing is None or existing.product_id != product.id:
                raise SubscriptionError("An edited subscription item does not belong to this subscription.")
        elif unused_existing:
            existing = next((item for item in unused_existing if item.product_id == product.id), None)
        if existing in unused_existing:
            unused_existing.remove(existing)
        try:
            raw_price = prices[line_index].strip() if prices else ""
            price = money(raw_price if raw_price else (existing.unit_price if existing else product.sales_price))
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


def _validate_plan(plan, start_date=None):
    if plan is None or not plan.is_active:
        raise SubscriptionError("Select an active subscription plan.")
    cycle = (plan.billing_cycle or "").casefold()
    if cycle not in BILLING_CYCLES:
        raise SubscriptionError("The plan billing cycle must be daily, weekly, monthly, or yearly.")
    if plan.price is None or not Decimal(str(plan.price)).is_finite() or Decimal(str(plan.price)) < 0:
        raise SubscriptionError("The plan price must be a finite non-negative amount.")
    if plan.start_date and plan.end_date and plan.end_date < plan.start_date:
        raise SubscriptionError("The plan availability dates are invalid.")
    if start_date and ((plan.start_date and start_date < plan.start_date)
                       or (plan.end_date and start_date > plan.end_date)):
        raise SubscriptionError("The plan is not available on the subscription start date.")
    return cycle


def _total(lines):
    return money(sum((line["amount"] for line in lines), Decimal("0.00")))


def _plan_values(plan, form, existing=None):
    try:
        raw_price = form.get("plan_price", "")
        price = money(raw_price if raw_price != "" else (existing.plan_price if existing else plan.price))
        tax_rate = Decimal(form.get("plan_tax_rate", str(existing.plan_tax_rate if existing else "0")) or "0")
        quantity = int(form.get("plan_quantity", str(existing.plan_quantity if existing else "1")))
    except (InvalidOperation, TypeError, ValueError):
        raise SubscriptionError("Plan price, quantity, or tax rate is invalid.")
    if not price.is_finite() or price < 0:
        raise SubscriptionError("Plan price cannot be negative.")
    if not tax_rate.is_finite() or tax_rate < 0 or tax_rate > 100:
        raise SubscriptionError("Plan tax rate must be between 0 and 100 percent.")
    if quantity < plan.minimum_quantity:
        raise SubscriptionError(f"Plan quantity must be at least {plan.minimum_quantity}.")
    return price, tax_rate, quantity


def _check_plan_products(plan, lines):
    included_ids = {link.product_id for link in plan.products}
    if any(line["product"].id in included_ids for line in lines):
        raise SubscriptionError("Products included in the selected plan cannot also be added as extra charges.")


def _subscription_total(plan_price, plan_tax, plan_quantity, lines):
    base = money(plan_price * plan_quantity)
    plan_amount = money(base * (Decimal("1") + plan_tax / Decimal("100")))
    return money(plan_amount + _total(lines))


def create_subscription(form):
    try:
        customer = db.session.get(Customer, int(form.get("customer_id", "")))
        plan = db.session.get(Plan, int(form.get("plan_id", "")))
    except (ValueError, TypeError):
        customer = plan = None
    if customer is None:
        raise SubscriptionError("Select a valid customer.")
    start, expiry = _parse_dates(form.get("start_date"), form.get("expiry_date"))
    _validate_plan(plan, start)
    lines = _parse_lines(form)
    _check_plan_products(plan, lines)
    plan_price, plan_tax, plan_quantity = _plan_values(plan, form)
    payment_terms = (form.get("payment_terms") or "").strip()
    if len(payment_terms) > 80:
        raise SubscriptionError("Payment terms must be 80 characters or fewer.")
    status = "draft" if form.get("save_as_draft") else "quotation"
    subscription = Subscription(
        subscription_number=subscription_number(), customer=customer, plan=plan,
        start_date=start, expiry_date=expiry, payment_terms=payment_terms or None,
        status=status, total_amount=_subscription_total(plan_price, plan_tax, plan_quantity, lines),
        plan_price=plan_price, plan_tax_rate=plan_tax, plan_quantity=plan_quantity,
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
    start, expiry = _parse_dates(form.get("start_date"), form.get("expiry_date"))
    _validate_plan(plan, start)
    lines = _parse_lines(form, subscription.items)
    _check_plan_products(plan, lines)
    plan_price, plan_tax, plan_quantity = _plan_values(plan, form, subscription if plan.id == subscription.plan_id else None)
    terms = (form.get("payment_terms") or "").strip()
    if len(terms) > 80:
        raise SubscriptionError("Payment terms must be 80 characters or fewer.")
    subscription.customer = customer
    subscription.plan = plan
    subscription.plan_price = plan_price
    subscription.plan_tax_rate = plan_tax
    subscription.plan_quantity = plan_quantity
    subscription.start_date = start
    subscription.expiry_date = expiry
    subscription.payment_terms = terms or None
    subscription.items.clear()
    subscription.items.extend(SubscriptionItem(
        product=line["product"], quantity=line["quantity"], unit_price=line["unit_price"],
        tax_rate=line["tax_rate"], amount=line["amount"],
    ) for line in lines)
    subscription.total_amount = _subscription_total(plan_price, plan_tax, plan_quantity, lines)
    db.session.commit()
    return subscription


def transition(subscription, action):
    close_expired(subscription)
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
    subscription.status = target
    db.session.commit()
    return subscription


def add_cycle(start, cycle, anchor_date=None):
    if cycle == "daily":
        return start + timedelta(days=1)
    if cycle == "weekly":
        return start + timedelta(weeks=1)
    if cycle == "monthly":
        year = start.year + (start.month // 12)
        month = start.month % 12 + 1
        anchor = anchor_date or start
        target_last_day = monthrange(year, month)[1]
        anchor_is_month_end = anchor.day == monthrange(anchor.year, anchor.month)[1]
        target_day = target_last_day if anchor_is_month_end else min(anchor.day, target_last_day)
        return start.replace(year=year, month=month, day=target_day)
    if cycle == "yearly":
        try:
            return start.replace(year=start.year + 1)
        except ValueError:  # Leap day renews to the last day of February.
            return start.replace(year=start.year + 1, day=28)
    raise SubscriptionError("Unsupported billing cycle.")


def renew(subscription, today=None):
    today = today or date.today()
    close_expired(subscription, today)
    if subscription.status != "active":
        raise SubscriptionError("Only active subscriptions can be renewed; paused or closed subscriptions must be resumed or replaced explicitly.")
    if subscription.expiry_date != today:
        raise SubscriptionError("A subscription can only be renewed once, on its expiry date.")
    cycle = _validate_plan(subscription.plan)
    old_expiry = subscription.expiry_date
    new_expiry = add_cycle(old_expiry, cycle, anchor_date=subscription.start_date)
    result = db.session.execute(update(Subscription).where(
        Subscription.id == subscription.id,
        Subscription.status == "active",
        Subscription.expiry_date == old_expiry,
    ).values(expiry_date=new_expiry).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.session.rollback()
        raise SubscriptionError("This subscription was already renewed or is no longer eligible.")
    db.session.add(SubscriptionRenewal(
        subscription_id=subscription.id, previous_expiry_date=old_expiry,
        new_expiry_date=new_expiry, renewed_at=today,
    ))
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise SubscriptionError("This subscription period has already been renewed.") from exc
    db.session.refresh(subscription)
    return subscription


def close_expired(subscription, today=None):
    today = today or date.today()
    result = db.session.execute(update(Subscription).where(
        Subscription.id == subscription.id,
        Subscription.status.in_({"active", "paused", "confirmed"}),
        Subscription.expiry_date.is_not(None),
        Subscription.expiry_date < today,
    ).values(status="closed"))
    if result.rowcount:
        db.session.commit()
        db.session.refresh(subscription)
        return True
    return False


def expire_subscriptions(today=None):
    """Close every expired nonterminal subscription using the shared policy."""
    today = today or date.today()
    result = db.session.execute(update(Subscription).where(
        Subscription.status.in_({"active", "paused", "confirmed"}),
        Subscription.expiry_date.is_not(None),
        Subscription.expiry_date < today,
    ).values(status="closed").execution_options(synchronize_session="fetch"))
    if result.rowcount:
        db.session.commit()
    return result.rowcount or 0


def convert_quotation(quotation, plan):
    if quotation.status == "converted":
        raise SubscriptionError("This quotation has already been converted.")
    if quotation.status != "confirmed":
        raise SubscriptionError("Only a confirmed quotation can be converted.")
    if not quotation.items:
        raise SubscriptionError("The quotation has no items to convert.")
    today = date.today()
    cycle = _validate_plan(plan, today)
    expiry = add_cycle(today, cycle, anchor_date=today)
    subscription = Subscription(
        subscription_number=subscription_number(), customer_id=quotation.customer_id,
        plan_id=plan.id, source_quotation_id=quotation.id, start_date=today, expiry_date=expiry,
        payment_terms="As quoted", status="confirmed", total_amount=money(quotation.total_amount),
        # A converted quotation is already the fully agreed total. Keep the plan
        # association for billing-cycle metadata without adding a second charge.
        plan_price=Decimal("0.00"), plan_tax_rate=Decimal("0.0000"), plan_quantity=1,
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
    close_expired(subscription)
    return {
        "subscription_id": subscription.id,
        "subscription_number": subscription.subscription_number,
        "customer_id": subscription.customer_id,
        "plan": {"id": subscription.plan_id, "name": subscription.plan.name,
                 "billing_cycle": subscription.plan.billing_cycle,
                 "unit_price": str(subscription.plan_price),
                 "quantity": subscription.plan_quantity,
                 "tax_rate": str(subscription.plan_tax_rate),
                 "amount": str(money(subscription.plan_price * subscription.plan_quantity
                                      * (Decimal("1") + subscription.plan_tax_rate / Decimal("100"))))},
        "items": [{"product_id": item.product_id, "product_name": item.product.name,
                   "quantity": item.quantity, "unit_price": str(item.unit_price),
                   "tax_rate": str(item.tax_rate), "amount": str(item.amount)}
                  for item in subscription.items],
        "start_date": subscription.start_date.isoformat() if subscription.start_date else None,
        "expiry_date": subscription.expiry_date.isoformat() if subscription.expiry_date else None,
        "total_amount": str(subscription.total_amount),
        "status": subscription.status,
    }
