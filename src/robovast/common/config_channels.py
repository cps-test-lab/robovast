# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The three destination channels a configuration entry authors fixed values into.

``scenario:`` is what the trial does, ``sim:`` what it runs in, ``sut:`` how the system under
test is configured. A configuration entry and a preset both carry them under ``parameters:``,
so one grammar describes a fixed value wherever it is written.

Kept apart from the models on purpose: the ``extends:`` and preset expanders read these before
any model exists, and must not import :mod:`robovast.common.config`.

**These read an AUTHORED block.** A *generated* cell carries its own top-level ``sim`` and
``sut`` keys -- what the variations wrote -- which are a different thing sharing two names.
"""

SCENARIO = "scenario"
SIM = "sim"
SUT = "sut"

#: Every channel, in the order they are emitted and reported.
CHANNELS = (SCENARIO, SIM, SUT)


def channel(entry, name):
    """The authored *name* channel of a configuration entry or preset, ``{}`` when unset."""
    if not isinstance(entry, dict):
        return {}
    parameters = entry.get("parameters")
    if not isinstance(parameters, dict):
        return {}
    return parameters.get(name) or {}
