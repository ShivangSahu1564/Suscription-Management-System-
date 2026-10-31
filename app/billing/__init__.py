from flask import Blueprint, render_template

bp = Blueprint("billing", __name__)


@bp.get("/")
def index():
    return render_template("base.html", page_title="Billing")
