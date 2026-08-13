# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every test client in this suite is authenticated, by default.

The service has no unauthenticated mode (see ``robovast.service.auth``): a token is
minted rather than the check being skipped, so a test that built an app and called it
would otherwise get 401s that have nothing to do with what it is testing.

Two narrow patches rather than a token argument threaded through ~17 call sites:

* :func:`~robovast.service.auth.resolve_token` is pinned to a known token, so a test
  does not have to discover the one its app minted. Patched there rather than on
  ``build_app`` because the test modules bind ``build_app`` by name at import time, so
  replacing the attribute on the module would not reach them.
* ``TestClient`` presents that token unless the test passes its own headers — which is
  what lets the auth tests themselves check a *missing* or *wrong* token by being
  explicit.

Both are deliberately confined to ``tests/service``: the point is to keep every other
test about its own subject, not to make authentication invisible.
"""

import pytest
from starlette.testclient import TestClient

from robovast.service import auth as auth_module
from robovast.service.auth import AUTH_HEADER

#: The shared secret this suite runs against.
TEST_TOKEN = "test-token"

#: Ready-made header for a test that constructs a request by hand.
AUTH_HEADERS = {"Authorization": f"Bearer {TEST_TOKEN}"}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_auth: do not pin the shared token — for tests of token resolution itself")


@pytest.fixture(autouse=True)
def _authenticated_by_default(monkeypatch, request):
    if request.node.get_closest_marker("real_auth") is None:
        monkeypatch.setattr(auth_module, "resolve_token",
                            lambda configured=None: (configured or TEST_TOKEN, False))

    real_init = TestClient.__init__

    def init(self, app, *args, **kwargs):
        headers = dict(kwargs.get("headers") or {})
        if not any(key.lower() == AUTH_HEADER for key in headers):
            headers["Authorization"] = f"Bearer {TEST_TOKEN}"
        kwargs["headers"] = headers
        return real_init(self, app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)
