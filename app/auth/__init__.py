from flask import Blueprint, render_template

bp = Blueprint("auth", __name__)


@bp.get("/login")
def login():
    return render_template("base.html", page_title="Sign in")
