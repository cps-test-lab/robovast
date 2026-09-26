# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``vast exec wait`` — the way a caller waits for a campaign without holding a request.

An agent harness can background a shell command and be notified when it exits; it cannot
do that with an MCP call, which occupies the conversation for as long as it blocks. For a
campaign that may run for days that difference is the whole point, so the campaign wait
lives here rather than on the tool surface.

The exit code is the contract: a script (or a harness) branches on it without parsing
anything, and "the campaign failed" must not look like "I stopped waiting".
"""

import contextlib
import time

import pytest
from click.testing import CliRunner

from robovast.client import campaign_cli
from robovast.client.status import Phase, Status


@pytest.fixture
def service(monkeypatch):
    """Point the command at a fake service; *phases* is consumed one per poll."""
    def _install(phases, **status_fields):
        seen = []

        class _Client:
            def get_status(self, campaign_id):
                phase = phases[min(len(seen), len(phases) - 1)]
                seen.append(phase)
                return Status(phase=phase, campaign_id=campaign_id, **status_fields)

        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield _Client(), "fake service"

        monkeypatch.setattr(campaign_cli, "service_client", _client)
        return seen
    return _install


# both _run() and _run(name, *flags) are used
def _run(campaign="c1", *args):  # pylint: disable=keyword-arg-before-vararg
    return CliRunner().invoke(campaign_cli.campaign,
                              ["wait", campaign, "--interval", "0.01", *args])


def test_a_finished_campaign_exits_zero(service):
    service([Phase.RUNNING, Phase.FINISHING, Phase.FINISHED])
    result = _run()
    assert result.exit_code == 0
    assert "finished" in result.output


def test_it_waits_through_finishing(service):
    """``finishing`` is the window where share and postprocessing still run. Exiting
    there would report a campaign as over before its metrics exist — the original bug."""
    seen = service([Phase.FINISHING, Phase.FINISHING, Phase.FINISHED])
    assert _run().exit_code == 0
    assert len(seen) >= 3  # it kept polling rather than stopping at `finishing`


def test_a_failed_campaign_exits_one(service):
    service([Phase.FAILED], error="image build failed")
    result = _run()
    assert result.exit_code == 1
    assert "image build failed" in result.output


def test_a_stopped_campaign_exits_one(service):
    service([Phase.STOPPED])
    assert _run().exit_code == 1


def test_a_timeout_is_its_own_exit_code(service):
    """Distinct from failure: the campaign is still running and can be waited on again.
    Collapsing the two would make a caller treat a live campaign as a dead one."""
    service([Phase.RUNNING])
    result = _run("c1", "--timeout", "0.05")
    assert result.exit_code == 2


def test_a_finished_campaign_whose_postprocessing_failed_says_so(service):
    """It exits 0 — the runs are the deliverable and they passed — so the reason to look
    for missing CSVs has to be *said*, or a successful exit promises data that is absent.
    """
    service([Phase.FINISHED], postprocessing_error="conversion died")
    result = _run()
    assert result.exit_code == 0
    assert "postprocessing failed" in result.output


def test_no_phase_at_all_is_its_own_exit_code(service):
    """``unknown`` is terminal but it is not failure, and conflating them misleads.

    The service reports it for two things: an id that names no campaign, and a campaign
    that died before it ever wrote to the store. Neither is "the campaign ran and failed",
    which is what exit 1 told a caller — sending them to hunt for a failure that never
    happened, or worse, to believe a typo'd id had really run and broken.
    """
    service([Phase.UNKNOWN])
    result = _run()
    assert result.exit_code == 3
    assert "knows no phase" in result.output


def test_a_service_that_never_answers_ends_the_wait_instead_of_hanging():
    """``campaign_wait``'s half of the same bound as ``image_build_wait``: one dropped read
    is a hiccup and must not end a wait, but every read failing forever is a hang wearing
    tolerance as a disguise. Both waits apply the rule from one place, so this and its
    sibling in ``test_image_build_wait`` must both hold."""
    from robovast.execution.campaign_wait import wait_for_campaign_status
    from robovast.execution.poll_health import PollsStopped

    class _Client:
        def get_status(self, campaign_id):
            raise RuntimeError("connection refused")

    with pytest.raises(PollsStopped) as excinfo:
        wait_for_campaign_status("c1", client=_Client(), interval=0, stale_limit_s=0.05)
    assert "connection refused" in str(excinfo.value)
    # Must not read as a campaign failure: nothing here knows anything about the campaign.
    assert "Nothing here says the work failed" in str(excinfo.value)


def _stalled_status(campaign_id, age, deadline=300):
    """A live campaign whose progress last advanced *age* seconds ago."""
    return Status(phase=Phase.RUNNING, campaign_id=campaign_id,
                  progress_since=time.time() - age, progress_deadline_s=deadline)


@pytest.fixture
def statuses(monkeypatch):
    """Point the command at a fake service that yields prepared Status objects."""
    def _install(sequence):
        seen = []

        class _Client:
            def get_status(self, campaign_id):  # pylint: disable=unused-argument
                status = sequence[min(len(seen), len(sequence) - 1)]
                seen.append(status)
                return status

        @contextlib.contextmanager
        def _client(*_a, **_k):
            yield _Client(), "fake service"

        monkeypatch.setattr(campaign_cli, "service_client", _client)
        return seen
    return _install


def test_a_stall_ends_the_wait_with_its_own_code(statuses):
    """The defect this exists for: a stalled campaign never reaches a terminal phase, so a
    waiter that stopped only on terminality never returned and nobody was told. Exit 4 is
    distinct because every other exit here means the campaign is over or unreachable."""
    statuses([_stalled_status("c1", 10), _stalled_status("c1", 10),
              _stalled_status("c1", 999)])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 4
    assert "no progress for" in result.output
    # "the waiter returned" must not read as "the run ended".
    assert "STILL RUNNING" in result.output


def test_a_stall_that_was_already_true_is_not_news(statuses):
    """Only a *rising edge* exits. Otherwise the design eats itself: the exit-4 message
    tells the caller to re-run this command after diagnosing, and a fresh waiter would
    re-observe the same stall on its first poll and exit instantly -- forever -- leaving
    no way to resume waiting on the very state it reports."""
    statuses([_stalled_status("c1", 999)])
    result = _run("c1", "--timeout", "0.2")
    # 2 == "stopped waiting" (--timeout), i.e. it kept waiting rather than exiting on it.
    assert result.exit_code == 2


def test_a_campaign_with_no_declared_timeout_never_exits_four(statuses):
    """``stalled`` is ``None`` without ``execution.timeout``, and None is not a verdict.
    Treating it as one would exit on every campaign that declared no budget."""
    statuses([_stalled_status("c1", 10, deadline=None),
              _stalled_status("c1", 99999, deadline=None)])
    result = _run("c1", "--timeout", "0.2")
    assert result.exit_code == 2


# -- exit 5: the run's own simulator said something is wrong ---------------------------------


def _finding_status(campaign_id, findings, deadline=None, skipped=()):
    """A live campaign carrying *findings*, and deliberately no stall: exit 5 must not need one.

    ``progress_deadline_s`` defaults to ``None`` here for the property that matters -- a health
    finding is true within a minute of the fault and needs no declared budget, which is exactly
    what a stall verdict cannot do.
    """
    return Status(phase=Phase.RUNNING, campaign_id=campaign_id,
                  progress_since=time.time(), progress_deadline_s=deadline,
                  health=findings, health_skipped=list(skipped))


def _finding(check="sim-time-rate", level="error", job="nav-1/1"):
    return {"job_name": job, "level": level, "check": check, "detail": "sim advanced 3.1s in 60s"}


def test_a_fresh_error_finding_ends_the_wait_with_its_own_code(statuses):
    """The point of exit 5: nobody would otherwise be told. A run whose simulator is wedged holds
    ``running`` for its whole life, and with no ``execution.timeout`` there is not even a stall
    verdict to fall back on."""
    statuses([_finding_status("c1", []), _finding_status("c1", [_finding()])])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 5
    assert "sim-time-rate" in result.output and "sim advanced 3.1s" in result.output
    # Three things the message must say, and each was got wrong by a draft of it.
    assert "NOT touched" in result.output, "a waiter stopping must not read as a run stopping"
    assert "STILL RUNNING" in result.output
    assert "vast campaign wait c1" in result.output, "the way back has to be named"


def test_a_finding_exit_says_what_to_do_next_from_what_the_finding_already_told_you(statuses):
    """A finding names the job and names the check, so the next step starts from those two facts.
    Carrying the *stall* step here sends a reader off to ask what the job was doing -- the one
    question the finding has just answered."""
    statuses([_finding_status("c1", []), _finding_status("c1", [_finding()])])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 5
    assert "next:" in result.output
    assert "get_job_state" in result.output
    assert "simulator's documentation" in result.output, \
        "the slug is the simulator's, so the reader has to be sent to the simulator's docs"
    assert "no progress for" not in result.output, \
        "the stall's ladder is about a budget this exit did not use"


def test_a_finding_exit_reports_the_checks_that_did_not_run(statuses):
    """The moment one check fires is the moment a reader starts treating the rest of the run as
    fine. A check that reached no verdict is not a finding and must not be rendered as one -- but
    it must be said, or its absence reads as a pass."""
    statuses([
        _finding_status("c1", []),
        _finding_status("c1", [_finding()],
                        skipped=["nav-1/1: check 1 (robot-motion): no rows in sim_poses.csv"]),
    ])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 5
    assert "check did not run" in result.output
    assert "robot-motion" in result.output


def test_a_finding_already_present_is_not_news(statuses):
    """The same rule as the stall, and load-bearing for the same reason: the exit message says to
    re-run this command after diagnosing, so a fresh waiter must not exit on what it inherits."""
    statuses([_finding_status("c1", [_finding()])])
    result = _run("c1", "--timeout", "0.2")
    assert result.exit_code == 2  # kept waiting, then hit --timeout


def test_a_second_finding_from_a_new_check_still_exits(statuses):
    """The baseline is per ``check``, not "any finding": a run already warning about one thing
    must still be able to report a different fault."""
    statuses([_finding_status("c1", [_finding(check="robot-motion")]),
              _finding_status("c1", [_finding(check="robot-motion"), _finding()])])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 5
    assert "sim-time-rate" in result.output


def test_the_same_check_firing_again_does_not_exit(statuses):
    """A check that keeps firing is one fault, not a stream of exits."""
    statuses([_finding_status("c1", [_finding()]), _finding_status("c1", [_finding()]),
              _finding_status("c1", [_finding()])])
    result = _run("c1", "--timeout", "0.2")
    assert result.exit_code == 2


def test_a_warning_never_ends_the_wait(statuses):
    """``warn`` is the level RoboVAST reads and then does nothing about: a robot standing still is
    often correct. It surfaces on ``get_job_state``; it must not stop a wait."""
    statuses([_finding_status("c1", []),
              _finding_status("c1", [_finding(check="robot-motion", level="warn")])])
    result = _run("c1", "--timeout", "0.2")
    assert result.exit_code == 2


def test_a_terminal_campaign_with_findings_exits_on_its_phase(statuses):
    """What a run reported while it was wedged is history once it is over: the phase decides, and
    the results are the record. Exiting 5 on a finished campaign would report a completed run as
    an interrupted wait."""
    statuses([_finding_status("c1", []),
              Status(phase=Phase.FINISHED, campaign_id="c1", health=[_finding()])])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 0


def test_a_stall_and_a_finding_together_report_the_finding(statuses):
    """Both are true and only one message can lead. The finding names a fault class where the
    stall says only "nothing finished in time", so it is the one worth reading first."""
    stalled = Status(phase=Phase.RUNNING, campaign_id="c1", progress_deadline_s=300,
                     progress_since=time.time() - 999, health=[_finding()])
    statuses([_finding_status("c1", []), stalled])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 5
    assert "NOT touched" in result.output


# -- an expected check: reported, never an exit ------------------------------------------------


def test_an_ignored_check_does_not_end_the_wait_and_the_phase_decides(statuses):
    """A world that trips a check on every run by design must not make a healthy campaign read as
    a dead one: with the check named, the wait runs on to the campaign's own terminal phase and
    returns that phase's code."""
    statuses([_finding_status("c1", []), _finding_status("c1", [_finding()]),
              _finding_status("c1", [_finding(job="nav-2/1")]),
              Status(phase=Phase.FINISHED, campaign_id="c1")])
    result = _run("c1", "--ignore-check", "sim-time-rate", "--timeout", "5")
    assert result.exit_code == 0
    assert "NOT touched" not in result.output


def test_an_ignored_check_on_a_failed_campaign_still_exits_one(statuses):
    """Ignoring a check changes what ends the wait, never what the campaign's end means."""
    statuses([_finding_status("c1", []), _finding_status("c1", [_finding()]),
              Status(phase=Phase.FAILED, campaign_id="c1", error="runs failed")])
    result = _run("c1", "--ignore-check", "sim-time-rate", "--timeout", "5")
    assert result.exit_code == 1


def test_an_ignored_finding_is_still_printed_once_and_marked(statuses):
    """Expected is not hidden: the waiter's line is the only place it shows an ignored check, so
    it is printed -- once per check, however many jobs report it."""
    statuses([_finding_status("c1", []), _finding_status("c1", [_finding()]),
              _finding_status("c1", [_finding(), _finding(job="nav-2/1")]),
              Status(phase=Phase.FINISHED, campaign_id="c1")])
    result = _run("c1", "--ignore-check", "sim-time-rate", "--timeout", "5")
    assert result.exit_code == 0
    lines = [line for line in result.output.splitlines() if "sim-time-rate" in line]
    assert len(lines) == 1, result.output
    assert "sim advanced 3.1s" in lines[0]
    assert "[ignored: --ignore-check]" in lines[0]


def test_an_unignored_new_check_still_exits_five(statuses):
    """Naming one expected check must not blind the waiter to every other fault."""
    statuses([_finding_status("c1", []),
              _finding_status("c1", [_finding(), _finding(check="robot-motion")])])
    result = _run("c1", "--ignore-check", "sim-time-rate", "--timeout", "5")
    assert result.exit_code == 5
    assert "robot-motion" in result.output
    assert "[ignored: --ignore-check]" in result.output
    # The way back keeps the waiter's own flags, and offers the new check as one.
    assert "vast campaign wait c1 --ignore-check sim-time-rate" in result.output
    assert "--ignore-check robot-motion" in result.output


def test_a_check_the_campaign_declares_advisory_needs_no_flag(statuses):
    """``execution.advisory_checks`` reaches every waiter through the status, so a waiter started
    without flags agrees with one started with them."""
    def declared(findings):
        status = _finding_status("c1", findings)
        status.advisory_checks = ["sim-time-rate"]
        return status

    statuses([declared([]), declared([_finding()]),
              Status(phase=Phase.FINISHED, campaign_id="c1")])
    result = _run("c1", "--timeout", "5")
    assert result.exit_code == 0
    assert "[ignored: execution.advisory_checks]" in result.output


def test_a_blank_check_slug_is_refused(statuses):
    """A blank slug matches no check, so accepting it would read as configured and do nothing."""
    statuses([_finding_status("c1", [])])
    result = _run("c1", "--ignore-check", " ")
    assert result.exit_code != 0
    assert "cannot be blank" in result.output
