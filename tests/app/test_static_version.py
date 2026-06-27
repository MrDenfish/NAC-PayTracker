"""The stylesheet link must be cache-busted with a NON-EMPTY content hash on
every template instance (main + auth/onboarding/billing/documents each build
their own Jinja2Templates, so each must register the `static_v` global)."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from nac_pay.app.main import app
from nac_pay.app.static_version import STATIC_VERSION

_LINK = re.compile(r"styles\.css\?v=([0-9a-f]+)")


def test_static_version_is_a_real_hash():
    assert re.fullmatch(r"[0-9a-f]{8}", STATIC_VERSION)


def test_login_page_stylesheet_is_versioned():
    """The auth layout uses a SEPARATE Jinja2Templates instance — regression
    for the bug where only main's instance had the global, so auth pages
    rendered `styles.css?v=` with an empty version."""
    client = TestClient(app)  # AUTH_REQUIRED unset → /login renders
    r = client.get("/login")
    assert r.status_code == 200
    m = _LINK.search(r.text)
    assert m, "login page stylesheet link is not version-busted"
    assert m.group(1) == STATIC_VERSION
