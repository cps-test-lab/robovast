# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a plain ``vast`` invocation is allowed to import.

``load_plugins()`` imports **every** registered CLI plugin on every invocation, so one
module-level import in one plugin is paid for by ``vast login``, ``vast wait`` and
``vast --help`` alike. Four such imports in ``execution_utils/cli.py`` pulled the whole
Kubernetes client into a command that never touches a cluster: 1777 modules where 625 do.

That is invisible from the outside -- nothing fails, it is just slow and it makes the
cluster stack a hard requirement of the CLI. So it needs a test rather than a convention,
and the test runs in a **subprocess**: by the time the rest of the suite has run,
``sys.modules`` in this process says nothing about what a fresh CLI start would import.

Deferring is the fix, not removing: the operator commands still need these, they just
import them in the command body.

``numpy`` and ``scenario_execution`` were once excluded here, as coming from
``robovast.common`` and needing a larger untangling. That untangling is done -- the CLI
root imported ``load_config`` for one call site, ``common/common.py`` imported both at
module level for three ``isinstance`` branches and one function, and
``variation/parameter_variation.py`` imported numpy for two seeding calls -- so they are
now held to the same rule as the rest. 1777 modules originally, 624 after the cluster
lane was deferred, 383 now, and nothing heavy at all.

``convert_dataclasses_to_dict`` shows the trick worth reusing: it consults numpy only
when ``sys.modules`` already has it. If nothing imported numpy, no object in the process
can be a numpy value, so the branches cannot match and importing it to discover that is
precisely the cost being avoided.
"""

import subprocess
import sys
import textwrap

import pytest

#: Nothing a plain ``vast`` invocation needs: the first five belong to the cluster lane
#: and the local Docker lane, the last two to config generation and scenario parsing.
FORBIDDEN = ("kubernetes", "boto3", "google", "paramiko", "docker",
             "numpy", "scenario_execution")


def _startup_modules() -> tuple[set, int]:
    """Import the CLI the way ``vast`` does, in a fresh interpreter."""
    script = textwrap.dedent("""
        import json, sys
        from robovast.common.cli.cli import load_plugins
        load_plugins()
        print(json.dumps({"mods": sorted(m for m in sys.modules if "." not in m),
                          "count": len(sys.modules)}))
    """)
    out = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, check=True)
    import json
    data = json.loads(out.stdout.strip().splitlines()[-1])
    return set(data["mods"]), data["count"]


@pytest.mark.parametrize("forbidden", FORBIDDEN)
def test_a_plain_vast_start_does_not_import(forbidden):
    mods, _ = _startup_modules()
    assert forbidden not in mods, (
        f"`{forbidden}` is imported when the CLI starts, so every `vast` command pays "
        f"for it. Move the import into the command body that needs it "
        f"(`# pylint: disable=import-outside-toplevel`), as the operator commands in "
        f"execution_utils/cli.py do.")


def test_the_startup_module_count_stays_in_the_hundreds():
    """A ceiling, not a target. It caught a 3x regression once and would again."""
    _, count = _startup_modules()
    assert count < 500, (
        f"the CLI now imports {count} modules at startup (was 383). Something gained a "
        f"module-level import of a heavy subsystem; see this module's docstring.")
