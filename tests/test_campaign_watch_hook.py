# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The Claude Code hook that stops a turn ending silently mid-campaign.

`start_campaign` returns as soon as the campaign is named; the campaign runs on for
minutes or days. The bug this guards is an agent reading one status, seeing "running",
and ending its turn — so the user is told a campaign finished when it had barely begun.

It is a floor under a law, not the law: **block once, then allow**. A sweep can
legitimately run for days and no in-session wait survives that, so holding a session
hostage would be a worse failure than the one being fixed. One block turns a silent exit
into a stated decision, which is the actual defect.

It ships in the plugin rather than as loose glue, so these tests load it by path.
"""

import importlib.util
import json
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "campaign_watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("campaign_watch", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hook(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    return _load()


def _ledger(hook, session="s1"):
    return hook._ledger_path({"session_id": session})  # noqa: SLF001


def _start(hook, campaign, session="s1"):
    hook.record({"session_id": session, "tool_response": {"campaign_id": campaign}},
                _ledger(hook, session))


def _check(hook, capsys, session="s1"):
    hook.check({"session_id": session}, _ledger(hook, session))
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_a_started_campaign_blocks_the_first_turn_end(hook, capsys):
    _start(hook, "camp-a")
    decision = _check(hook, capsys)
    assert decision["decision"] == "block"
    assert "camp-a" in decision["reason"]


def test_it_blocks_once_and_then_allows(hook, capsys):
    """Blocking until done would hold a three-day sweep's session hostage — a worse
    failure than the silent exit it is fixing."""
    _start(hook, "camp-a")
    assert _check(hook, capsys) is not None
    assert _check(hook, capsys) is None


def test_every_pending_campaign_is_named_not_just_the_first(hook, capsys):
    """The bug this shipped with: it marked *all* pending ids as warned but named only
    ``pending[0]``. Start three, hear about one, and the other two are silently recorded
    as handled — exactly the loss the hook exists to prevent, reintroduced whenever more
    than one campaign is in flight."""
    for cid in ("camp-a", "camp-b", "camp-c"):
        _start(hook, cid)
    reason = _check(hook, capsys)["reason"]
    assert all(cid in reason for cid in ("camp-a", "camp-b", "camp-c"))
    assert reason.count("vast wait") == 3, "each needs its own runnable command"


@pytest.mark.parametrize("command", [
    "vast exec wait camp-a --interval 10",
    "vast wait camp-a --interval 10",           # after waiting leaves the exec group
    "/home/u/.venv/bin/vast exec wait camp-a",  # an explicit path still counts
])
def test_a_backgrounded_waiter_stands_the_hook_down(hook, capsys, command):
    """Nagging an agent that chose the better mechanism teaches the wrong one."""
    _start(hook, "camp-a")
    hook.delegated({"session_id": "s1", "tool_input": {"command": command}},
                   _ledger(hook))
    assert _check(hook, capsys) is None


def test_an_unrelated_bash_command_does_not_stand_it_down(hook, capsys):
    _start(hook, "camp-a")
    hook.delegated({"session_id": "s1", "tool_input": {"command": "ls -la"}},
                   _ledger(hook))
    assert _check(hook, capsys) is not None


def test_stopping_a_campaign_settles_it(hook, capsys):
    """Abandoning one deliberately is a decision; what the hook objects to is silence."""
    _start(hook, "camp-a")
    hook.clear({"session_id": "s1",
                "tool_response": {"campaign_id": "camp-a", "ok": True}}, _ledger(hook))
    assert _check(hook, capsys) is None


def test_a_refused_launch_records_nothing(hook, capsys):
    """No service, bad arguments: nothing started, so there is nothing to wait for."""
    hook.record({"session_id": "s1", "tool_response": {"error": "no service"}},
                _ledger(hook))
    assert _check(hook, capsys) is None


def test_a_stale_entry_stops_blocking(hook, capsys):
    """A crashed session must not greet the next one with a campaign that ended hours
    ago. A stale entry costs one backgrounded waiter, which exits immediately."""
    _start(hook, "camp-a")
    path = _ledger(hook)
    aged = json.loads(path.read_text())
    aged["camp-a"]["started_at"] = time.time() - hook.STALE_AFTER_S - 1
    path.write_text(json.dumps(aged))

    assert _check(hook, capsys) is None


def test_an_entry_just_inside_the_window_still_blocks(hook, capsys):
    """The boundary in the other direction, so the ageing cannot silently swallow live
    campaigns if STALE_AFTER_S is ever shortened."""
    _start(hook, "camp-a")
    path = _ledger(hook)
    fresh = json.loads(path.read_text())
    fresh["camp-a"]["started_at"] = time.time() - hook.STALE_AFTER_S + 60
    path.write_text(json.dumps(fresh))

    assert _check(hook, capsys) is not None


def test_sessions_do_not_block_each_other(hook, capsys):
    """Agents working in one tree in parallel each get their own ledger."""
    _start(hook, "camp-a", session="s1")
    assert _check(hook, capsys, session="s2") is None
    assert _check(hook, capsys, session="s1") is not None


def _rearm(hook, campaign, stalled, session="s1"):
    hook.rearm({"session_id": session,
                "tool_response": {"campaign_id": campaign, "stalled": stalled}},
               _ledger(hook, session))


def test_a_stall_re_arms_a_campaign_the_waiter_handed_off(hook, capsys):
    """`vast wait` exits 4 on a stall, which leaves the campaign alive and still marked
    handed-off. Without re-arming, the guard is spent and the agent can stop silently on a
    wedged campaign — the exact failure this hook exists to prevent."""
    _start(hook, "camp-a")
    hook.delegated({"session_id": "s1",
                    "tool_input": {"command": "vast wait camp-a"}}, _ledger(hook))
    assert _check(hook, capsys) is None          # handed off: nothing to say
    _rearm(hook, "camp-a", True)
    decision = _check(hook, capsys)
    assert decision is not None and "camp-a" in decision["reason"]


def test_a_healthy_status_read_does_not_re_arm(hook, capsys):
    """The narrowness is the property worth pinning: re-arming on any status read would nag
    about healthy sweeps a waiter is legitimately watching, and a guard that fires when
    nothing is wrong is one agents learn to ignore."""
    _start(hook, "camp-a")
    hook.delegated({"session_id": "s1",
                    "tool_input": {"command": "vast wait camp-a"}}, _ledger(hook))
    assert _check(hook, capsys) is None
    for verdict in (False, None):                # inside budget / no verdict possible
        _rearm(hook, "camp-a", verdict)
        assert _check(hook, capsys) is None


def test_re_arming_an_unknown_campaign_is_harmless(hook, capsys):
    """A status read for a campaign this session never started must not invent an entry."""
    _rearm(hook, "camp-elsewhere", True)
    assert _check(hook, capsys) is None


def _rearm_response(hook, campaign, response, session="s1"):
    hook.rearm({"session_id": session, "tool_input": {"campaign_id": campaign},
                "tool_response": {"campaign_id": campaign, **response}},
               _ledger(hook, session))


def _handed_off(hook, capsys, campaign="camp-a"):
    _start(hook, campaign)
    hook.delegated({"session_id": "s1",
                    "tool_input": {"command": f"vast wait {campaign}"}}, _ledger(hook))
    assert _check(hook, capsys) is None
    return campaign


def test_an_error_finding_re_arms_too(hook, capsys):
    """`vast wait` exits 5 on one, leaving the campaign live and still marked handed-off — the
    same hole a stall opens, so it needs the same patch. Claiming both and covering one would
    make the guard's own docstring wrong."""
    campaign = _handed_off(hook, capsys)
    _rearm_response(hook, campaign,
                    {"health_findings": [{"level": "error", "check": "sim-time-rate"}]})
    decision = _check(hook, capsys)
    assert decision is not None and campaign in decision["reason"]


def test_a_warning_does_not_re_arm(hook, capsys):
    """A warning never ends a wait, so it must not re-arm the guard either: a robot standing
    still is often correct, and a guard that fires on it is one agents learn to ignore."""
    campaign = _handed_off(hook, capsys)
    _rearm_response(hook, campaign,
                    {"health_findings": [{"level": "warn", "check": "robot-motion"}]})
    assert _check(hook, capsys) is None


def test_a_job_state_read_re_arms_on_its_simulator_s_findings(hook, capsys):
    """`get_job_state` passes the simulator's whole document through under `simulator`, warnings
    included — so the level has to be read there rather than assumed from the field's presence."""
    campaign = _handed_off(hook, capsys)
    _rearm_response(hook, campaign, {"simulator": {"findings": [
        {"level": "warn", "check": "robot-motion"}]}})
    assert _check(hook, capsys) is None, "a warning in the document is still only a warning"
    _rearm_response(hook, campaign, {"simulator": {"findings": [
        {"level": "warn", "check": "robot-motion"},
        {"level": "error", "check": "sim-time-start"}]}})
    assert _check(hook, capsys) is not None


def test_a_reply_that_could_not_read_anything_does_not_re_arm(hook, capsys):
    """`unavailable` says a read failed, which is not the same as the run being wrong. Only the
    two conditions that actually end a wait may re-arm; this hook computes no verdict of its own."""
    campaign = _handed_off(hook, capsys)
    _rearm_response(hook, campaign, {"simulator": None, "unavailable": ["could not read"]})
    assert _check(hook, capsys) is None
