from datetime import date

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select

from ..extensions import db
from ..models import Customer, Plan, Product, Quotation, QuotationTemplate
from .service import (
    QuotationError, convert_to_subscription, create_quotation, save_template,
    set_status, update_quotation,
)

bp = Blueprint("quotations", __name__, template_folder="../templates")
MANAGER_ROLES = {"admin", "staff"}


def is_manager():
    return current_user.is_authenticated and (current_user.role or "").casefold() in MANAGER_ROLES


def require_manager():
    if not is_manager():
        abort(403)


def require_customer_or_manager():
    if not is_manager() and (current_user.role or "").casefold() != "customer":
        abort(403)


def get_visible_quote(quote_id):
    quote = db.session.get(Quotation, quote_id)
    if quote is None:
        abort(404)
    if not is_manager():
        require_customer_or_manager()
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == current_user.id))
        if customer_id is None or quote.customer_id != customer_id:
            abort(403)
    if quote.valid_until and quote.valid_until < date.today() and quote.status in {"draft", "quotation"}:
        quote.status = "expired"
        db.session.commit()
    return quote


def form_context(quotation=None):
    templates = db.session.scalars(select(QuotationTemplate).order_by(QuotationTemplate.name)).all()
    return {
        "customers": db.session.scalars(select(Customer).order_by(Customer.company_name)).all(),
        "products": db.session.scalars(select(Product).order_by(Product.name)).all(),
        "templates": templates,
        "template_data": [{
            "id": template.id,
            "name": template.name,
            "items": [{"product_id": item.product_id, "quantity": item.quantity,
                       "tax_rate": str(item.tax_rate), "unit_price": str(item.unit_price)}
                      for item in template.items],
        } for template in templates],
        "quotation": quotation,
        "today": date.today().isoformat(),
    }


@bp.get("/")
@login_required
def index():
    require_customer_or_manager()
    if is_manager():
        quotations = db.session.scalars(select(Quotation).order_by(Quotation.id.desc())).all()
    else:
        customer_id = db.session.scalar(select(Customer.id).where(Customer.user_id == current_user.id))
        quotations = db.session.scalars(select(Quotation).where(
            Quotation.customer_id == customer_id if customer_id else False
        ).order_by(Quotation.id.desc())).all()
    for quote in quotations:
        if quote.valid_until and quote.valid_until < date.today() and quote.status in {"draft", "quotation"}:
            quote.status = "expired"
    db.session.commit()
    return render_template("quotations.html", quotations=quotations, can_manage=is_manager())


@bp.route("/create", methods=["GET", "POST"])
@login_required
def create():
    require_manager()
    context = form_context()
    if request.method == "POST":
        try:
            quote = create_quotation(request.form)
            flash(f"Quotation {quote.quotation_number} created.", "success")
            return redirect(url_for("quotations.detail", quote_id=quote.id))
        except QuotationError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("create_quotation.html", **context)


@bp.route("/<int:quote_id>/edit", methods=["GET", "POST"])
@login_required
def edit(quote_id):
    require_manager()
    quote = db.session.get(Quotation, quote_id)
    if quote is None:
        abort(404)
    if quote.status != "draft":
        flash("Only draft quotations can be edited.", "warning")
        return redirect(url_for("quotations.detail", quote_id=quote.id))
    if request.method == "POST":
        try:
            update_quotation(quote, request.form)
            flash("Quotation updated.", "success")
            return redirect(url_for("quotations.detail", quote_id=quote.id))
        except QuotationError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("create_quotation.html", **form_context(quote), editing=True)


@bp.get("/<int:quote_id>")
@login_required
def detail(quote_id):
    quote = get_visible_quote(quote_id)
    plans = db.session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.name)).all() if is_manager() else []
    subtotal = sum((line.unit_price * line.quantity for line in quote.items), start=0)
    tax_total = sum((line.amount - (line.unit_price * line.quantity) for line in quote.items), start=0)
    return render_template("quotation_detail.html", quotation=quote, plans=plans,
                           subtotal=subtotal, tax_total=tax_total, can_manage=is_manager())


@bp.post("/<int:quote_id>/confirm")
@login_required
def confirm(quote_id):
    require_manager()
    quote = db.session.get(Quotation, quote_id) or abort(404)
    try:
        set_status(quote, "confirmed")
        flash("Quotation confirmed.", "success")
    except QuotationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("quotations.detail", quote_id=quote.id))


@bp.post("/<int:quote_id>/cancel")
@login_required
def cancel(quote_id):
    require_manager()
    quote = db.session.get(Quotation, quote_id) or abort(404)
    try:
        set_status(quote, "cancelled")
        flash("Quotation cancelled.", "success")
    except QuotationError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("quotations.detail", quote_id=quote.id))


@bp.post("/<int:quote_id>/convert")
@login_required
def convert(quote_id):
    require_manager()
    quote = db.session.get(Quotation, quote_id) or abort(404)
    try:
        plan = db.session.get(Plan, int(request.form.get("plan_id", "")))
        if plan is None or not plan.is_active:
            raise QuotationError("Select a valid active plan for the subscription.")
        subscription = convert_to_subscription(quote, plan)
        flash(f"Quotation converted to subscription {subscription.subscription_number}.", "success")
        return redirect(url_for("subscriptions.detail", subscription_id=subscription.id))
    except (QuotationError, ValueError, TypeError) as exc:
        db.session.rollback()
        flash(str(exc) or "Select a valid plan.", "danger")
    return redirect(url_for("quotations.detail", quote_id=quote.id))


@bp.get("/products/<int:product_id>")
@login_required
def product_data(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    return jsonify(id=product.id, name=product.name, sales_price=str(product.sales_price))


@bp.get("/templates")
@login_required
def template_list():
    require_manager()
    templates = db.session.scalars(select(QuotationTemplate).order_by(QuotationTemplate.name)).all()
    return render_template("quotation_templates.html", templates=templates, products=form_context()["products"], editing=None)


@bp.route("/templates/create", methods=["GET", "POST"])
@login_required
def template_create():
    require_manager()
    context = form_context()
    if request.method == "POST":
        try:
            template = save_template(request.form.get("name", ""), request.form)
            flash("Quotation template saved.", "success")
            return redirect(url_for("quotations.template_list"))
        except QuotationError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("quotation_templates.html", templates=[], products=context["products"], editing=None)


@bp.route("/templates/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
def template_edit(template_id):
    require_manager()
    template = db.session.get(QuotationTemplate, template_id) or abort(404)
    if request.method == "POST":
        try:
            save_template(request.form.get("name", ""), request.form, template)
            flash("Quotation template updated.", "success")
            return redirect(url_for("quotations.template_list"))
        except QuotationError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("quotation_templates.html", templates=[], products=form_context()["products"], editing=template)
