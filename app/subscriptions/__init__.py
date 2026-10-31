from datetime import date

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from ..extensions import db
from ..models import Customer, Plan, Product, Subscription
from .service import (
    SubscriptionError, billing_data, close_expired, convert_quotation,
    create_subscription, renew, transition, update_subscription,
)

bp = Blueprint("subscriptions", __name__)
MANAGER_ROLES = {"admin", "staff"}


def is_manager():
    return current_user.is_authenticated and (current_user.role or "").casefold() in MANAGER_ROLES


def require_manager():
    if not is_manager():
        abort(403)


def require_customer_or_manager():
    if not is_manager() and (current_user.role or "").casefold() != "customer":
        abort(403)


def visible_subscription(subscription_id):
    subscription = db.session.get(Subscription, subscription_id)
    if subscription is None:
        abort(404)
    if not is_manager():
        require_customer_or_manager()
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == current_user.id))
        if customer_id is None or subscription.customer_id != customer_id:
            abort(403)
    close_expired(subscription)
    return subscription


def form_context(subscription=None):
    return {
        "customers": db.session.scalars(select(Customer).order_by(Customer.company_name)).all(),
        "plans": db.session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.name)).all(),
        "products": db.session.scalars(select(Product).order_by(Product.name)).all(),
        "subscription": subscription,
        "today": date.today().isoformat(),
    }


@bp.get("/")
@login_required
def index():
    require_customer_or_manager()
    if is_manager():
        subscriptions = db.session.scalars(select(Subscription).order_by(Subscription.id.desc())).all()
    else:
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == current_user.id))
        subscriptions = db.session.scalars(select(Subscription).where(
            Subscription.customer_id == customer_id if customer_id else False
        ).order_by(Subscription.id.desc())).all()
    for subscription in subscriptions:
        close_expired(subscription)
    return render_template("subscriptions.html", subscriptions=subscriptions, can_manage=is_manager())


@bp.route("/create", methods=["GET", "POST"])
@login_required
def create():
    require_manager()
    if request.method == "POST":
        try:
            subscription = create_subscription(request.form)
            flash(f"Subscription {subscription.subscription_number} created.", "success")
            return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))
        except SubscriptionError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("create_subscription.html", **form_context())


@bp.route("/<int:subscription_id>/edit", methods=["GET", "POST"])
@login_required
def edit(subscription_id):
    require_manager()
    subscription = db.session.get(Subscription, subscription_id)
    if subscription is None:
        abort(404)
    if subscription.status != "draft":
        flash("Only draft subscriptions can be edited.", "warning")
        return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))
    if request.method == "POST":
        try:
            update_subscription(subscription, request.form)
            flash("Subscription updated.", "success")
            return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))
        except SubscriptionError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("edit_subscription.html", **form_context(subscription), editing=True)


@bp.get("/<int:subscription_id>")
@login_required
def detail(subscription_id):
    subscription = visible_subscription(subscription_id)
    subtotal = sum((item.unit_price * item.quantity for item in subscription.items), start=0)
    tax_total = sum((item.amount - item.unit_price * item.quantity for item in subscription.items), start=0)
    return render_template("subscription_detail.html", subscription=subscription,
                           subtotal=subtotal, tax_total=tax_total, can_manage=is_manager())


@bp.post("/<int:subscription_id>/confirm")
@login_required
def confirm(subscription_id):
    return _run_action(subscription_id, "confirm")


@bp.post("/<int:subscription_id>/quote")
@login_required
def send_as_quotation(subscription_id):
    return _run_action(subscription_id, "quote")


@bp.post("/<int:subscription_id>/activate")
@login_required
def activate(subscription_id):
    return _run_action(subscription_id, "activate")


@bp.post("/<int:subscription_id>/pause")
@login_required
def pause(subscription_id):
    return _run_action(subscription_id, "pause")


@bp.post("/<int:subscription_id>/resume")
@login_required
def resume(subscription_id):
    return _run_action(subscription_id, "resume")


@bp.post("/<int:subscription_id>/close")
@login_required
def close(subscription_id):
    return _run_action(subscription_id, "close")


def _run_action(subscription_id, action):
    require_manager()
    subscription = db.session.get(Subscription, subscription_id) or abort(404)
    try:
        transition(subscription, action)
        messages = {"quote": "Subscription quotation issued.", "confirm": "Subscription confirmed.",
                    "activate": "Subscription activated.", "pause": "Subscription paused.",
                    "resume": "Subscription resumed.", "close": "Subscription closed."}
        flash(messages[action], "success")
    except SubscriptionError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))


@bp.post("/<int:subscription_id>/renew")
@login_required
def renew_subscription(subscription_id):
    require_manager()
    subscription = db.session.get(Subscription, subscription_id) or abort(404)
    try:
        renew(subscription)
        flash("Subscription renewed.", "success")
    except SubscriptionError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))


@bp.post("/from-quotation/<int:quotation_id>")
@login_required
def convert_quotation_route(quotation_id):
    require_manager()
    from ..models import Quotation
    quotation = db.session.get(Quotation, quotation_id) or abort(404)
    try:
        plan = db.session.get(Plan, int(request.form.get("plan_id", "")))
        subscription = convert_quotation(quotation, plan)
        flash(f"Quotation converted to subscription {subscription.subscription_number}.", "success")
        return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))
    except (SubscriptionError, ValueError, TypeError) as exc:
        db.session.rollback()
        flash(str(exc) or "Select a valid plan.", "danger")
        return redirect(url_for("quotations.detail", quote_id=quotation_id))


@bp.get("/<int:subscription_id>/billing-data")
@login_required
def billing_data_endpoint(subscription_id):
    subscription = visible_subscription(subscription_id)
    return jsonify(billing_data(subscription))


@bp.get("/products/<int:product_id>")
@login_required
def product_data(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    return jsonify(id=product.id, name=product.name, sales_price=str(product.sales_price))
