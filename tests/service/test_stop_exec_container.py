# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``stop_exec_container`` on the real transport, not a fake of it.

The other tests of this verb (``test_exec_tools.py``, ``test_no_dangling_cluster_flag.py``)
supply a fake transport, which proves the CLI and the MCP tool *reach* the interface. This
one calls the real method, and pins its signature against the interface.
"""

from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from tests.service.null_service import NullService
@pytest.fixture
def manager():
    """Stand in for the exec manager. It is a *property*, so it is patched on the class --
    nothing here starts a container or touches Docker."""
    mgr = MagicMock()
    with patch.object(NullService, "_exec_manager",
                      new_callable=PropertyMock, return_value=mgr):
        yield mgr


def test_it_delegates_to_the_exec_manager(manager):
    """The call reaches the exec manager."""
    manager.stop.return_value = "stopped"
    transport = object.__new__(NullService)

    assert transport.stop_exec_container() == "stopped"
    manager.stop.assert_called_once_with()


def test_it_takes_no_arguments():
    """The signature the interface declares."""
    import inspect

    from robovast.service.interface import RobovastInterface

    declared = inspect.signature(RobovastInterface.stop_exec_container)
    actual = inspect.signature(NullService.stop_exec_container)
    assert list(actual.parameters) == list(declared.parameters) == ["self"]


def test_passing_one_is_rejected_rather_than_ignored():
    """A caller passing an argument fails loudly rather than having it ignored."""
    transport = object.__new__(NullService)

    with pytest.raises(TypeError):
        # pylint: disable-next=too-many-function-args  -- passing the removed argument is what this asserts
        transport.stop_exec_container("local")
