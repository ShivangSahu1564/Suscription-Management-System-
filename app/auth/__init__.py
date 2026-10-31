from flask import Blueprint, abort
from flask_login import current_user, login_required

bp = Blueprint("auth", __name__)


def role_required(*roles):
    """Restrict a view to authenticated users with one of the given roles."""
    allowed_roles = frozenset(roles)

    def decorator(view):
        from functools import wraps

        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    return role_required("admin")(view)


def staff_required(view):
    return role_required("admin", "staff")(view)


def customer_required(view):
    return role_required("customer")(view)


from . import routes  # noqa: E402,F401
