# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Why in-cluster builds are unavailable, said once and said completely.

The refusal reached a user naming **one** remedy: re-run ``vast exec cluster setup`` with
``--ingress-host``. That is the fix for "never published". It is the wrong fix for the state
that actually happened -- a *published* deployment whose registry prefix a ``setup`` re-run
had dropped -- where ``vast exec cluster upgrade`` recovers the host from the live Ingress.
An operator following the message re-ran setup on a deployment that was already published.

The in-pod service cannot tell the two apart: the prefix is baked at setup/upgrade time and
read back out of the environment, and the service has no RBAC to read its own Ingress (and
is deliberately not given any). So the message has to name both and point at the command
that *can* tell them apart.

It is shared between two callers but not identical at them: each keeps its own opening
clause, because "this campaign builds a container image" is what a campaign author needs to
hear and a request caller does not.
"""

import textwrap

import pytest

from robovast.execution.cluster_config.base_config import RegistryConfig


def test_a_configured_registry_has_nothing_to_explain():
    assert RegistryConfig(registry_prefix="reg.example/robovast").why_disabled() == ""
    assert RegistryConfig(registry_prefix="reg.example/robovast").enabled()


def test_it_names_both_remedies():
    """The whole point. One remedy is a wrong answer half the time."""
    why = RegistryConfig().why_disabled()

    assert "vast exec cluster upgrade" in why, "the published-but-prefix-dropped fix"
    assert "vast exec cluster setup" in why, "the never-published fix"
    assert "--ingress-host" in why
    assert "vast doctor" in why, "must point at what distinguishes the two states"


def test_it_leaks_no_registry_detail():
    """Registry endpoints, prefixes and credentials never cross the client interface --
    this string does. A prefix pasted in here would be the easiest way to break that."""
    why = RegistryConfig(
        registry_prefix="",                      # disabled, so the message is produced
        push_secret_name="robovast-registry-push",
        pull_secret_name="robovast-registry-pull",
        base_experiment_image="reg.internal/base:1",
    ).why_disabled()

    for secret in ("robovast-registry-push", "robovast-registry-pull",
                   "reg.internal/base:1"):
        assert secret not in why, f"{secret!r} leaked into a client-facing message"


@pytest.mark.parametrize("opener,expected", [
    ("cannot build an image: ", "cannot build an image: this cluster has nowhere to push it."),
    ("this campaign builds a container image, but ",
     "this campaign builds a container image, but this cluster has nowhere to push it."),
])
def test_it_composes_with_each_callers_opener(opener, expected):
    """The shared half starts mid-sentence on purpose. "nowhere to push **a built image**"
    read as a stutter after either opener had already named the image."""
    composed = opener + RegistryConfig().why_disabled()

    assert composed.startswith(expected), textwrap.fill(composed, 78)


def test_both_call_sites_use_it():
    """Two sites, one definition. A third is about to be added for the handshake, which is
    why this stopped being two hand-written strings."""
    import inspect

    from robovast.execution.cluster_execution import cluster_service

    source = inspect.getsource(cluster_service)
    assert source.count("why_disabled()") >= 2, (
        "a refusal stopped using the shared definition; the two would drift, and the one "
        "naming a single remedy is the one that misdirects")
    # And the old hand-written explanation is gone from both.
    assert "an unpublished service has no address" not in source
    assert "RoboVAST's own registry is published on the service's Ingress" not in source
