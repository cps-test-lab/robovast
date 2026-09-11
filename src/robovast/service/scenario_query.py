# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Asking the scenario image whether the campaign's scenario parses -- imports and all.

The sibling of :mod:`robovast.service.world_query`, and it exists for the same reason: a
question only a container can answer, whose failure is otherwise per-trial.

**Why the image and not this host.** An OpenSCENARIO 2 file's ``import osc.<library>``
lines resolve against the libraries *installed where it runs*, registered as
``scenario_execution.osc_libraries`` entry points. So "does this scenario parse?" has no
answer outside an image: the service's own environment carries a different set, and a
scenario that parses here can die at the first line of every trial. The failure is quiet
in exactly the wrong way -- the campaign is composed, scheduled, pulled and started, each
run dies during parse, and the campaign reports finished.

**Why a full parse and not a list of imports.** Reading the ``import`` lines and comparing
them against what the image registers would need this module to know how a library name
maps to a package, which is a vocabulary that goes stale the moment someone ships a new
one. Parsing asks the question the runner will ask, in the words the runner will use, and
catches the syntax errors and unresolved references on the way -- the same class of defect
and the same fix, for no extra call.

``robovast`` never imports ``scenario_execution``: the parse runs inside the container, as
a script this module writes and the image's own interpreter executes.
"""

import json
import logging
import shlex

logger = logging.getLogger(__name__)

#: What the in-container script prints: exactly one JSON line on stdout, whatever happened.
#: A verdict is data, not an exit code -- "the scenario is broken" and "I could not ask"
#: must be distinguishable, and a shell that exits 1 for both makes them the same answer.
#: A non-zero exit or a missing line therefore means only the second.
_PROBE = r'''
import json, sys

path = {path}

def say(**payload):
    sys.stdout.write("\n" + json.dumps(payload) + "\n")
    sys.exit(0)

try:
    import py_trees
    from scenario_execution.model.model_resolver import resolve_internal_model
    from scenario_execution.model.osc2_parser import OpenScenario2Parser
    from scenario_execution.utils.logging import Logger
except Exception as exc:
    say(unchecked="this image has no scenario_execution to parse with (%s: %s)"
        % (type(exc).__name__, exc))

try:
    parser = OpenScenario2Parser(Logger("validate", False))
    parsed = parser.parse_file(path, log_model=False)
    model = parser.load_internal_model(parsed, path, log_model=False, debug=False)
except Exception as exc:
    # The TYPE as well as the message: an exception raised bare stringifies to ""
    # and would otherwise arrive as a refusal that names nothing.
    say(error="%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__)

# RESOLUTION, not only the model. Building the model checks that the file is well formed and
# that every `import osc.<library>` resolves; it does not bind an invocation to the action it
# names, so an argument the action does not take, or a name two imported libraries both
# declare, is invisible until the run -- once per trial, with the campaign reporting finished.
try:
    resolve_internal_model(model, py_trees.composites.Sequence(name="", memory=True),
                           parser.logger, False)
except Exception as exc:
    message = "%s: %s" % (type(exc).__name__, exc) if str(exc) else type(exc).__name__
    # A parameter with no value is not a defect in the scenario: the configuration supplies
    # those, and this check has none to give. Resolution binds arguments BEFORE it needs a
    # parameter's value, so stopping here still leaves the invocations checked -- what it does
    # not check is whatever the file does after its first unbound parameter.
    if "is used but has no value" not in str(exc):
        say(error=message)

say(ok=True)
'''


#: How much of the container's own output to carry into a problem. Enough for a traceback's
#: last frames, short enough not to bury the verdict it is attached to.
_TAIL_CHARS = 600


def _said(output: str, verdict_line: str = "") -> str:
    """What the container printed, minus the verdict line and the lead-in it prints every time.

    Carried on every failing path, because the JSON message alone is only as good as the
    exception behind it: an import that failed inside a package, a parser that logged the real
    detail before raising, and a script that died before printing anything all leave their
    evidence here and nowhere else. Bounded and taken from the END, since that is where a
    traceback's cause is.
    """
    lines = [line for line in (output or "").splitlines()
             if line.strip() and line.strip() != verdict_line.strip()]
    text = "\n".join(lines).strip()
    return text[-_TAIL_CHARS:] if text else ""


def _problem(message: str, severity: str = "error") -> dict:
    """One structured problem, in the shape ``validate_project_file`` returns."""
    return {"stage": "scenario", "config": None, "field": "execution.scenario_file",
            "message": message, "severity": severity}


def _verdict(output: str):
    """``(payload, the raw line)``, or ``(None, "")`` when the probe produced no verdict.

    The line comes back so a caller can strip it from the output it reports beside the verdict,
    rather than printing the same message twice.

    Scanned from the end: the image's own startup writes to stdout (a ROS setup, a
    deprecation notice), so the line the probe printed is the last JSON object, not the
    first line of the output.
    """
    for line in reversed((output or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed, line
    return None, ""


def scenario_problems(exec_call, *, workspace_id: str, config_path: str,
                      scenario_path: str) -> list:
    """Does this campaign's scenario parse AND resolve in the image that will run it?

    Resolve, not only parse: building the model checks the file's shape and that every
    ``import osc.<library>`` resolves, and stops before an invocation is bound to the action it
    names. An argument the action does not take, or a name two imported libraries both declare,
    survives that and kills every trial instead.

    *scenario_path* is the scenario file's path relative to the workspace root, which is
    where the exec lane mounts the project (``/sources/<workspace_id>``) -- the same
    rewrite ``world_query`` performs, done here by construction because this module builds
    the whole command rather than adapting a backend's.

    One problem, or none. An empty list means the parse ran and succeeded; anything that
    could not be asked comes back as ``unchecked`` naming what would settle it. Silence
    never stands for a pass.
    """
    from robovast.common.errors import ExecPathUnavailable
    from robovast.service.interface import ExecRequest

    container_path = f"/sources/{workspace_id}/{scenario_path.lstrip('/')}"
    script = _PROBE.format(path=repr(container_path))
    command = f"python3 -c {shlex.quote(script)}"
    try:
        result = exec_call(ExecRequest(
            command=command, workspace_id=workspace_id, config_path=config_path,
            # The scenario container, because that is where the scenario runs -- and the
            # one container every campaign has, including one with no simulator at all.
            container="scenario",
            # The held pool: a read-only question, cheap to repeat, and it must not
            # disturb a container the caller is holding.
            query=True))
    except ExecPathUnavailable as exc:
        # Before the arm below, whose remedy is a service log and a bug report: an exec path
        # that does not stream is a property of the deployment, there is no traceback to
        # read, and pointing at the service log sends a caller to look for a defect that is
        # not there.
        logger.warning("the scenario check did not run: %s", exc)
        return [_problem(
            f"this campaign's scenario was NOT parsed: {exc}. Next: nothing about the "
            ".vast changes this -- the scenario can only be parsed where a command can run "
            "in a container.",
            severity="unchecked")]
    except Exception as exc:  # noqa: BLE001 - the check crashing is not a bad campaign
        logger.warning("the scenario check did not run: %s", exc)
        return [_problem(
            f"this campaign's scenario was NOT parsed: the check itself failed here "
            f"({exc}). Next: nothing about the .vast changes this -- it is a defect in "
            "the service, whose log carries the traceback (`vast service log`).",
            severity="unchecked")]

    output = (result.stdout or "") + (result.stderr or "")
    verdict, line = _verdict(output)
    said = _said(output, line)
    if verdict is None:
        return [_problem(
            "this campaign's scenario was NOT parsed: the check produced no verdict in "
            f"the scenario image (exit {result.exit_code})"
            + (f": {said}" if said else "")
            + ". Next: build the image first (build_experiment_image), then validate "
              "again.", severity="unchecked")]
    if verdict.get("ok"):
        return []
    if verdict.get("unchecked"):
        return [_problem(
            f"this campaign's scenario was NOT parsed: {verdict['unchecked']}. Next: this "
            "is a property of the image, not of the .vast -- a scenario image that cannot "
            "parse a scenario cannot run one either." + (f" It said: {said}" if said else ""),
            severity="unchecked")]
    # The parser's own message, verbatim and unwrapped: it names the file, the line and the
    # column, and a library it could not find by the name the author wrote. Rephrasing it
    # would lose the position, which is the whole of what makes it actionable. Whatever else
    # the image printed follows it, since a parser that logged its detail before raising put
    # it there and nowhere else.
    return [_problem(
        f"the scenario does not parse in the image that would run it: "
        f"{verdict.get('error')}" + (f" It also said: {said}" if said else ""))]
