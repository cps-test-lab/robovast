# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Run-view panel remotes: descriptors + asset serving.

Covers both delivery paths for a Module-Federation run-view panel:

* **package-provided** — a ``robovast.panel_types`` entry point (``robovast_nav``'s
  ``costmap``) gets a ``remote`` descriptor in GET /campaigns/{id}/panels and its bundle
  served at GET /panel_types/{name}/assets/{path}.
* **user-authored** ``custom`` — a bundle staged into the campaign's ``_config/`` gets a
  descriptor and is served at GET /campaigns/{id}/panel_assets/{path}, path-confined.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from robovast.service.app import build_app
from robovast.service.client import LocalTransport


def _costmap_bundle_built() -> bool:
    """Whether robovast_nav's ``costmap`` panel ships a *built* WEB_PANEL bundle.

    The panel's ``remoteEntry.js`` is produced by the package's frontend build; a
    source checkout that installed robovast_nav without building its UI has the
    entry point but no bundle, so serving it 404s. Skip that one case rather than
    fail — the same spirit as the ``_GROWTH_SIM.exists()`` skips elsewhere.
    """
    from robovast.service.app import _resolve_plugin_asset
    try:
        _resolve_plugin_asset("robovast.panel_types", "costmap", "remoteEntry.js",
                              "WEB_PANEL")
        return True
    except Exception:  # noqa: BLE001 - unknown entry / no bundle → treat as not built
        return False


def _local_transport(results_root) -> LocalTransport:
    lt = LocalTransport.__new__(LocalTransport)
    lt._campaigns = {}
    lt._lock = threading.Lock()
    lt.store = None
    lt._campaigns_root = lambda: results_root
    return lt


def _make_campaign(tmp_path):
    # Minimal campaign snapshot: _config/<name>.vast with a package (costmap) panel and a
    # user-authored custom panel whose bundle is staged under _config/panels/my/.
    cfg = tmp_path / "camp-1" / "_config"
    (cfg / "panels" / "my").mkdir(parents=True)
    (cfg / "camp.vast").write_text(
        "version: 2\n"
        "visualization:\n"
        "  results:\n"
        "    run_view:\n"
        "      panels:\n"
        "      - playback:\n"
        "      - costmap:\n"
        "          title: Nav2\n"
        "      - custom:\n"
        "          remote: panels/my\n"
        "          module: ./myPanel\n"
    )
    (cfg / "panels" / "my" / "remoteEntry.js").write_text("// built bundle\n")
    (tmp_path / "secret.txt").write_text("outside")
    return TestClient(build_app(_local_transport(tmp_path)))


def test_panels_carry_remote_descriptors(tmp_path):
    with _make_campaign(tmp_path) as client:
        resp = client.get("/campaigns/camp-1/panels")
        assert resp.status_code == 200
        panels = {p["type"]: p for p in resp.json()["panels"]}

        # Built-in: no remote descriptor (host-native).
        assert "remote" not in panels["playback"]

        # Package-provided: robovast_nav's costmap resolves to a /panel_types asset. All
        # robovast_nav panels share one MF container ("robovast_nav"), but the asset URL is
        # still keyed by the entry-point name.
        cm = panels["costmap"]["remote"]
        assert cm["name"] == "robovast_nav"
        assert cm["module"] == "./costmap"
        assert cm["remote_entry_url"] == "/panel_types/costmap/assets/remoteEntry.js"

        # User-authored: served from the campaign's staged _config/.
        cu = panels["custom"]["remote"]
        assert cu["module"] == "./myPanel"
        assert cu["remote_entry_url"] == "/campaigns/camp-1/panel_assets/panels/my/remoteEntry.js"


@pytest.mark.skipif(not _costmap_bundle_built(),
                    reason="robovast_nav costmap WEB_PANEL bundle not built in this checkout")
def test_serves_package_panel_asset(tmp_path):
    with _make_campaign(tmp_path) as client:
        resp = client.get("/panel_types/costmap/assets/remoteEntry.js")
        assert resp.status_code == 200
        assert resp.content  # the real robovast_nav bundle


def test_serves_custom_panel_asset(tmp_path):
    with _make_campaign(tmp_path) as client:
        resp = client.get("/campaigns/camp-1/panel_assets/panels/my/remoteEntry.js")
        assert resp.status_code == 200
        assert b"built bundle" in resp.content


def test_unknown_package_panel_is_404(tmp_path):
    with _make_campaign(tmp_path) as client:
        assert client.get("/panel_types/nope/assets/remoteEntry.js").status_code == 404


def test_custom_panel_path_escape_rejected(tmp_path):
    lt = _local_transport(tmp_path)
    _make_campaign(tmp_path)  # create the layout
    import pytest
    with pytest.raises(ValueError):
        lt.resolve_campaign_panel_asset("camp-1", "../../secret.txt")
