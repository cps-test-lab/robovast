#!/usr/bin/env python3
"""Never end a turn silently in the middle of a campaign.

`start_campaign` returns as soon as the campaign is named and the campaign runs on for
minutes, hours or days. `start_campaign` now hands back the command that waits for it
(`vast wait <id>`, backgrounded) — but nothing *forces* it to be run, and the
reported bug was exactly an agent reading one status and stopping. This hook is the floor
under that: the first attempt to end a turn with a campaign nobody is waiting for is
blocked with the command to run.

**Block once, then allow.** A sweep can legitimately run for days, and no in-session wait
survives that (the service announces the end over ntfy instead). Blocking until done
would hold the user's session hostage; blocking once turns a silent exit into a stated
decision, which is the actual defect.

Pure bookkeeping: no robovast import, no service call, no second opinion about whether a
campaign is over. The MCP tools are the only authority on that and this only records what
they already reported. A stale entry therefore costs one backgrounded waiter, which exits
immediately on an already-finished campaign — it self-heals rather than wedging a session.

Wired in this plugin's ``hooks/hooks.json`` as:
  PostToolUse  mcp__.*__start_campaign  -> record
  PostToolUse  mcp__.*__stop_campaign   -> clear
  PostToolUse  Bash                     -> delegated  (a backgrounded waiter was launched)
  Stop, SubagentStop                    -> check

The tool matchers are patterns, not literals: the prefix is `mcp__<server-name>__`, and
the server name is whatever the user typed in `claude mcp add`. Hardcoding `robovast`
made the guard silently do nothing for anyone who chose another name -- installed,
inert, and indistinguishable from working.
"""

import json
import os
import sys
import time
from pathlib import Path

# A campaign nobody waited on stops being this hook's business eventually: a crashed or
# abandoned session must not greet the next one with a block about a campaign that ended
# hours ago.
STALE_AFTER_S = 6 * 3600


def _ledger_path(payload):
    """Per-session file. Sessions are the isolation boundary: agents working in this tree
    in parallel each get their own, so one agent's campaign never blocks another's turn.
    """
    root = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or "."
    session = str(payload.get("session_id") or "unknown").replace("/", "_")
    directory = Path(root) / ".claude" / ".campaign-watch"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session}.json"


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # An unreadable ledger means this hook has no opinion, not that the turn is
        # suspect. Never let bookkeeping damage break a session.
        return {}


def _write(path, data):
    """Atomic replace, because subagents share one session id.

    Several PostToolUse hooks can run concurrently under one session; a read-modify-write
    that is not atomic drops whichever campaign lost the race, and a dropped campaign is
    exactly the one nobody then waits for.
    """
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _live(data):
    """Entries still worth blocking on."""
    now = time.time()
    return {cid: e for cid, e in data.items()
            if now - float(e.get("started_at", 0)) < STALE_AFTER_S}


def _tool_response(payload):
    """The tool's result, whatever shape the harness passes it in."""
    response = payload.get("tool_response")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return {}
    return response if isinstance(response, dict) else {}


def record(payload, path):
    campaign_id = _tool_response(payload).get("campaign_id")
    if not campaign_id:
        return  # a refused launch (no service, bad args) started nothing
    data = _live(_read(path))
    data.setdefault(str(campaign_id), {"started_at": time.time(), "warned": False})
    _write(path, data)


def clear(payload, path):
    """Drop a campaign once a tool reports it is genuinely over.

    Abandoning a campaign deliberately (`stop_campaign`) counts as settling it: what this
    hook objects to is leaving one unattended in silence, not choosing to end it.
    """
    response = _tool_response(payload)
    campaign_id = str(response.get("campaign_id") or
                      (payload.get("tool_input") or {}).get("campaign_id") or "")
    if not campaign_id:
        return
    finished = bool(response.get("done")) or bool(response.get("ok"))
    if not finished:
        return
    data = _live(_read(path))
    if data.pop(campaign_id, None) is not None:
        _write(path, data)


def delegated(payload, path):
    """A backgrounded `vast wait` is waiting for it, so this hook need not nag.

    Blocking in an MCP call is not the only correct way to see a campaign out, and it is
    the worse one for a long sweep: it occupies the conversation for as long as it runs.
    Backgrounding the CLI waiter frees the agent and still gets it notified when the
    campaign lands. Left unrecognised, this hook stopped the turn precisely when the
    agent had chosen the better mechanism — nagging about the right answer teaches the
    wrong one.

    Whichever campaign ids the command mentions are marked handed-off at *launch*, not at
    exit: that is the moment responsibility moves to the waiter.
    """
    command = str((payload.get("tool_input") or {}).get("command") or "")
    # Matches `vast` + `wait`, not the full spelling. That is what let this survive the
    # move of waiting out of the execution group (`vast exec wait` -> `vast wait`)
    # without a change: requiring `exec` would have stopped it recognising a correct
    # waiter at the rename, and nagging an agent that chose the right mechanism teaches
    # the wrong one. Both spellings still match, which is right -- an older install has
    # the old one.
    if "vast" not in command or "wait" not in command:
        return
    data = _live(_read(path))
    handed = [cid for cid in data if cid in command]
    for cid in handed:
        data[cid]["warned"] = True  # a waiter owns it; do not stop the turn for it
    if handed:
        _write(path, data)


def check(_payload, path):
    """Block the first attempt to end a turn on an unwaited campaign."""
    data = _live(_read(path))
    pending = [cid for cid, e in data.items() if not e.get("warned")]
    if not pending:
        if data != _read(path):
            _write(path, data)  # drop stale entries we just aged out
        return
    for cid in pending:
        data[cid]["warned"] = True
    _write(path, data)
    # Every pending id, not just the first. Marking them all warned while naming one was
    # silent data loss: start three campaigns, get told about one, and the other two are
    # recorded as handled without anyone ever hearing of them.
    listed = ", ".join(pending)
    waits = "\n".join(f"    vast wait {cid} --interval 10" for cid in pending)
    plural = "campaigns were" if len(pending) > 1 else "campaign was"
    print(json.dumps({
        "decision": "block",
        "reason": (
            f"{len(pending)} {plural} started and never waited to completion: {listed}.\n"
            f"Run each of these in the BACKGROUND (Bash run_in_background=true) — each "
            f"exits when its campaign is genuinely over and you are notified then, so "
            f"you stay free meanwhile:\n{waits}\n"
            "If you are not going to wait, say so explicitly: name the campaign ids and "
            "say ntfy announces the end. If you are abandoning one, call "
            "stop_campaign(campaign_id=...). "
            "This is the only time you will be stopped for these campaigns."),
    }))


ACTIONS = {"record": record, "clear": clear, "delegated": delegated, "check": check}


def main():
    action = ACTIONS.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if action is None:
        return
    payload = {}
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return
    action(payload, _ledger_path(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001
        # A hook that raises must not be able to trap a session. Failing open costs the
        # backstop; failing closed costs the user their turn, every turn.
        pass
