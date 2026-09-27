# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What the exit status of a ``vast`` verb says: the one definition of each code.

Every verb shares :class:`CommonExit`. A waiting verb (``vast campaign wait``,
``vast image wait``) adds outcome codes from :data:`FIRST_OUTCOME_CODE` up, so a script
can branch on the exit status without parsing anything and never mistakes an outcome for
a usage error. Each code is defined here once, with its meaning; the commands raise these
members, and every other place that lists the codes renders them from here -- the
command's ``--help``, the ``next_step`` an MCP tool hands back, the run-experiments prompt,
and the tables in ``docs/client.rst`` (rendered by ``docs/_ext/wait_exit_codes.py``).
Prose elsewhere names a member, never its number;
``tests/execution/test_wait_exit_codes_sync.py`` keeps it that way.

Deliberately free of imports beyond the standard library: the CLI builds its help text
from this at import time, and a ``vast`` invocation must not pay for the poll loop's
dependencies to show it.
"""

import re
import textwrap
from enum import IntEnum

#: Where :func:`documents_exit_codes` puts the codes, alone on a line of the docstring.
EXIT_CODES_PLACEHOLDER = "{exit_codes}"


class CommonExit(IntEnum):
    """The exit codes every ``vast`` verb shares.

    ``USAGE_ERROR`` is click's, raised before the verb runs, and cannot be renumbered; so
    no verb gives its code another meaning.
    """

    def __new__(cls, code: int, meaning: str):
        member = int.__new__(cls, code)
        member._value_ = code
        member.meaning = meaning
        member.full_meaning = meaning
        return member

    SUCCESS = 0, "Success: the command did what it was asked."
    FAILED = 1, "Failed; the message on stderr says why."
    USAGE_ERROR = (2, "Usage error: an unknown command or option, or a missing or malformed "
                      "argument. Nothing was run.")


#: The lowest code a verb may give an outcome of its own.
FIRST_OUTCOME_CODE = max(CommonExit) + 1


class WaitExit(IntEnum):
    """Base for a waiting command's exit codes; a subclass declares one member per code.

    Each member is ``CODE, label, meaning`` with an optional ``still_running``: *label* is
    the few words an inline list uses, *meaning* the sentence the help and the docs use, and
    *still_running* marks a code that ends the wait while the waited-on work goes on --
    a hand-off to the caller, not an ending. *CODE* is ``CommonExit.SUCCESS`` or
    ``CommonExit.FAILED``, or at least :data:`FIRST_OUTCOME_CODE`; a member that means the
    same in two subclasses has the same name and code in both.
    """

    def __new__(cls, code: int, label: str, meaning: str, still_running: bool = False):
        member = int.__new__(cls, code)
        member._value_ = code
        member.label = label
        member.meaning = meaning
        member.still_running = still_running
        return member

    @classmethod
    def summary(cls) -> str:
        """One line for a ``next_step`` or a prompt: every code with its label."""
        over = [f"{m.value} {m.label}" for m in cls if not m.still_running]
        running = [f"{m.value} {m.label}" for m in cls if m.still_running]
        text = "exit " + ", ".join(over)
        if running:
            text += "; still running: " + ", ".join(running)
        return text

    @classmethod
    def help_block(cls) -> str:
        r"""The codes as a click help block, one per line (``\b`` keeps click from rewrapping).

        Includes click's usage error, which the command can exit with too.
        """
        rows = sorted([*cls, CommonExit.USAGE_ERROR])
        width = max(len(m.name) for m in rows)
        lines = ["\b", "Exit codes:"]
        for m in rows:
            head = f"  {m.value}  {m.name.ljust(width)}  "
            lines += textwrap.wrap(m.full_meaning, width=76, initial_indent=head,
                                   subsequent_indent=" " * len(head))
        return "\n".join(lines)

    @property
    def full_meaning(self) -> str:
        """*meaning*, plus what a still-running code leaves behind."""
        if self.still_running:
            return f"{self.meaning} Still running, and nothing is waiting on it now."
        return self.meaning


class CampaignWaitExit(WaitExit):
    """``vast campaign wait``: how the campaign ended, or why the wait ended without it."""

    FINISHED = CommonExit.SUCCESS, "finished", "Finished, past postprocessing."
    FAILED = CommonExit.FAILED, "failed/stopped", "Failed, or stopped."
    STOPPED_WAITING = (3, "stopped waiting",
                       "Stopped waiting: --timeout elapsed, or the service stopped answering. "
                       "The campaign is unaffected and can be waited on again.")
    NO_PHASE = (4, "no such campaign",
                "The service knows no phase for this id: a typo, or a campaign that died "
                "before recording one.")
    STALLED = (5, "stalled",
               "Stalled: nothing has completed for longer than one run may take.", True)
    HEALTH_FINDING = (6, "simulator fault",
                      "A running job's simulator reported an error-level health finding "
                      "about itself.", True)


class ImageWaitExit(WaitExit):
    """``vast image wait``, and ``vast image build`` unless ``--no-wait``: how the builds ended."""

    BUILT = CommonExit.SUCCESS, "built", "Every build finished and its image is available."
    FAILED = CommonExit.FAILED, "failed", "At least one build failed."
    STOPPED_WAITING = (3, "stopped waiting",
                       "Stopped waiting: --timeout elapsed, or the service stopped answering. "
                       "The builds are unaffected and can be waited on again.")


def documents_exit_codes(codes: type[WaitExit]):
    """Decorator for a click command: render *codes* into its docstring, and so its ``--help``.

    Goes *below* the ``@command()`` decorator, since click reads the docstring when that one
    runs. The placeholder must stand alone on its line; the block takes that line's
    indentation, so the docstring still dedents as one.
    """
    pattern = re.compile(rf"^([ \t]*){re.escape(EXIT_CODES_PLACEHOLDER)}[ \t]*$", re.M)

    def apply(command):
        doc = command.__doc__ or ""
        if len(pattern.findall(doc)) != 1:
            raise ValueError(f"{command.__name__}'s docstring needs {EXIT_CODES_PLACEHOLDER} "
                             f"alone on one line, exactly once")
        command.__doc__ = pattern.sub(
            lambda m: textwrap.indent(codes.help_block(), m.group(1)), doc)
        return command
    return apply
