# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""How long a browser may reuse each part of the served SPA.

Restarting the service onto a new build rehashes everything under ``assets/``, so a tab
that is still holding the previous ``index.html`` asks for chunk names that no longer
exist and its next lazy view fails to load. The UI answers that with a Reload button --
which only works if the reload is guaranteed to fetch the *new* document.

Plain ``StaticFiles`` sends ``ETag`` and ``Last-Modified`` but no ``Cache-Control``, which
leaves ``index.html`` heuristically cacheable: a browser may reuse it for a while without
asking, and then the reload hands back the same stale document and the button is a lie.
So the two directives are pinned here, in both directions -- the hashed assets must stay
cacheable, or every navigation re-downloads several megabytes of editor and charting
vendor over a link that is often a ``kubectl port-forward``.
"""

import pytest
from starlette.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport


@pytest.fixture
def client(tmp_path, monkeypatch):
    """An app serving a fake dist: one entry document, one hashed asset."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/index-AAAA.js"></script>',
        encoding="utf-8")
    (dist / "assets" / "index-AAAA.js").write_text("export default 1\n", encoding="utf-8")
    # Unhashed, and served from the same mount -- the rule has to be about the directory,
    # not about being a document.
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    monkeypatch.setenv("ROBOVAST_UI_DIST", str(dist))
    with TestClient(build_app(LocalTransport(), mount_mcp=False)) as test_client:
        yield test_client


def test_the_entry_document_is_revalidated(client):
    """``no-cache`` is not "do not store": with the ETag it costs a 304, not a download."""
    for path in ("/", "/index.html"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path


def test_a_hashed_asset_is_kept(client):
    """Its name changes when its content does, so reuse can never serve the wrong bytes."""
    response = client.get("/assets/index-AAAA.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_an_unhashed_file_outside_assets_is_revalidated(client):
    """The rule is structural, so a new unhashed file in the build is safe by default."""
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_a_vanished_chunk_is_still_a_404(client):
    """What the browser sees after a redeploy, and what the UI's Reload branch reacts to.

    ``html=True`` must not turn this into ``index.html`` with a 200: a module script whose
    body is HTML fails with a parse error naming nothing, instead of a fetch error the
    boundary can recognise.
    """
    assert client.get("/assets/index-OLDHASH.js").status_code == 404
