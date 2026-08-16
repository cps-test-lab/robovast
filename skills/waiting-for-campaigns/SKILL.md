---
name: waiting-for-campaigns
description: Use when a RoboVAST campaign has been started and the turn is about to end — how to wait for it without holding the conversation open, and what to say if you are not going to wait.
---

# Waiting for a campaign

`start_campaign` returns as soon as the campaign is *named*. The campaign then runs for
minutes, hours or days. Reading one status and reporting the result is the mistake this
skill exists to prevent: it tells the user a campaign finished when it has barely begun.

## Wait for it, in the background

```
Bash(command="vast exec wait <campaign_id> --interval 10", run_in_background=true)
```

Run it as the **whole** command — nothing chained after it. The exit code is the entire
signal, and `; echo …` makes the harness observe the wrapper's status instead: a failed
campaign then reports success.

| exit | means |
|---|---|
| 0 | finished, past postprocessing |
| 1 | failed or stopped |
| 2 | `--timeout` elapsed; the campaign is still running |
| 3 | the service has no phase for that id — a typo, or it died before recording one |

Backgrounded, it costs you nothing: you stay free, and you are notified when it exits.

## Or say you are not waiting

A sweep can legitimately run for days, and no in-session wait survives a closed session.
Not waiting is fine — *silently* not waiting is not. Say which campaign you are leaving,
and that ntfy announces the end.

## Or abandon it

`stop_campaign(campaign_id=…)` if it should not continue. That settles it too.

## What the hook does

The first attempt to end a turn on a campaign nobody is waiting for is blocked, once, and
then allowed. It is a floor under the three choices above, not a fourth: it never waits
for you, and it never blocks a second time for the same campaign.
