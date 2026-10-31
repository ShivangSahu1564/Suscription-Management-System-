"""Invoice, payment, and financial reporting routes."""

from datetime import date, timedelta
from decimal import Decimal
import hmac
import secrets
from uuid import uuid4

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..auth import staff_required
from ..extensions import db
from ..models import Customer, Invoice, InvoiceItem, Payment, Subscription
from .service import (
    BillingError, calculate_balance_due, calculate_paid_amount, generate_invoice,
    is_invoice_overdue, record_payment,
)

bp = Blueprint("billing", __name__)
MANAGER_ROLES = {"admin", "staff"}
VIEWABLE_ROLES = MANAGER_ROLES | {"customer"}
PAYMENT_METHOD_LABELS = {
    "cash": "Cash", "upi": "UPI", "card": "Card", "bank_transfer": "Bank transfer",
}


def _is_manager():
    return current_user.is_authenticated and (current_user.role or "").casefold() in MANAGER_ROLES


def _require_view_role():
    if not current_user.is_authenticated or (current_user.role or "").casefold() not in VIEWABLE_ROLES:
        abort(403)


def _csrf_token():
    token = session.get("billing_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["billing_csrf_token"] = token
    return token


@bp.app_context_processor
def inject_billing_csrf_token():
    return {"billing_csrf_token": _csrf_token}


def _require_csrf():
    expected = session.get("billing_csrf_token", "")
    submitted = request.form.get("csrf_token", "")
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        abort(400, description="Invalid or missing CSRF token.")


@bp.before_request
def protect_billing_posts():
    if request.method == "POST" and current_user.is_authenticated:
        _require_csrf()


def _invoice_query():
    return select(Invoice).options(
        selectinload(Invoice.customer), selectinload(Invoice.subscription),
        selectinload(Invoice.items).selectinload(InvoiceItem.product),
        selectinload(Invoice.payments),
    )


def _visible_invoice(invoice_id):
    invoice = db.session.scalar(_invoice_query().where(Invoice.id == invoice_id))
    if invoice is None:
        abort(404)
    if not _is_manager():
        _require_view_role()
        if invoice.customer.user_id != current_user.id:
            abort(403)
    return invoice


def _visible_payment(payment_id):
    payment = db.session.scalar(
        select(Payment).options(selectinload(Payment.invoice).selectinload(Invoice.customer))
        .where(Payment.id == payment_id)
    )
    if payment is None:
        abort(404)
    if not _is_manager():
        _require_view_role()
        if payment.invoice.customer.user_id != current_user.id:
            abort(403)
    return payment


def _metrics(invoice):
    paid = calculate_paid_amount(invoice.id)
    balance = calculate_balance_due(invoice.id)
    overdue = is_invoice_overdue(invoice)
    if invoice.status == "draft":
        display_status = "Draft"
    elif invoice.status == "cancelled":
        display_status = "Cancelled"
    elif balance == 0 and paid > 0:
        display_status = "Paid"
    elif overdue:
        display_status = "Overdue"
    elif paid > 0:
        display_status = "Partially paid"
    elif balance > 0:
        display_status = "Unpaid"
    else:
        display_status = (invoice.status or "unknown").title()
    return {
        "invoice": invoice, "paid_amount": paid, "balance_due": balance,
        "is_overdue": overdue, "display_status": display_status,
        "payment_state": (
            "paid" if balance == 0 and paid > 0 else
            "partially_paid" if paid > 0 and balance > 0 else
            "unpaid" if balance > 0 and invoice.status not in {"draft", "cancelled"} else
            invoice.status
        ),
    }


def _visible_invoices():
    _require_view_role()
    statement = _invoice_query()
    if not _is_manager():
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == current_user.id))
        if customer_id is None:
            return []
        statement = statement.where(Invoice.customer_id == customer_id)
    return db.session.scalars(statement.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())).all()


def _date_window():
    today = date.today()
    period = request.args.get("period", "this_month").casefold()
    if period == "today":
        start = end = today
    elif period == "this_week":
        start, end = today - timedelta(days=today.weekday()), today
    elif period == "this_year":
        start, end = today.replace(month=1, day=1), today
    elif period == "custom":
        try:
            start = date.fromisoformat(request.args.get("start", ""))
            end = date.fromisoformat(request.args.get("end", ""))
            if start > end:
                raise ValueError
        except ValueError:
            period = "this_month"
            start, end = today.replace(day=1), today
    else:
        period = "this_month"
        start, end = today.replace(day=1), today
    return period, start, end


def _payment_scope_statement(start=None, end=None):
    statement = select(Payment).join(Invoice, Payment.invoice_id == Invoice.id)
    if not _is_manager():
        statement = statement.where(Invoice.customer_id == Customer.id).where(Customer.user_id == current_user.id)
        statement = statement.join(Customer, Invoice.customer_id == Customer.id)
    if start:
        statement = statement.where(Payment.payment_date >= start)
    if end:
        statement = statement.where(Payment.payment_date <= end)
    return statement


def _dashboard_data(invoices, start, end):
    in_range = [invoice for invoice in invoices if invoice.invoice_date and start <= invoice.invoice_date <= end]
    rows = [_metrics(invoice) for invoice in in_range]
    invoice_ids = [invoice.id for invoice in invoices]
    payment_rows = db.session.scalars(
        select(Payment).where(
            Payment.invoice_id.in_(invoice_ids) if invoice_ids else Payment.id == -1,
            Payment.payment_date >= start,
            Payment.payment_date <= end,
        ).order_by(Payment.payment_date, Payment.id)
    ).all()
    method_breakdown = {}
    monthly_totals = {}
    revenue_received = Decimal("0.00")
    for payment in payment_rows:
        method = PAYMENT_METHOD_LABELS.get(
            payment.payment_method,
            (payment.payment_method or "Other").replace("_", " ").title(),
        )
        data = method_breakdown.setdefault(method, {"count": 0, "amount": Decimal("0.00")})
        data["count"] += 1
        data["amount"] += Decimal(str(payment.amount))
        revenue_received += Decimal(str(payment.amount))
        month = payment.payment_date.strftime("%Y-%m")
        monthly_totals[month] = monthly_totals.get(month, Decimal("0.00")) + Decimal(str(payment.amount))
    monthly = sorted(monthly_totals.items())
    return {
        "total_invoices": len(rows),
        "paid_invoices": sum(row["display_status"] == "Paid" for row in rows),
        "outstanding_invoices": sum(row["balance_due"] > 0 and row["invoice"].status not in {"cancelled", "draft"} for row in rows),
        "overdue_invoices": sum(row["is_overdue"] for row in rows),
        "revenue_received": revenue_received,
        "total_outstanding": sum((row["balance_due"] for row in rows if row["invoice"].status not in {"cancelled", "draft"}), Decimal("0.00")),
        "payment_count": len(payment_rows),
        "method_breakdown": method_breakdown,
        "monthly_revenue": [(month, Decimal(str(amount or 0))) for month, amount in monthly],
        "unpaid_invoices": sum(row["payment_state"] == "unpaid" for row in rows),
        "partially_paid_invoices": sum(row["payment_state"] == "partially_paid" for row in rows),
        "cancelled_invoices": sum(row["invoice"].status == "cancelled" for row in rows),
        "cash_payments": method_breakdown.get("Cash", {}).get("amount", Decimal("0.00")),
        "upi_payments": method_breakdown.get("UPI", {}).get("amount", Decimal("0.00")),
        "card_payments": method_breakdown.get("Card", {}).get("amount", Decimal("0.00")),
        "bank_transfer_payments": method_breakdown.get("Bank transfer", {}).get("amount", Decimal("0.00")),
    }


def _badge_class(status):
    return {
        "Draft": "text-bg-secondary", "Confirmed": "text-bg-primary",
        "Paid": "text-bg-success", "Overdue": "text-bg-danger",
        "Cancelled": "text-bg-dark", "Unpaid": "text-bg-warning",
        "Partially paid": "text-bg-info",
    }.get(status, "text-bg-light")


@bp.get("/")
@login_required
def index():
    invoices = _visible_invoices()
    period, start, end = _date_window()
    summary = _dashboard_data(invoices, start, end)
    active_subscriptions = []
    if _is_manager():
        active_subscriptions = db.session.scalars(
            select(Subscription).where(Subscription.status == "active").order_by(Subscription.id.desc())
        ).all()
    return render_template("billing/dashboard.html", summary=summary,
                           active_subscriptions=active_subscriptions, can_manage=_is_manager(),
                           period=period, start=start, end=end)


@bp.get("/invoices")
@login_required
def invoices():
    all_invoices = _visible_invoices()
    search = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "all").casefold()
    if search:
        needle = search.casefold()
        all_invoices = [inv for inv in all_invoices if needle in inv.invoice_number.casefold()
                        or needle in inv.customer.company_name.casefold()]
    rows = [_metrics(invoice) for invoice in all_invoices]
    filters = {
        "paid": lambda row: row["display_status"] == "Paid",
        "unpaid": lambda row: row["payment_state"] == "unpaid",
        "partially_paid": lambda row: row["payment_state"] == "partially_paid",
        "overdue": lambda row: row["is_overdue"],
        "cancelled": lambda row: row["invoice"].status == "cancelled",
    }
    if status_filter in filters:
        rows = [row for row in rows if filters[status_filter](row)]
    elif status_filter == "draft":
        rows = [row for row in rows if row["invoice"].status == "draft"]
    elif status_filter == "confirmed":
        rows = [row for row in rows if row["invoice"].status == "confirmed"]
    elif status_filter != "all":
        status_filter = "all"
    return render_template("billing/invoices.html", invoice_rows=rows,
                           status_filter=status_filter, search=search,
                           can_manage=_is_manager(), badge_class=_badge_class)


@bp.get("/invoices/<int:invoice_id>")
@login_required
def detail(invoice_id):
    invoice = _visible_invoice(invoice_id)
    metrics = _metrics(invoice)
    return render_template("billing/invoice_detail.html", invoice=invoice, metrics=metrics,
                           can_manage=_is_manager(), badge_class=_badge_class,
                           payment_methods=PAYMENT_METHOD_LABELS,
                           new_submission_key=uuid4().hex, today_date=date.today())


@bp.get("/payments")
@login_required
def payment_list():
    _require_view_role()
    statement = _payment_scope_statement()
    payments = db.session.scalars(statement.options(
        selectinload(Payment.invoice).selectinload(Invoice.customer)
    ).order_by(Payment.payment_date.desc(), Payment.id.desc())).all()
    return render_template("billing/payments.html", payments=payments,
                           can_manage=_is_manager(), method_labels=PAYMENT_METHOD_LABELS)


@bp.get("/payments/<int:payment_id>")
@login_required
def payment_detail(payment_id):
    payment = _visible_payment(payment_id)
    return render_template("billing/payment_detail.html", payment=payment,
                           balance_due=calculate_balance_due(payment.invoice_id),
                           method_labels=PAYMENT_METHOD_LABELS)


@bp.get("/payments/<int:payment_id>/receipt")
@login_required
def payment_receipt(payment_id):
    payment = _visible_payment(payment_id)
    return render_template("billing/payment_receipt.html", payment=payment,
                           balance_due=calculate_balance_due(payment.invoice_id),
                           method_labels=PAYMENT_METHOD_LABELS)


@bp.get("/invoices/<int:invoice_id>/payments")
@login_required
def invoice_payments(invoice_id):
    invoice = _visible_invoice(invoice_id)
    return render_template("billing/payments.html", payments=invoice.payments,
                           invoice=invoice, can_manage=_is_manager(),
                           method_labels=PAYMENT_METHOD_LABELS)


@bp.post("/invoices/<int:invoice_id>/payments")
@staff_required
def create_payment(invoice_id):
    invoice = _visible_invoice(invoice_id)
    try:
        payment = record_payment(
            invoice_id=invoice.id, amount=request.form.get("amount"),
            method=request.form.get("payment_method"), payment_date=request.form.get("payment_date"),
            transaction_reference=request.form.get("transaction_reference"),
            note=request.form.get("note"), submission_key=request.form.get("submission_key"),
        )
    except BillingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("billing.detail", invoice_id=invoice.id))
    flash("Payment recorded successfully.", "success")
    return redirect(url_for("billing.payment_detail", payment_id=payment.id))


@bp.post("/invoices/generate/<int:subscription_id>")
@staff_required
def generate(subscription_id):
    try:
        invoice = generate_invoice(subscription_id)
    except BillingError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
        return redirect(url_for("billing.invoices"))
    flash(f"Invoice {invoice.invoice_number} generated as a draft.", "success")
    return redirect(url_for("billing.detail", invoice_id=invoice.id))


@bp.post("/invoices/<int:invoice_id>/confirm")
@staff_required
def confirm(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    if invoice.status != "draft":
        flash("Only draft invoices can be confirmed.", "warning")
        return redirect(url_for("billing.detail", invoice_id=invoice.id))
    invoice.status = "confirmed"
    db.session.commit()
    flash(f"Invoice {invoice.invoice_number} confirmed.", "success")
    return redirect(url_for("billing.detail", invoice_id=invoice.id))


@bp.post("/invoices/<int:invoice_id>/cancel")
@staff_required
def cancel(invoice_id):
    invoice = db.session.get(Invoice, invoice_id) or abort(404)
    paid_amount = calculate_paid_amount(invoice.id)
    if invoice.status not in {"draft", "confirmed"} or paid_amount > 0:
        flash("Only unpaid draft or confirmed invoices can be cancelled.", "warning")
        return redirect(url_for("billing.detail", invoice_id=invoice.id))
    invoice.status = "cancelled"
    db.session.commit()
    flash(f"Invoice {invoice.invoice_number} cancelled.", "success")
    return redirect(url_for("billing.detail", invoice_id=invoice.id))


@bp.get("/invoices/<int:invoice_id>/print")
@login_required
def print_invoice(invoice_id):
    invoice = _visible_invoice(invoice_id)
    metrics = _metrics(invoice)
    return render_template("billing/invoice_print.html", invoice=invoice, metrics=metrics)
