"""Invoice generation and financial calculations for subscription billing."""

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import Discount, Invoice, InvoiceItem, Payment, Subscription

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


class BillingError(ValueError):
    """Raised when an invoice cannot be generated or calculated safely."""


def _money(value):
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise BillingError("A billing amount is invalid.")
    if not result.is_finite():
        raise BillingError("A billing amount must be finite.")
    return result


def _add_cycle(start, cycle):
    cycle = (cycle or "").strip().casefold()
    if cycle == "daily":
        return start + timedelta(days=1)
    if cycle == "weekly":
        return start + timedelta(days=7)
    if cycle == "monthly":
        year = start.year + (start.month // 12)
        month = start.month % 12 + 1
        return start.replace(year=year, month=month, day=min(start.day, monthrange(year, month)[1]))
    if cycle == "yearly":
        try:
            return start.replace(year=start.year + 1)
        except ValueError:
            return start.replace(year=start.year + 1, day=28)
    raise BillingError("The subscription has an unsupported billing frequency.")


def _due_date(invoice_date, terms):
    text = (terms or "").strip().casefold()
    if not text:
        days = 30
    elif text in {"due on receipt", "due immediately"}:
        days = 0
    else:
        match = re.fullmatch(r"net\s*(\d+)", text)
        days = int(match.group(1)) if match else 30
    return invoice_date + timedelta(days=days)


def _eligible_discounts(subscription, invoice_date):
    discounts = db.session.scalars(select(Discount).order_by(Discount.id)).all()
    eligible = []
    for discount in discounts:
        if not discount.is_active:
            continue
        if discount.start_date and invoice_date < discount.start_date:
            continue
        if discount.end_date and invoice_date > discount.end_date:
            continue
        if discount.usage_limit is not None and discount.usage_count >= discount.usage_limit:
            continue
        allowed_ids = {product.id for product in discount.products}
        lines = [item for item in subscription.items if not allowed_ids or item.product_id in allowed_ids]
        if not lines or sum(item.quantity for item in lines) < discount.minimum_quantity:
            continue
        purchase_total = sum(
            (_money(item.unit_price) * int(item.quantity) for item in subscription.items), ZERO
        )
        if purchase_total < _money(discount.minimum_purchase_amount):
            continue
        value = _money(discount.value)
        kind = (discount.discount_type or "").strip().casefold()
        if kind in {"percentage", "percent"}:
            if value > Decimal("100.00"):
                raise BillingError(f"Discount {discount.name!r} exceeds 100 percent.")
            base = sum((_money(item.unit_price) * item.quantity for item in lines), ZERO)
            saving = _money(base * value / Decimal("100"))
        elif kind in {"fixed", "fixed_amount"}:
            base = sum((_money(item.unit_price) * item.quantity for item in lines), ZERO)
            saving = min(value, base)
        else:
            raise BillingError(f"Discount {discount.name!r} has an unsupported type.")
        eligible.append((saving, discount.id, discount, lines))
    return eligible


def _allocate_discount(lines, discount, eligible_lines):
    """Return each line's discounted taxable base, preserving cents exactly."""
    bases = {item.id: _money(item.unit_price) * item.quantity for item in lines}
    eligible_ids = {item.id for item in eligible_lines}
    eligible_base = sum((bases[item.id] for item in eligible_lines), ZERO)
    amount = min(discount, eligible_base)
    allocations = {item.id: ZERO for item in lines}
    if amount == ZERO or eligible_base == ZERO:
        return {item.id: bases[item.id] for item in lines}
    remaining = amount
    eligible_lines = list(eligible_lines)
    for item in eligible_lines[:-1]:
        share = _money(amount * bases[item.id] / eligible_base)
        share = min(share, bases[item.id], remaining)
        allocations[item.id] = share
        remaining -= share
    allocations[eligible_lines[-1].id] = min(remaining, bases[eligible_lines[-1].id])
    return {item.id: bases[item.id] - allocations[item.id] for item in lines}


def _invoice_number(today):
    return f"INV-{today:%Y%m%d}-{uuid4().hex[:10].upper()}"


def generate_invoice(subscription_id):
    """Generate the earliest due, not-yet-invoiced period for an active subscription."""
    try:
        subscription_id = int(subscription_id)
    except (TypeError, ValueError):
        raise BillingError("A valid subscription is required.")
    subscription = db.session.get(Subscription, subscription_id)
    if subscription is None:
        raise BillingError("The subscription does not exist.")
    if (subscription.status or "").casefold() != "active":
        raise BillingError("Only active subscriptions can generate invoices.")
    if subscription.customer is None:
        raise BillingError("The subscription has no customer.")
    if not subscription.items:
        raise BillingError("The subscription has no invoiceable product lines.")
    if subscription.start_date is None:
        raise BillingError("The subscription has no billing start date.")

    cycle = (subscription.plan.billing_cycle or "").strip().casefold() if subscription.plan else ""
    # Validate the frequency before querying periods.
    _add_cycle(subscription.start_date, cycle)
    invoice_date = date.today()

    existing = db.session.scalars(select(Invoice).where(
        Invoice.subscription_id == subscription.id,
    ).order_by(Invoice.billing_period_start, Invoice.id)).all()
    if any(row.billing_period_start is None or row.billing_period_end is None for row in existing):
        raise BillingError(
            "This subscription has a legacy invoice without a billing period; reconcile it before generating another."
        )
    existing_periods = {(row.billing_period_start, row.billing_period_end) for row in existing}

    period_start = subscription.start_date
    while period_start <= invoice_date:
        period_end = _add_cycle(period_start, cycle) - timedelta(days=1)
        if (period_start, period_end) not in existing_periods:
            break
        period_start = period_end + timedelta(days=1)
    else:
        raise BillingError("The next billing period has not started yet.")

    line_bases = []
    for item in subscription.items:
        try:
            quantity = int(item.quantity)
            unit_price = _money(item.unit_price)
            tax_rate = Decimal(str(item.tax_rate))
        except (TypeError, ValueError, InvalidOperation):
            raise BillingError("A subscription line has invalid quantity, price, or tax.")
        if quantity <= 0 or unit_price < 0 or not tax_rate.is_finite() or tax_rate < 0 or tax_rate > 100:
            raise BillingError("A subscription line has invalid quantity, price, or tax.")
        if item.product is None:
            raise BillingError("A subscription line references a missing product.")
        line_bases.append((item, unit_price, quantity, tax_rate))

    discounts = _eligible_discounts(subscription, invoice_date)
    selected = max(discounts, key=lambda candidate: (candidate[0], -candidate[1])) if discounts else None
    selected_discount = selected[0] if selected else ZERO
    eligible_lines = selected[3] if selected else []
    taxable_bases = _allocate_discount(
        [row[0] for row in line_bases], selected_discount, eligible_lines
    ) if selected else {row[0].id: row[1] * row[2] for row in line_bases}

    invoice = Invoice(
        invoice_number=_invoice_number(invoice_date),
        subscription=subscription,
        customer=subscription.customer,
        invoice_date=invoice_date,
        due_date=_due_date(invoice_date, subscription.payment_terms),
        billing_period_start=period_start,
        billing_period_end=period_end,
        status="draft",
    )
    invoice.items = []
    for item, unit_price, quantity, tax_rate in line_bases:
        invoice.items.append(InvoiceItem(
            product=item.product,
            description=item.product.name,
            quantity=quantity,
            unit_price=unit_price,
            tax_rate=tax_rate,
            # amount is the discounted pre-tax line base; tax is stored on Invoice.
            amount=taxable_bases[item.id],
        ))
    invoice.discount_amount = selected_discount
    calculate_invoice_totals(invoice)
    db.session.add(invoice)
    if selected:
        selected[2].usage_count += 1
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise BillingError("An invoice already exists for this subscription billing period.") from exc
    except Exception:
        db.session.rollback()
        raise
    return invoice


def calculate_invoice_totals(invoice):
    """Set invoice totals from snapshotted lines; returns the totals as Decimals."""
    subtotal = ZERO
    tax_amount = ZERO
    discounted_base = ZERO
    for item in invoice.items:
        quantity = int(item.quantity)
        if quantity <= 0:
            raise BillingError("Invoice line quantities must be positive.")
        unit_price = _money(item.unit_price)
        line_base = _money(item.amount)
        if item.tax_rate is None:
            raise BillingError("This invoice line has no tax-rate snapshot and cannot be recalculated safely.")
        rate = Decimal(str(item.tax_rate))
        if unit_price < 0 or line_base < 0 or not rate.is_finite() or rate < 0 or rate > 100:
            raise BillingError("Invoice lines contain invalid financial values.")
        subtotal += unit_price * quantity
        discounted_base += line_base
        tax_amount += _money(line_base * rate / Decimal("100"))
    subtotal = _money(subtotal)
    tax_amount = _money(tax_amount)
    discount_amount = _money(invoice.discount_amount or ZERO)
    if discount_amount < 0 or discount_amount > subtotal:
        raise BillingError("Invoice discount is outside the subtotal range.")
    invoice.subtotal = subtotal
    invoice.tax_amount = tax_amount
    invoice.discount_amount = discount_amount
    invoice.total_amount = _money(discounted_base + tax_amount)
    return {
        "subtotal": invoice.subtotal,
        "tax_amount": invoice.tax_amount,
        "discount_amount": invoice.discount_amount,
        "total_amount": invoice.total_amount,
    }


def calculate_paid_amount(invoice_id):
    try:
        invoice_id = int(invoice_id)
    except (TypeError, ValueError):
        raise BillingError("A valid invoice is required.")
    if db.session.get(Invoice, invoice_id) is None:
        raise BillingError("The invoice does not exist.")
    payments = db.session.scalars(select(Payment.amount).where(Payment.invoice_id == invoice_id)).all()
    return _money(sum((_money(amount) for amount in payments), ZERO))


def calculate_balance_due(invoice_id):
    try:
        invoice_id = int(invoice_id)
    except (TypeError, ValueError):
        raise BillingError("A valid invoice is required.")
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        raise BillingError("The invoice does not exist.")
    balance = _money(invoice.total_amount) - calculate_paid_amount(invoice_id)
    return max(_money(balance), ZERO)


def is_invoice_overdue(invoice):
    if invoice is None:
        raise BillingError("An invoice is required.")
    return bool(invoice.status != "cancelled" and invoice.due_date and invoice.due_date < date.today()
                and calculate_balance_due(invoice.id) > ZERO)


PAYMENT_METHODS = {"cash", "upi", "card", "bank_transfer"}


def record_payment(invoice_id, amount, method, payment_date=None,
                   transaction_reference=None, note=None, submission_key=None):
    """Record one successful manual payment atomically and return its row."""
    try:
        invoice_id = int(invoice_id)
    except (TypeError, ValueError):
        raise BillingError("A valid invoice is required.")

    try:
        raw_amount = Decimal(str(amount))
        payment_amount = raw_amount.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise BillingError("Enter a valid payment amount.")
    if not raw_amount.is_finite() or raw_amount != payment_amount or payment_amount <= ZERO:
        raise BillingError("Payment amount must be positive and use no more than two decimal places.")

    normalized_method = (method or "").strip().casefold().replace(" ", "_")
    if normalized_method not in PAYMENT_METHODS:
        raise BillingError("Select a supported payment method.")
    if isinstance(payment_date, str):
        try:
            payment_date = date.fromisoformat(payment_date)
        except ValueError:
            raise BillingError("Enter a valid payment date.")
    if payment_date is None:
        payment_date = date.today()
    if not isinstance(payment_date, date) or isinstance(payment_date, datetime):
        raise BillingError("Enter a valid payment date.")
    key = (submission_key or "").strip()
    if not key or len(key) > 64:
        raise BillingError("A valid payment submission key is required.")
    reference = (transaction_reference or "").strip() or None
    note = (note or "").strip() or None
    if reference and len(reference) > 120:
        raise BillingError("Transaction reference is too long.")
    if note and len(note) > 2000:
        raise BillingError("Payment note is too long.")

    invoice = db.session.scalar(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    if invoice is None:
        raise BillingError("The invoice does not exist.")
    if db.session.scalar(select(Payment.id).where(Payment.submission_key == key)) is not None:
        raise BillingError("This payment submission has already been processed.")
    if invoice.status == "cancelled":
        raise BillingError("Payments cannot be recorded against cancelled invoices.")
    if invoice.status == "draft":
        raise BillingError("Confirm the invoice before recording a payment.")
    if invoice.status not in {"confirmed", "paid"}:
        raise BillingError("This invoice is not eligible for payment.")

    balance = calculate_balance_due(invoice_id)
    if payment_amount > balance:
        raise BillingError("Payment amount cannot exceed the outstanding balance.")

    payment = Payment(
        invoice=invoice,
        amount=payment_amount,
        payment_method=normalized_method,
        payment_date=payment_date,
        transaction_reference=reference,
        submission_key=key,
        note=note,
    )
    db.session.add(payment)
    new_balance = _money(balance - payment_amount)
    invoice.status = "paid" if new_balance == ZERO else "confirmed"
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise BillingError("This payment submission has already been processed.") from exc
    except Exception:
        db.session.rollback()
        raise
    return payment
