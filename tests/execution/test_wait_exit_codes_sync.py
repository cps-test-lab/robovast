# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Drift guard: a waiting command's exit codes are defined once, and only there.

``vast campaign wait`` and ``vast image wait`` are branched on by exit status, so their
codes are an interface. Each is a member of an enum in :mod:`robovast.execution.wait_exit`;
the commands raise those members, and every list of the codes -- the ``--help``, the
``next_step`` an MCP tool hands back, the run prompt, the table in ``docs/client.rst`` -- is
rendered from them. A hand-written copy drifts, since nothing fails when the command gains a
code and the copy does not. So these fail on a code stated **by number** anywhere a reader or
an agent would take it as the contract, and on a command exiting with anything but a member.
"""

import ast
import importlib
import pathlib
import re

import click
import pytest

from robovast.execution.wait_exit import CampaignWaitExit, ImageWaitExit, WaitExit

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CLIENT = _ROOT / "src" / "robovast_client" / "robovast" / "client"

#: Where a code stated by number would be read as the contract.
_SCANNED = ("docs", "skills", "hooks", "src")
_SUFFIXES = {".py", ".rst", ".md", ".txt"}
#: The definition itself, which is the one place the numbers are written.
_DEFINITION = _ROOT / "src" / "robovast_client" / "robovast" / "execution" / "wait_exit.py"

#: A waiting command named in the file.
_WAIT_COMMAND = re.compile(r"\b(?:campaign|image) wait\b")
#: A paragraph about waiting: a code by number there is read as a waiting command's.
_ABOUT_WAITING = re.compile(
    r"\b(?:(?:campaign|image) wait|waiter|the wait|stall(?:s|ed)?|health finding)\b", re.I)
#: "exit 4", "exits 4", "(exit 0 finished", "exit code 2", "codes ``4`` and": a code by number.
#: Not "exit 135", and not ``sys.exit(1)``, whose parenthesis follows with no space.
_BY_NUMBER = re.compile(
    r"\b(?:exit(?:s|ed|ing)?(?:\s+(?:code|status))?\s+\(?|codes?\s+)"
    r"`{0,2}([0-5])`{0,2}(?!\d|\.\d)", re.I)
#: A table row whose first cell is a bare code: ``| 4 |`` or ``* - ``4````.
_TABLE_ROW = re.compile(r"^\s*(?:\|\s*|\*\s+-\s+)`{0,2}([0-5])`{0,2}\s*(?:\||$)", re.M)


def by_number(text: str) -> list[str]:
    """Every place *text* states a waiting command's exit code by its number."""
    found = []
    mentions_wait = bool(_WAIT_COMMAND.search(text))
    # A blank line, or a comment line with nothing on it, ends a paragraph.
    for paragraph in re.split(r"\n[ \t]*#?[ \t]*\n", text):
        if _ABOUT_WAITING.search(paragraph):
            found += [m.group(0).strip() for m in _BY_NUMBER.finditer(paragraph)]
        if mentions_wait:
            found += [m.group(0).strip() for m in _TABLE_ROW.finditer(paragraph)]
    return found


def _scanned_files():
    for top in _SCANNED:
        for path in sorted((_ROOT / top).rglob("*")):
            if (path.suffix in _SUFFIXES and path.is_file() and path != _DEFINITION
                    and "_build" not in path.parts and "node_modules" not in path.parts):
                yield path


@pytest.mark.parametrize("text", [
    "Wait with `vast campaign wait <id>` (exit 0 finished, 1 failed/stopped).",
    "the waiter ends on a stall (exit 4)",
    "ending ``vast campaign wait`` at exit 4. Read the age",
    "`vast campaign wait` exits 4 on a stall and 5 on a finding",
    "``vast campaign wait``\n\n.. list-table::\n\n   * - ``3``\n     - No phase.",
    "`vast campaign wait`:\n\n| exit | means |\n|---|---|\n| 4 | stalled |",
    "Codes ``4`` and ``5`` are why this waiter exists.",
    "block until every build is done: vast image wait b1 (exit 0 built, 1 failed)",
])
def test_the_guard_recognises_a_code_stated_by_number(text):
    """The shapes the hand-written copies took. A guard that matched none of them would pass
    on exactly the drift it is here to stop."""
    assert by_number(text)


@pytest.mark.parametrize("text", [
    "a container that overruns shared memory dies of SIGBUS (exit 135)",
    "print the failure and exit 1",
    "raise SystemExit(CampaignWaitExit.STALLED)",
    "if failed: sys.exit(1)",
    "`vast campaign wait` ends as ``STALLED``, see :ref:`client-wait-exit-codes`",
])
def test_the_guard_leaves_other_exit_codes_and_names_alone(text):
    assert not by_number(text)


def test_no_wait_exit_code_is_stated_by_number():
    """Name the member (``CampaignWaitExit.STALLED``), point at the table
    (:ref:`client-wait-exit-codes`), or render the list from the enum (``.summary()``,
    ``@documents_exit_codes``) -- never write the number."""
    offenders = [f"{path.relative_to(_ROOT)}: {hit!r}"
                 for path in _scanned_files()
                 for hit in by_number(path.read_text(encoding="utf-8", errors="replace"))]
    assert not offenders, (
        "a waiting command's exit code is stated by number outside "
        "robovast.execution.wait_exit:\n  " + "\n  ".join(offenders))


def _exit_values(func: ast.FunctionDef):
    """The argument of every ``raise SystemExit(...)`` and ``sys.exit(...)`` in *func*."""
    for node in ast.walk(func):
        call = node.exc if isinstance(node, ast.Raise) else node
        if not isinstance(call, ast.Call):
            continue
        name = ast.unparse(call.func)
        if name in ("SystemExit", "sys.exit", "exit"):
            yield call.args[0] if call.args else None


def _members(expr, enum: type[WaitExit]) -> list:
    """The members *expr* can evaluate to; fails on anything else (a literal, a variable)."""
    if isinstance(expr, ast.IfExp):
        return _members(expr.body, enum) + _members(expr.orelse, enum)
    source = ast.unparse(expr) if expr is not None else "<no argument>"
    assert (isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name)
            and expr.value.id == enum.__name__ and expr.attr in enum.__members__), (
        f"exits with {source}, not a member of {enum.__name__}")
    return [enum[expr.attr]]


@pytest.mark.parametrize("module, functions, enum", [
    ("campaign_cli.py", ["wait"], CampaignWaitExit),
    ("cli.py", ["image_wait", "_wait_for_builds"], ImageWaitExit),
])
def test_the_command_exits_only_with_members_and_with_every_one(module, functions, enum):
    """A literal is a second definition of a code; a member nothing raises is a documented
    code the command never exits with."""
    tree = ast.parse((_CLIENT / module).read_text(encoding="utf-8"))
    defs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    raised = {member for name in functions
              for value in _exit_values(defs[name])
              for member in _members(value, enum)}
    assert raised == set(enum), f"never exited with: {sorted(set(enum) - raised)}"


@pytest.mark.parametrize("module, group, enum", [
    ("robovast.client.campaign_cli", "campaign", CampaignWaitExit),
    ("robovast.client.cli", "image", ImageWaitExit),
])
def test_the_help_lists_every_code_from_the_enum(module, group, enum):
    command = getattr(importlib.import_module(module), group).commands["wait"]
    text = command.get_help(click.Context(command, info_name=f"{group} wait"))
    for member in enum:
        assert re.search(rf"^\s+{member.value}\s+{member.name}\s", text, re.M), member.name


@pytest.mark.parametrize("enum", [CampaignWaitExit, ImageWaitExit])
def test_the_docs_render_one_table_per_command(enum):
    """One canonical table, at the label every other page links to."""
    directive = f".. wait-exit-codes:: {enum.__module__}.{enum.__name__}"
    uses = [p.relative_to(_ROOT) for p in (_ROOT / "docs").rglob("*.rst")
            for _ in range(p.read_text(encoding="utf-8").count(directive))]
    assert uses == [pathlib.Path("docs/client.rst")], uses
