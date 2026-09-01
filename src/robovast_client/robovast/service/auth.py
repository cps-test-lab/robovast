# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Who is talking to the service, and may they.

One shared secret, presented two ways, because the two kinds of client can carry
different things:

* **Browsers → a cookie.** Not a preference. ``EventSource`` cannot set request headers,
  so a header-only scheme would break every live stream in the web UI; same-origin
  cookies are sent automatically, which is also why the SPA needs no change at all.
* **CLI and MCP → ``Authorization: Bearer``.** A static header, which is what makes a
  headless MCP client configurable at all.

**There is no unauthenticated mode.** The tempting shortcut — "no token configured means
auth off" — makes development and production different code paths, which is the classic
source of "worked on my machine", and it makes the dangerous state (reachable *and*
open) reachable by omission. Instead :func:`resolve_token` mints an ephemeral token when
none is configured, and ``vast serve`` prints a login URL carrying it, the way Jupyter
has done for years.

The middleware resolves a :class:`Principal` rather than answering yes/no. Today every
authenticated caller may do everything, so the distinction buys nothing *yet* — it buys
that swapping the shared secret for a real identity provider later replaces one function
instead of touching every route. ``oauth2-proxy`` in front of the Ingress would set
``X-Forwarded-Email``; that is a new branch here and nothing else.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Environment variable holding the shared secret, in the pod and on a dev machine alike.
TOKEN_ENV_VAR = "ROBOVAST_AUTH_TOKEN"

#: Cookie the browser gets after logging in. ``__Host-`` is deliberately *not* used: it
#: mandates ``Secure``, which would make the cookie unusable over plain http on a
#: developer's loopback.
SESSION_COOKIE = "robovast_session"

#: Companion cookie holding the self-declared display name. Readable by JavaScript on
#: purpose — the UI shows it, and it is not a credential.
NAME_COOKIE = "robovast_user"

#: Header the CLI and MCP present the token in.
AUTH_HEADER = "authorization"

#: What an unauthenticated API caller is told. A constant because the gate reports its own
#: refusals to whoever asked to hear them (see :class:`AuthMiddleware`), and a record that
#: paraphrased the reply would be a second wording of the same refusal.
UNAUTHENTICATED_DETAIL = ("not authenticated: present the shared token as "
                          "'Authorization: Bearer <token>', or run 'vast login <url>'")

#: Header carrying the caller's self-declared name. Not a credential and never treated
#: as one: it says who someone *claims* to be, which with a shared secret is all anyone
#: can say.
USER_HEADER = "x-robovast-user"

def _public_paths() -> frozenset[str]:
    """Paths served without a token.

    The kubelet's probe, which carries no credential and whose failure would restart the
    pod forever; and the login page itself, which is how a session is obtained. Both come
    from :class:`~robovast.service.interface.Routes` so this list cannot drift from the
    routes it names.
    """
    from robovast.service.interface import Routes
    return frozenset({Routes.HEALTHZ, Routes.LOGIN})


#: Resolved once: the route table is a module-level constant, not runtime state.
PUBLIC_PATHS = _public_paths()


@dataclass(frozen=True)
class Principal:
    """Who the request is from.

    *name* is self-declared and unverified — with a shared secret nobody can prove who
    they are, and the UI labels it as such. *source* names how the caller authenticated,
    so a later identity provider is distinguishable from the shared secret without
    changing anything that reads this.
    """

    authenticated: bool
    name: str | None = None
    source: str = "anonymous"

    @property
    def display_name(self) -> str | None:
        """The name to record, or ``None`` when the caller did not give one.

        A missing name stays missing: "nobody said" and "someone called themselves X"
        are different facts, and inventing a placeholder would erase the difference.
        """
        return self.name or None


def generate_token() -> str:
    """A fresh shared secret."""
    return secrets.token_urlsafe(32)


def resolve_token(configured: str | None = None) -> tuple[str, bool]:
    """The token to enforce, and whether it had to be invented.

    Returns ``(token, ephemeral)``. *ephemeral* is what lets ``vast serve`` print a
    ready-to-click login URL instead of leaving a developer to guess a secret nobody
    set — and it is why there is no unauthenticated mode to slip into.
    """
    token = (configured or os.environ.get(TOKEN_ENV_VAR) or "").strip()
    if token:
        return token, False
    return generate_token(), True


def _token_matches(presented: str, expected: str) -> bool:
    """Constant-time comparison. ``==`` on a secret leaks its prefix through timing."""
    return hmac.compare_digest(presented.encode(), expected.encode())


def _bearer(header_value: str) -> str:
    """The token out of an ``Authorization`` header, or ``""``."""
    prefix = "bearer "
    if header_value[:len(prefix)].lower() == prefix:
        return header_value[len(prefix):].strip()
    return ""


def _cookies(raw: str) -> dict[str, str]:
    """Parse a Cookie header without pulling in http.cookies' quoting rules."""
    out = {}
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def principal_from_headers(headers: dict[str, str], expected: str) -> Principal:
    """Resolve a :class:`Principal` from already-lowercased request headers.

    Header first, then cookie: a CLI that presents a token should not be overridden by a
    stale browser session sharing the same header jar.
    """
    name = (headers.get(USER_HEADER) or "").strip() or None

    presented = _bearer(headers.get(AUTH_HEADER, ""))
    if presented and _token_matches(presented, expected):
        return Principal(authenticated=True, name=name, source="shared-secret")

    cookies = _cookies(headers.get("cookie", ""))
    session = cookies.get(SESSION_COOKIE, "")
    if session and _token_matches(session, expected):
        # The browser's own name cookie, when the request carried no explicit header.
        return Principal(authenticated=True,
                         name=name or (cookies.get(NAME_COOKIE) or "").strip() or None,
                         source="shared-secret")

    return Principal(authenticated=False, name=name)


def wants_html(headers: dict[str, str]) -> bool:
    """True when this looks like a browser navigation rather than an API call.

    Decides whether an unauthenticated request gets a login page or a JSON 401 — a
    person typing the URL should meet a password box, not a parse error.
    """
    return "text/html" in headers.get("accept", "")


class AuthMiddleware:
    """Pure-ASGI gate in front of the whole app, ``/mcp`` mount included.

    ASGI rather than ``BaseHTTPMiddleware`` because the latter buffers through an
    anyio stream that has repeatedly broken long-lived streaming responses — and this
    service's campaign list, log tails and job logs are all Server-Sent Events that must
    reach the browser byte-by-byte.
    """

    def __init__(self, app, token: str, on_reject=None):
        self.app = app
        self.token = token
        #: Called with ``(path, detail)`` for each caller turned away with a 401, or ``None``.
        #: The gate runs in front of the app, outside every exception handler that app
        #: installs, so this refusal is the one that reaches a client without passing through
        #: them -- without the hook it is invisible to anything recording what was refused.
        #: A callable rather than a log: this package is the client, and must not learn what
        #: the service keeps its records in.
        self.on_reject = on_reject

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers") or []}
        principal = principal_from_headers(headers, self.token)
        if principal.authenticated:
            # Carried on the scope so routes can read it without re-parsing headers.
            scope["state"] = dict(scope.get("state") or {})
            scope["state"]["principal"] = principal
            await self.app(scope, receive, send)
            return

        await self._reject(scope, send, headers)

    async def _reject(self, scope, send, headers):
        if wants_html(headers):
            target = scope.get("path", "/")
            if scope.get("query_string"):
                target += "?" + scope["query_string"].decode("latin-1")
            location = "/login?next=" + _quote(target)
            await _send_simple(send, 303, b"", [(b"location", location.encode())])
            return
        # Only this branch. The html branch above redirects to the login page, which is the
        # sign-in flow working rather than an action anyone was refused.
        if self.on_reject is not None:
            try:
                self.on_reject(scope.get("path", ""), UNAUTHENTICATED_DETAIL)
            except Exception:  # pylint: disable=broad-except
                logger.debug("could not report an auth refusal", exc_info=True)
        await _send_simple(
            send, 401,
            json.dumps({"detail": UNAUTHENTICATED_DETAIL}).encode(),
            [(b"content-type", b"application/json"),
             # Named scheme, so a generic HTTP client reports "unauthorized" rather than
             # inventing a challenge nobody offered.
             (b"www-authenticate", b'Bearer realm="robovast"')])


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


async def _send_simple(send, status: int, body: bytes, headers: list) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})
