# AGENTS.md — working agreements for RoboVAST

Project **invariants** for any agent or contributor changing this repository: what a change
must hold, not how the system works. How it works is in `docs/`, linked from here and never
restated — a copy of a documented fact is a second source that will disagree.

## 1. A change must work across every surface and backend

One operation contract behind four clients and two execution lanes, so an operation is done
only when every client and both lanes have it.

- Thread it end to end and never implement it inside one client — `docs/developer_guide.rst`,
  "Add an interface operation".
- CLI, MCP and web UI stay behaviourally consistent: same inputs, same results, whichever
  client and lane the caller uses.
- On the cluster lane say which part of a campaign you need — `docs/architecture.rst`, "Fetch
  what the caller needs, not the campaign".
- Every request is authenticated and identity comes from the resolved `Principal` —
  `docs/http_api.rst`, "Who may call it".

## 2. Documentation is split by audience, and describes the is-state

`docs/` separates user-facing task pages from internal design pages (`developer_guide.rst`,
`architecture.rst`); when behaviour changes, update both and mix neither into the other.

- **Write what the system does now.** No "previously", no "**Removed.**" entry — delete it. A
  migration step the reader must still perform, and an error message about a setting they
  still have, are not history and stay.
- Same for comments, test docstrings, CLI help and shipped examples: a guard keeps its reason
  and loses the incident that produced it.
- **Numbers from one cluster are not documentation.** State the property that holds anywhere
  and the command that produces the reader's own figures.
- A PR or comment states the rule, not the run that revealed it. Keep a measurement only where
  it is load-bearing, and then as an order of magnitude.
- A rebase or extraction replays the prose of the commits it carries and nothing fails when
  that prose no longer holds — grep the result against these rules rather than trusting it.

## 3. Keep the code clean — no deprecated paths, no fallbacks

Change code in place and delete what it replaces: one correct path, no compatibility shim or
silent workaround. Handling genuinely absent data is fine; tolerating the old way is not.

## 4. Report only what the caller can rely on

An interface's job is to tell the truth about what happened; a wrong answer that looks right is
worse than an error, because nothing downstream can detect it.

- **Never ignore an argument** — fail instead. Argument-free policy may have a precedence; a
  supplied argument may not.
- **Never advertise a path, id or capability the caller cannot use** from where it is.
- **Distinguish absent data from a failed lookup**, and let a status say "unhealthy" rather
  than leaving failure discoverable only by grepping a log.
- **Long-running work returns a handle**, not a blocked call that times out as a false failure.
- **One source of truth per fact** — derive or reference it; a second copy will disagree.

## 5. Which distribution owns what

The distributions, their layering, the shared namespace and the entry-point boundary are in
`docs/architecture.rst` ("Four distributions, layered by audience", "Execution lanes are
resolved, not imported") and `docs/developer_guide.rst` ("Working across the distributions").
A change must hold four things:

- **The dependency direction**: `robovast` never depends on a lane, and a lane is installed by
  its own step rather than an extra.
- **A plugin imports without the thing it drives** — resolving entry points to list what is
  available must not need a Docker socket or a kubeconfig.
- **Missing means missing, not broken**: `vast` starts, each group lists only what is
  installed, and an absent lane names the lanes that exist.
- **A client install stays a working install** — including deferred imports of the core, which
  only fail at call time.

## 6. Comments in a `.vast` are short and rare

A `.vast` is configuration, not prose. Comment only what needs explaining — a value that would
otherwise read as arbitrary, or the constraint that fixes it.

- Prefer a short inline `#` comment on the line it explains; a standalone comment is **at most
  two lines**.
- No section banners, no restating the key, no recording what the value used to be (§2).

## 7. An addition to this file follows these rules

Add a rule here only when it constrains a change and no test can. Everything else is docs, or
a test.

- Keep it to a lead of at most three lines plus one level of bullets, and add no third level.
- A current fact about how RoboVAST works goes in `docs/` and is referenced from here, never
  written out twice.
- State the rule, not the incident, the campaign or the cluster that produced it (§2).
