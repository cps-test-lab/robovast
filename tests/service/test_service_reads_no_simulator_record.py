# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The service asks a simulator's own tool for its records rather than reading them."""

from pathlib import Path


def test_the_service_never_reads_a_simulator_s_own_records():
    """The invariant a well-meaning shortcut breaks first, so it is asserted rather than trusted.

    A live read *could* open the simulator's pose or clock record directly and would be marginally
    cheaper. It is deliberately not done: those files belong to the simulator and are free to be
    reshaped, so a reader here would be a hidden cross-repo coupling that breaks silently on the
    day they are. The service execs the container's own tool and reads the JSON it declares.

    Scoped to the service tree on purpose. Naming those records is correct in two other places: the
    simulator-specific backend package, which *is* the code that knows its simulator, and the
    results tables, where the CSV became a documented column set through the generic
    one-table-per-CSV-stem ingest rather than through anything reading it by name.
    """
    # From a module in it rather than from the package: `robovast.service` is a namespace package,
    # so it has no `__file__` of its own.
    import robovast.service.service_base as _base
    service_dir = Path(_base.__file__).parent
    offenders = [path.name for path in service_dir.rglob("*.py")
                 if "sim_poses" in path.read_text(encoding="utf-8")]
    assert offenders == [], (
        f"{offenders} names a simulator's own record. Ask the simulator's tool instead — see "
        "SimulatorBackend.health_command.")
