# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every implementation of an interface op must accept every parameter the op declares.

``RobovastInterface`` is bound three times over — ``LocalTransport``, ``ClusterService``
and ``HTTPTransport`` — and ``HTTPTransport`` forwards positionally. So adding a parameter
to an op and its local implementation leaves a trap: the abstract method and the one implementation agree, every type check passes, and the
route explodes at runtime with ``takes from 4 to 7 positional arguments but 8 were given``
the first time a user clicks the thing. That is exactly how ``render_campaign_notebook``'s
``batch`` shipped broken.

Nothing else catches it. The parameters are not part of the pydantic models, so the OpenAPI
schema check does not see them; the delegator forwards ``*args`` shaped by hand rather than
by signature; and no test exercised every binding of any single op.

Parameter *names*, not types or defaults: the delegator passes positionally, so what has to
line up is the order and the count, and a name is how you tell whether the right value
landed in the right slot.
"""

import inspect

import pytest

from robovast.service.http_client import HTTPTransport
from robovast.service.interface import RobovastInterface
from robovast.service.local_transport import LocalTransport

#: Every concrete binding of the interface. ``ClusterService`` is deliberately absent: it
#: subclasses ``LocalTransport`` and overriding is optional, so an op it does not override
#: resolves to the parent's and is already covered.
_IMPLEMENTATIONS = (LocalTransport, HTTPTransport)


def _abstract_ops():
    """The interface's abstract method names, in declaration order."""
    return sorted(RobovastInterface.__abstractmethods__)


def _params(func):
    """Positional/keyword parameter names of *func*, without ``self``.

    ``*args``/``**kwargs`` are reported too, since an implementation that takes them
    accepts anything and cannot be checked further.
    """
    sig = inspect.signature(func)
    return [name for name in sig.parameters if name != "self"], sig


@pytest.mark.parametrize("op", _abstract_ops())
@pytest.mark.parametrize("impl", _IMPLEMENTATIONS, ids=lambda c: c.__name__)
def test_implementation_accepts_every_declared_parameter(op, impl):
    declared, _ = _params(getattr(RobovastInterface, op))
    implemented, sig = _params(getattr(impl, op))

    # A catch-all signature (``**kwargs``) accepts whatever the interface adds.
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in sig.parameters.values()):
        return

    missing = [name for name in declared if name not in implemented]
    assert not missing, (
        f"{impl.__name__}.{op}() is missing {missing}, declared by RobovastInterface.{op}(). "
        f"A delegator forwards positionally, so this fails at runtime on the first call, not "
        f"at import: add the parameter here and pass it on.")


@pytest.mark.parametrize("op", _abstract_ops())
@pytest.mark.parametrize("impl", _IMPLEMENTATIONS, ids=lambda c: c.__name__)
def test_implementation_keeps_the_declared_parameter_order(op, impl):
    """Order too, because the delegators forward positionally.

    A reordered implementation would still "accept every parameter" while silently binding
    ``theme`` to ``batch``. Only the shared prefix is compared -- an implementation may add
    its own trailing parameters.
    """
    declared, _ = _params(getattr(RobovastInterface, op))
    implemented, sig = _params(getattr(impl, op))
    if any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in sig.parameters.values()):
        return

    shared = [name for name in implemented if name in declared]
    assert shared == [name for name in declared if name in implemented], (
        f"{impl.__name__}.{op}() takes {implemented}, reordering "
        f"RobovastInterface.{op}()'s {declared}. Positional forwarding would put values in "
        f"the wrong slots.")
