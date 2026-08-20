# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``_execution/providers.yaml`` — which distributions supplied a campaign's assets.

The record has THREE states and only two were reachable. Populated is "these providers";
empty is "asked, and there were none"; absent is "could not be asked", which
``read_providers_record`` documents as unknown and the publication gate classifies as opaque.

Writing empty for the third case is the one outcome that must not happen, and it did. The
record is written by whichever process prepares the campaign, which on a cluster lane is the
service pod — and that pod carries no simulator, so nothing registers the entry-point groups,
so an empty record was written and read back as "this campaign depended on no asset
providers". The campaign's image meanwhile had three private providers installed from a
private repository. A false clean is worse than a refusal, because a refusal gets examined.
"""

from robovast.common.campaign_data import read_providers_record
from robovast.common.execution import _record_asset_providers


class _Backend:
    """A backend declaring groups, as the roqsim one does."""

    ASSET_ENTRY_POINT_GROUPS = ("roqsim.models", "roqsim.worlds")


def _record(tmp_path, monkeypatch, *, backend, found):
    from robovast.common import execution

    monkeypatch.setattr(execution, "_campaign_simulator_backend", lambda _data: backend)
    monkeypatch.setattr("robovast.common.config_plugins.provider_provenance",
                        lambda _groups: dict(found))
    _record_asset_providers(tmp_path, {})
    return read_providers_record(tmp_path)


def test_groups_declared_but_nothing_found_is_unknown(tmp_path, monkeypatch):
    """The case that was silently wrong. Nothing found where a simulator's providers were
    expected means this process could not see them — not that the campaign had none."""
    assert _record(tmp_path, monkeypatch, backend=_Backend(), found={}) is None


def test_no_backend_records_an_honest_empty(tmp_path, monkeypatch):
    """A campaign with no simulator genuinely has no asset providers, and saying so is what
    keeps "none" distinguishable from a campaign predating this file."""
    assert _record(tmp_path, monkeypatch, backend=None, found={}) == {}


def test_what_was_found_is_recorded(tmp_path, monkeypatch):
    found = {"roqsim_assets_props": {"version": "0.1.0", "commit": "c" * 40,
                                     "url": "https://host/private"}}
    assert _record(tmp_path, monkeypatch, backend=_Backend(), found=found) == found


def test_unknown_is_opaque_to_the_publication_gate(tmp_path, monkeypatch):
    """The whole point of the distinction, asserted through the code that consumes it: absent
    must refuse publication, where empty passes it."""
    from robovast.results_processing.reproducibility import _classify_providers

    unknown = _record(tmp_path, monkeypatch, backend=_Backend(), found={})
    empty = _record(tmp_path / "none", monkeypatch, backend=None, found={})
    assert [e["class"] for e in _classify_providers(unknown)] == ["opaque"]
    assert _classify_providers(empty) == []
