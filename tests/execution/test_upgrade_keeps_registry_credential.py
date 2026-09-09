# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An upgrade must not rewrite the push Secret without the built-in registry's credential.

``vast service upgrade`` reads the registry host off the live Ingress. It used to call
``deploy_service`` with that host and no password, because it had none to pass -- and the
push Secret is rendered from *both* and replaced on conflict, so the empty password did not
leave the existing entry alone: it rewrote the Secret without it.

Nothing failed at that moment, which is what makes it worth a test. The registry went on
requiring the password its htpasswd still held, and the next experiment-image build pushed
anonymously and got a 401 naming a registry -- with the upgrade that removed the credential
well behind it. ``ensure_registry_htpasswd`` could not repair it afterwards either: it
recovers the password from this very Secret, so once an upgrade had dropped the entry it
minted a fresh password on every later setup and the registry's copy and the client's could
never agree again.

``cluster setup`` always did this correctly; the fix is that the upgrade path now recovers
(or mints) the same way, through the same function.
"""

import inspect

from robovast.execution.cluster_execution import cli


def _upgrade_source():
    """The body of the upgrade command, where the deploy_service call site lives."""
    return inspect.getsource(cli)


def test_the_upgrade_passes_a_registry_password_beside_the_host():
    """Host without password is the exact shape that rewrote the Secret without its entry."""
    src = _upgrade_source()
    call = src[src.index("registry_host=ingress_host"):]
    call = call[:call.index(")")]
    assert "registry_password=" in call, (
        "vast service upgrade passes registry_host to deploy_service; without a password "
        "beside it the push Secret is replaced without the built-in registry's entry, and "
        "the next build pushes anonymously into a registry that requires auth")


def test_the_upgrade_recovers_that_password_the_way_setup_does():
    """One function recovers-or-mints, so the htpasswd and the Secret cannot disagree."""
    src = _upgrade_source()
    assert "ensure_registry_htpasswd(" in src, (
        "the password must come from ensure_registry_htpasswd -- the same function "
        "cluster setup uses -- so the registry's htpasswd and the client credential are "
        "written from one value")
