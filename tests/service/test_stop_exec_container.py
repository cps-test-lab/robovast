# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``stop_exec_container`` on the real transport, not a fake of it.

The method raised ``NameError`` on every call: a ``del backend`` outlived the parameter it
deleted, left behind when the per-request lane selector went and a service became
single-lane. ``vast exec stop-container`` was broken on the local lane for as long as that
line survived.

Nothing caught it. Every existing test of this verb -- in ``test_exec_tools.py`` and
``test_no_dangling_cluster_flag.py`` -- supplies its own fake transport with a
``stop_exec_container`` of two lines, which is right for what those tests assert (that the
CLI and the MCP tool *reach* the interface) and is exactly why the implementation behind it
went unexercised. pylint's ``E0602`` was the only thing that saw it.

So this one calls the real method. A fake that cannot be wrong about its own signature
proves nothing about the code it stands in for.
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from robovast.service.client import LocalTransport


@pytest.fixture
def manager():
    """Stand in for the exec manager. It is a *property*, so it is patched on the class --
    nothing here starts a container or touches Docker."""
    mgr = MagicMock()
    with patch.object(LocalTransport, "_exec_manager",
                      new_callable=PropertyMock, return_value=mgr):
        yield mgr


def test_it_delegates_to_the_exec_manager(manager):
    """The regression: this raised NameError before reaching the manager at all."""
    manager.stop.return_value = "stopped"
    transport = LocalTransport.__new__(LocalTransport)

    assert transport.stop_exec_container() == "stopped"
    manager.stop.assert_called_once_with()


def test_it_takes_no_arguments():
    """The signature the interface declares. The bug was a leftover `del` of a parameter
    that had already gone, so the pair is worth pinning together."""
    import inspect

    from robovast.service.interface import RobovastInterface

    declared = inspect.signature(RobovastInterface.stop_exec_container)
    actual = inspect.signature(LocalTransport.stop_exec_container)
    assert list(actual.parameters) == list(declared.parameters) == ["self"]


def test_passing_one_is_rejected_rather_than_ignored():
    """A caller still passing the removed lane argument must fail loudly.

    It happened: a CLI call site kept its positional after the parameter went, and the
    fake it was tested against accepted anything.
    """
    transport = LocalTransport.__new__(LocalTransport)

    with pytest.raises(TypeError):
        transport.stop_exec_container("local")
