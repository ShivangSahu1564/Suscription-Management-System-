from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import (Discount, DiscountProduct, Plan, PlanProduct, Product,
                      ProductVariant, QuotationItem, SubscriptionItem, InvoiceItem, Tax)

bp = Blueprint("catalog", __name__, template_folder="../templates")
CYCLES = {"daily", "weekly", "monthly", "yearly"}


def role():
    return (current_user.role or "").casefold() if current_user.is_authenticated else ""


def manager():
    return role() in {"admin", "staff"}


def require_manager():
    if not manager():
        abort(403)


def require_admin():
    if role() != "admin":
        abort(403)


def parse_decimal(raw, label, maximum=None):
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError):
        raise ValueError(f"{label} must be a valid number.")
    if not value.is_finite() or value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be between 0 and {maximum}." if maximum is not None else f"{label} cannot be negative.")
    return value


def parse_date(raw):
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("Enter a valid date.")


def parse_products(form, field="product_ids"):
    ids = []
    for raw in form.getlist(field):
        try:
            product_id = int(raw)
        except (ValueError, TypeError):
            raise ValueError("Select valid products.")
        product = db.session.get(Product, product_id)
        if product is None:
            raise ValueError("A selected product does not exist.")
        if product_id not in ids:
            ids.append(product_id)
    return ids


def safe_referrer(default_endpoint):
    referrer = request.referrer
    if referrer:
        parsed = urlparse(referrer)
        if parsed.scheme in {"http", "https"} and parsed.netloc == request.host:
            return referrer
        if not parsed.netloc and parsed.path.startswith("/"):
            return referrer
    return url_for(default_endpoint)


@bp.get("/")
@login_required
def index():
    require_manager()
    return render_template("catalog/index.html", page_title="Catalog")


@bp.get("/products")
@login_required
def products():
    require_manager()
    query = select(Product)
    term = request.args.get("q", "").strip()
    status = request.args.get("status", "")
    if term:
        query = query.where(or_(Product.name.ilike(f"%{term}%"), Product.product_type.ilike(f"%{term}%")))
    if status in {"active", "inactive"}:
        query = query.where(Product.is_active.is_(status == "active"))
    rows = db.session.scalars(query.order_by(Product.name)).all()
    return render_template("catalog/products.html", products=rows, q=term, status=status, can_edit=role() == "admin")


def product_form(product=None):
    try:
        name = request.form.get("name", "").strip()
        product_type = request.form.get("product_type", "service").strip() or "service"
        if not name or len(name) > 180:
            raise ValueError("Product name is required and must be at most 180 characters.")
        sales = parse_decimal(request.form.get("sales_price", ""), "Sales price")
        cost = parse_decimal(request.form.get("cost_price", ""), "Cost price")
        if product is None:
            product = Product(name=name)
            db.session.add(product)
        product.name, product.product_type = name, product_type
        product.sales_price, product.cost_price = sales, cost
        product.notes = request.form.get("notes", "").strip() or None
        db.session.commit()
        return product, None
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        return product, str(exc) if isinstance(exc, ValueError) else "Could not save the product."


@bp.route("/products/create", methods=["GET", "POST"])
@login_required
def create_product():
    require_admin()
    if request.method == "POST":
        product, error = product_form()
        if not error:
            flash("Product created.", "success")
            return redirect(url_for("catalog.product_detail", product_id=product.id))
        flash(error, "danger")
    return render_template("catalog/create_product.html", product=None)


@bp.get("/products/<int:product_id>")
@login_required
def product_detail(product_id):
    require_manager()
    product = db.session.get(Product, product_id) or abort(404)
    return render_template("catalog/product_detail.html", product=product, can_edit=role() == "admin")


@bp.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    require_admin()
    product = db.session.get(Product, product_id) or abort(404)
    if request.method == "POST":
        saved, error = product_form(product)
        if not error:
            flash("Product updated.", "success")
            return redirect(url_for("catalog.product_detail", product_id=saved.id))
        flash(error, "danger")
    return render_template("catalog/edit_product.html", product=product)


@bp.post("/products/<int:product_id>/toggle")
@login_required
def toggle_product(product_id):
    require_admin()
    product = db.session.get(Product, product_id) or abort(404)
    product.is_active = not product.is_active
    db.session.commit()
    flash("Product status updated.", "success")
    return redirect(safe_referrer("catalog.products"))


@bp.route("/products/<int:product_id>/variants", methods=["GET", "POST"])
@login_required
def variants(product_id):
    require_manager()
    product = db.session.get(Product, product_id) or abort(404)
    if request.method == "POST":
        require_admin()
        try:
            name = request.form.get("variant_name", "").strip()
            amount = parse_decimal(request.form.get("additional_price", ""), "Additional price")
            if not name:
                raise ValueError("Variant name is required.")
            db.session.add(ProductVariant(product=product, variant_name=name, additional_price=amount))
            db.session.commit()
            flash("Variant added.", "success")
            return redirect(url_for("catalog.variants", product_id=product.id))
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
    return render_template("catalog/variants.html", product=product)


@bp.post("/variants/<int:variant_id>/edit")
@login_required
def edit_variant(variant_id):
    require_admin()
    variant = db.session.get(ProductVariant, variant_id) or abort(404)
    try:
        name = request.form.get("variant_name", "").strip()
        amount = parse_decimal(request.form.get("additional_price", ""), "Additional price")
        if not name:
            raise ValueError("Variant name is required.")
        variant.variant_name, variant.additional_price = name, amount
        db.session.commit()
        flash("Variant updated.", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "danger")
    return redirect(url_for("catalog.variants", product_id=variant.product_id))


@bp.post("/variants/<int:variant_id>/toggle")
@login_required
def toggle_variant(variant_id):
    require_admin()
    variant = db.session.get(ProductVariant, variant_id) or abort(404)
    variant.is_active = not variant.is_active
    db.session.commit()
    return redirect(url_for("catalog.variants", product_id=variant.product_id))


def plan_values(form, plan=None):
    name = form.get("name", "").strip()
    cycle = form.get("billing_cycle", "").casefold()
    price = parse_decimal(form.get("price", ""), "Plan price")
    try:
        minimum = int(form.get("minimum_quantity", "1"))
    except ValueError:
        minimum = 0
    start, end = parse_date(form.get("start_date")), parse_date(form.get("end_date"))
    if not name:
        raise ValueError("Plan name is required.")
    if cycle not in CYCLES:
        raise ValueError("Choose daily, weekly, monthly, or yearly billing.")
    if minimum <= 0:
        raise ValueError("Minimum quantity must be positive.")
    if start and end and end < start:
        raise ValueError("End date cannot precede start date.")
    product_ids = parse_products(form)
    parsed = []
    all_product_ids = [p.id for p in db.session.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.name)).all()]
    submitted_quantities = form.getlist("quantities")
    for pid in product_ids:
        try:
            raw = submitted_quantities[all_product_ids.index(pid)]
        except (ValueError, IndexError):
            raise ValueError("Enter a quantity for every selected product.")
        try:
            q = int(raw)
        except ValueError:
            q = 0
        if q <= 0:
            raise ValueError("Product quantities must be positive.")
        parsed.append(q)
    if plan is None:
        plan = Plan()
        db.session.add(plan)
    plan.name, plan.billing_cycle, plan.price = name, cycle, price
    plan.minimum_quantity, plan.start_date, plan.end_date = minimum, start, end
    plan.auto_close, plan.is_active = bool(form.get("auto_close")), bool(form.get("is_active"))
    plan.products.clear()
    plan.products.extend(PlanProduct(product_id=pid, quantity=q) for pid, q in zip(product_ids, parsed))
    return plan


@bp.get("/plans")
@login_required
def plans():
    require_manager()
    return render_template("catalog/plans.html", plans=db.session.scalars(select(Plan).order_by(Plan.name)).all(), can_edit=role() == "admin")


def plan_form(plan=None, editing=False):
    products = db.session.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.name)).all()
    if request.method == "POST":
        try:
            plan = plan_values(request.form, plan)
            db.session.commit()
            flash("Plan saved.", "success")
            return redirect(url_for("catalog.plans"))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "Could not save the plan.", "danger")
    return render_template("catalog/edit_plan.html" if editing else "catalog/create_plan.html", plan=plan, products=products)


@bp.route("/plans/create", methods=["GET", "POST"])
@login_required
def create_plan():
    require_admin()
    return plan_form()


@bp.route("/plans/<int:plan_id>/edit", methods=["GET", "POST"])
@login_required
def edit_plan(plan_id):
    require_admin()
    plan = db.session.get(Plan, plan_id) or abort(404)
    return plan_form(plan, editing=True)


@bp.post("/plans/<int:plan_id>/toggle")
@login_required
def toggle_plan(plan_id):
    require_admin()
    plan = db.session.get(Plan, plan_id) or abort(404)
    plan.is_active = not plan.is_active
    db.session.commit()
    return redirect(url_for("catalog.plans"))


@bp.get("/taxes")
@login_required
def taxes():
    require_manager()
    return render_template("catalog/taxes.html", taxes=db.session.scalars(select(Tax).order_by(Tax.name)).all(), can_edit=role() == "admin")


@bp.post("/taxes")
@login_required
def save_tax():
    require_admin()
    try:
        tax_id = request.form.get("tax_id")
        tax = db.session.get(Tax, int(tax_id)) if tax_id else Tax()
        if tax is None:
            abort(404)
        name = request.form.get("name", "").strip()
        if not name:
            raise ValueError("Tax name is required.")
        tax.name, tax.rate = name, parse_decimal(request.form.get("rate", ""), "Tax percentage", Decimal("100"))
        if not tax_id:
            db.session.add(tax)
        db.session.commit()
        flash("Tax rule saved.", "success")
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        flash(str(exc) if isinstance(exc, ValueError) else "Tax name must be unique.", "danger")
    return redirect(url_for("catalog.taxes"))


@bp.post("/taxes/<int:tax_id>/toggle")
@login_required
def toggle_tax(tax_id):
    require_admin()
    tax = db.session.get(Tax, tax_id) or abort(404)
    tax.is_active = not tax.is_active
    db.session.commit()
    return redirect(url_for("catalog.taxes"))


@bp.get("/discounts")
@login_required
def discounts():
    require_manager()
    rows = db.session.scalars(select(Discount).order_by(Discount.name)).all()
    return render_template("catalog/discounts.html", discounts=rows, can_edit=role() == "admin", products=db.session.scalars(select(Product).order_by(Product.name)).all())


@bp.route("/discounts/create", methods=["GET", "POST"])
@login_required
def create_discount():
    require_admin()
    return discount_form()


@bp.route("/discounts/<int:discount_id>/edit", methods=["GET", "POST"])
@login_required
def edit_discount(discount_id):
    require_admin()
    discount = db.session.get(Discount, discount_id) or abort(404)
    return discount_form(discount)


def discount_form(discount=None):
    products = db.session.scalars(select(Product).order_by(Product.name)).all()
    if request.method == "POST":
        try:
            name = request.form.get("name", "").strip()
            kind = request.form.get("discount_type", "").casefold()
            value = parse_decimal(request.form.get("value", ""), "Discount value", Decimal("100") if kind == "percentage" else None)
            minimum_q = int(request.form.get("minimum_quantity", "1"))
            min_amount = parse_decimal(request.form.get("minimum_purchase_amount", "0"), "Minimum purchase amount")
            start, end = parse_date(request.form.get("start_date")), parse_date(request.form.get("end_date"))
            usage_raw = request.form.get("usage_limit", "").strip()
            usage = int(usage_raw) if usage_raw else None
            if not name or kind not in {"fixed", "percentage"} or minimum_q <= 0 or (usage is not None and usage < 0):
                raise ValueError("Enter a name, supported type, positive minimum quantity, and valid usage limit.")
            if start and end and end < start:
                raise ValueError("End date cannot precede start date.")
            product_ids = parse_products(request.form)
            if discount is None:
                discount = Discount(usage_count=0)
                db.session.add(discount)
            discount.name, discount.discount_type, discount.value = name, kind, value
            discount.minimum_quantity, discount.minimum_purchase_amount = minimum_q, min_amount
            discount.start_date, discount.end_date = start, end
            discount.usage_limit, discount.is_active = usage, bool(request.form.get("is_active"))
            discount.products.clear()
            discount.products.extend(DiscountProduct(product_id=pid) for pid in product_ids)
            db.session.commit()
            flash("Discount saved.", "success")
            return redirect(url_for("catalog.discounts"))
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            flash(str(exc) if isinstance(exc, ValueError) else "Could not save discount.", "danger")
    return render_template("catalog/create_discount.html" if discount is None else "catalog/create_discount.html", discount=discount, products=products)


@bp.post("/discounts/<int:discount_id>/toggle")
@login_required
def toggle_discount(discount_id):
    require_admin()
    discount = db.session.get(Discount, discount_id) or abort(404)
    discount.is_active = not discount.is_active
    db.session.commit()
    return redirect(url_for("catalog.discounts"))


def discount_is_available(discount, today=None):
    today = today or date.today()
    return bool(discount.is_active and (not discount.start_date or discount.start_date <= today)
                and (not discount.end_date or discount.end_date >= today)
                and (discount.usage_limit is None or discount.usage_count < discount.usage_limit))


@bp.get("/api/catalog-data")
@login_required
def catalog_data():
    require_manager()
    today = date.today()
    products = db.session.scalars(select(Product).where(Product.is_active.is_(True)).order_by(Product.name)).all()
    plans = db.session.scalars(select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.name)).all()
    taxes = db.session.scalars(select(Tax).where(Tax.is_active.is_(True)).order_by(Tax.name)).all()
    discounts = [d for d in db.session.scalars(select(Discount).order_by(Discount.name)).all() if discount_is_available(d, today)]
    return jsonify(products=[{"id": p.id, "name": p.name, "sales_price": str(p.sales_price), "variants": [{"id": v.id, "name": v.variant_name, "price": str(Decimal(p.sales_price) + Decimal(v.additional_price))} for v in p.variants if v.is_active]} for p in products], plans=[{"id": p.id, "name": p.name, "price": str(p.price), "billing_cycle": p.billing_cycle, "minimum_quantity": p.minimum_quantity, "start_date": p.start_date.isoformat() if p.start_date else None, "end_date": p.end_date.isoformat() if p.end_date else None, "auto_close": p.auto_close, "products": [{"product_id": pp.product_id, "quantity": pp.quantity} for pp in p.products]} for p in plans], taxes=[{"id": t.id, "name": t.name, "rate": str(t.rate)} for t in taxes], discounts=[{"id": d.id, "name": d.name, "type": d.discount_type, "value": str(d.value), "minimum_quantity": d.minimum_quantity, "minimum_purchase_amount": str(d.minimum_purchase_amount), "usage_count": d.usage_count, "usage_limit": d.usage_limit, "product_ids": [x.product_id for x in d.products]} for d in discounts])

