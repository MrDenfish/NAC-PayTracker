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
    """Expose ``static_v`` to a ``Jinja2Templates`` instance's environment."""
    templates.env.globals["static_v"] = STATIC_VERSION
