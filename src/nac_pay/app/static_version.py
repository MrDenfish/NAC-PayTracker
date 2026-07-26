"""Static-asset cache-busting version.

The app links ``styles.css`` with a ``?v=<hash>`` query so a CSS change
reaches the browser AND the Cloudflare edge cache (a hard refresh alone
does not purge Cloudflare's per-POP cache). The query changes only when
the CSS content changes.

The app builds several independent ``Jinja2Templates`` instances (main +
auth/onboarding/billing/documents route modules), each with its own Jinja
environment. Every one that renders a template referencing ``static_v``
must have the global registered — hence this shared helper, so a new
route module can't silently miss it.

``register`` is the shared template-env setup hook for the whole app, not
just the static-asset version — it's also where ``nav_is_admin`` (used by
``base.html`` to decide whether to show the Admin nav link) gets wired
into every Jinja environment.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _compute() -> str:
    try:
        data = (_HERE / "static" / "styles.css").read_bytes()
        return hashlib.sha256(data).hexdigest()[:8]
    except OSError:
        return "0"


# Computed once at import (per container build) — exactly when the bundled
# CSS can change.
STATIC_VERSION = _compute()


def register(templates) -> None:
    """Wire the shared template-env globals into a ``Jinja2Templates``
    instance: ``static_v`` (cache-busting) and ``nav_is_admin`` (nav
    visibility)."""
    templates.env.globals["static_v"] = STATIC_VERSION

    def _nav_is_admin(request) -> bool:
        from nac_pay.auth import auth_required, is_admin
        if not auth_required():
            return True
        uid = request.session.get("user_id") if hasattr(request, "session") else None
        return bool(uid) and is_admin(uid)

    templates.env.globals["nav_is_admin"] = _nav_is_admin
