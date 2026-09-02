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

"""Searching what runs *said*, across runs and across campaigns.

One tool, for the one question no existing surface can answer. ``get_campaign_log`` and
``get_job_log`` read a single stream and are the right tools for a live or a just-failed run;
neither can answer "which of these forty runs logged this, and did they fail?", because that
is a join between a log and a run's verdict and a stream has nothing to join to.

The merged ``run_log`` table can (see
:mod:`robovast.results_processing.run_log`), so this tool is a thin shape over SQL that keeps
the *reading* vocabulary identical to the other log tools — ``grep`` / ``min_severity`` /
``summarize`` / ``tail`` mean exactly what they mean there, because they are the same
:func:`~robovast.mcp_server.log_view.view_log`.

Cost: the rows live in the central index, so nothing is fetched per campaign any more — a
campaign is a ``WHERE campaign_id = …`` predicate. What a wide search still costs is the scan:
``grep`` is a regular expression evaluated over *every* log line of *every* campaign it spans,
and a campaign's merged log runs to millions of rows. That is what ``max_campaigns`` bounds, and
why it still defaults low; campaigns are searched newest-first, and every response says which
campaigns it searched and which it skipped — a partial answer that looked complete would be the
worst outcome here.
"""

import logging
import re

from robovast.common import log_summary
from robovast.mcp_server import data_access, log_view, service_access

logger = logging.getLogger(__name__)

#: Default number of campaigns a regex may fan out to. Low on purpose: each campaign added to a
#: search is a full regex scan of its merged log, and a caller who wants forty of them should
#: say so.
_DEFAULT_MAX_CAMPAIGNS = 5

#: How many campaigns to look at when resolving a regex. The list is cheap (it is served from
#: the service's cached index); the *scans* are not, which is what ``max_campaigns`` bounds.
_CAMPAIGN_SCAN = 200

#: Rows a summary reads per campaign, regardless of ``limit``. A summary's whole value is the
#: *counts*, so scanning only as far as the returned-row limit reports "no errors" for a run
#: whose errors came after the two-hundredth line. 5000 is the service's own per-query clamp;
#: a campaign that reaches it is named in ``truncated``/``note``, since its counts then cover
#: a prefix of its matches.
_SUMMARY_SCAN = 5000


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _resolve_campaigns(campaign_id: str, campaign_regex: bool,
                       max_campaigns: int) -> tuple[list, str]:
    """The campaigns to search, in the service's order (live first, then newest first),
    and a note about what was left out."""
    explicit = [campaign_id]
    if not campaign_regex:
        return explicit[:max_campaigns], (
            f"; {len(explicit) - max_campaigns} campaign(s) beyond max_campaigns not searched"
            if len(explicit) > max_campaigns else "")
    try:
        pattern = re.compile(campaign_id, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"campaign_id={campaign_id!r} is not a valid regex: {e}") from e

    from robovast.service.interface import \
        ListCampaignsRequest  # pylint: disable=import-outside-toplevel
    client = service_access.service_client()
    if client is None:
        from robovast.service.local_transport import \
            LocalTransport  # pylint: disable=import-outside-toplevel
        client = LocalTransport()
    page = client.list_campaigns(ListCampaignsRequest(limit=_CAMPAIGN_SCAN, offset=0))
    matched = [c.campaign_id for c in page.campaigns if pattern.search(c.campaign_id)]
    note = ""
    if len(matched) > max_campaigns:
        note = (f"; {len(matched) - max_campaigns} of {len(matched)} matching campaign(s) not "
                f"searched (max_campaigns={max_campaigns}) -- raise it or narrow the regex")
    return matched[:max_campaigns], note


def _shutdown_term() -> str:
    """"This line came before its run's scenario reached a verdict", as SQL.

    The verdict is read from where postprocessing recorded it rather than matched again
    here: ``scenario_timestamps`` is keyed on ``(config_name, run_id)``, so this is a
    primary-key lookup per row, and "when did the trial end" keeps one answer across the
    SQL, the web UI and the stream tools.

    On ``wall_ts`` and not ``sim_time``, because the clock map does not extrapolate: a
    run whose ``/clock`` stopped at shutdown has NULL ``sim_time`` on every line after
    the verdict, so a sim-time comparison would keep exactly the lines this drops. A run
    with no recorded verdict trims nothing — trimming to an invented moment is worse.
    """
    # Correlated on campaign_id as well as the run: one index holds every campaign, so
    # without it another campaign's verdict would trim this campaign's lines.
    return ("NOT EXISTS (SELECT 1 FROM scenario_timestamps s "
            "WHERE s.campaign_id = l.campaign_id "
            "AND s.config_name = l.config_name AND s.run_id = l.run_id "
            "AND s.wall_ts IS NOT NULL AND l.wall_ts IS NOT NULL "
            "AND l.wall_ts > s.wall_ts)")


def _glob_to_regex(pattern: str) -> str:
    """One glob, as a POSIX regex, anchored at both ends.

    Postgres runs this pattern, so it may use only what Postgres parses. That rules out
    ``fnmatch.translate``, whose output carries a scoped inline flag group (``(?s:...)``) and,
    on newer Pythons, atomic groups: Postgres rejects them, and a rejected pattern is a query
    that errors rather than one that returns the wrong rows.

    Anchored at BOTH ends because REGEXP searches rather than matches: unanchored, ``*-1`` also
    selects ``config-11`` and reports another configuration's runs as this one's.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "[":
            close = pattern.find("]", i + 1)
            if close < 0:                      # an unclosed '[' is a literal, as in fnmatch
                out.append(re.escape(ch))
            else:
                body = pattern[i + 1:close]
                if body.startswith("!"):       # fnmatch spells negation '!', regex spells it '^'
                    body = "^" + body[1:]
                out.append("[" + body + "]")
                i = close
        else:
            out.append(re.escape(ch))
        i += 1
    return r"\A" + "".join(out) + r"\Z"


def _campaign_term(campaign_id: str) -> str:
    """Scope to one campaign. The index holds every campaign's rows in one table, so an
    unscoped query does not read one campaign's log -- it reads the corpus."""
    return f"l.campaign_id = {_quote(campaign_id)}"


def _predicates(*, grep: str, min_severity: str, config_filter: str, run_id, container: str,
                node: str, source: str, t0, t1, in_window) -> list:
    """The WHERE terms, as SQL. ``grep`` uses ``REGEXP``, which every data query registers."""
    terms = []
    if grep:
        # Pre-checked so an obviously malformed pattern is a message about the pattern rather
        # than a failed query. It is a courtesy, not a guarantee: REGEXP() is `value ~ pattern`
        # in Postgres, whose dialect is not Python's, so a pattern accepted here can still be
        # rejected there.
        try:
            re.compile(grep)
        except re.error as e:
            raise ValueError(f"grep={grep!r} is not a valid regular expression: {e}") from e
        terms.append(f"REGEXP({_quote(grep)}, l.message)")
    if min_severity:
        floor = log_summary.severity_rank(min_severity)
        keep = [s for s in log_summary.SEVERITIES
                if log_summary.severity_rank(s) >= floor]
        terms.append(f"l.severity IN ({', '.join(_quote(s) for s in keep)})")
    if config_filter:
        # The same glob vocabulary the campaign tools use, carried by REGEXP: Postgres has no
        # GLOB operator, so a glob has to reach it as a regex.
        terms.append(f"REGEXP({_quote(_glob_to_regex(config_filter))}, l.config_name)")
    if run_id is not None:
        terms.append(f"l.run_id = {int(run_id)}")
    if container:
        terms.append(f"l.container = {_quote(container)}")
    if node:
        terms.append(f"l.node = {_quote(node)}")
    if source:
        terms.append(f"l.source = {_quote(source)}")
    if t0 is not None:
        terms.append(f"l.sim_time >= {float(t0)}")
    if t1 is not None:
        terms.append(f"l.sim_time <= {float(t1)}")
    if in_window is not None:
        terms.append(f"l.in_window = {1 if in_window else 0}")
    return terms


def _rollup_sql(terms: list, limit: int) -> str:
    """Hits per run for one campaign, joined to the run's verdict.

    The join is the whole point: it turns "this warning appears" into "this warning appears in
    the four runs that failed and none that passed". ``LEFT JOIN`` so a run missing from
    ``runs`` still reports its hits rather than disappearing.
    """
    where = f" WHERE {' AND '.join(terms)}" if terms else ""
    # Ranked, not `max(severity)`: severity is text, and alphabetically 'error' < 'other' <
    # 'warn', so a plain max over a run holding both an error and an info line reports "warn".
    # The rank mirrors log_summary.SEVERITIES and is mapped back in Python.
    return (
        f"SELECT l.config_name, l.run_id, r.passed, r.status, r.clock_map_source, "
        f"count(*) AS hits, min(l.sim_time) AS first_sim_time, min(l.wall_ts) AS first_wall_ts, "
        f"max(CASE l.severity WHEN 'error' THEN 2 WHEN 'warn' THEN 1 ELSE 0 END) "
        f"AS worst_severity_rank, min(l.message) AS example "
        f"FROM run_log l LEFT JOIN runs r "
        f"ON r.campaign_id = l.campaign_id "
        f"AND r.config_name = l.config_name AND r.run_id = l.run_id"
        # The run's own columns are grouped, not aggregated: Postgres requires every selected
        # column to be one or the other, and they are constant per (config_name, run_id) anyway.
        f"{where} GROUP BY l.config_name, l.run_id, r.passed, r.status, r.clock_map_source "
        f"ORDER BY hits DESC, l.config_name, l.run_id LIMIT {int(limit)}"
    )


def _count_sql(terms: list) -> str:
    """How many rows match, independent of how many are returned.

    Its own query because the row cap applies to the *result*: deriving the total from a capped
    page reports "2 matching lines" for a filter that matched forty-four, and the total is
    exactly what a caller uses to decide whether to page.
    """
    where = f" WHERE {' AND '.join(terms)}" if terms else ""
    return f"SELECT count(*) AS n FROM run_log l{where}"


def _lines_sql(terms: list, limit: int, offset: int) -> str:
    where = f" WHERE {' AND '.join(terms)}" if terms else ""
    return (
        f"SELECT l.config_name, l.run_id, l.sim_time, l.wall_ts, l.time_source, l.in_window, "
        f"l.container, l.node, l.source, l.level, l.severity, l.message "
        f"FROM run_log l{where} "
        f"ORDER BY l.config_name, l.run_id, l.sim_time IS NOT NULL, l.sim_time, l.wall_ts "
        f"LIMIT {int(limit)} OFFSET {int(offset)}"
    )


#: What ``severity`` a rank means, mirroring :data:`log_summary.SEVERITIES`.
_SEVERITY_BY_RANK = {0: "other", 1: "warn", 2: "error"}

#: A row's level as a log line's marker. A row with no level of its own (plain stdout) gets one
#: from the severity the merge computed, so the reconstructed line carries the verdict that was
#: already reached rather than inviting the keyword scan to reach a different one.
_LEVEL_FOR_SEVERITY = {"error": "ERROR", "warn": "WARN", "other": "INFO"}


def _as_line(row: dict) -> str:
    """One row rendered the way the other log tools' text is, so ``view_log`` can filter and
    summarize it with the same grammar rather than a second one written for this table.

    The stamp must be a *number*: the grammar is ``[LEVEL] [<seconds>] [node]:``, and a
    placeholder like ``[-]`` makes the whole prefix unrecognizable -- which silently drops the
    level too, so a run's errors summarize as ``error: 0``. A row with no sim time falls back to
    its wall stamp, and failing that to 0, because *when* is not what this rendering is for.
    """
    stamp = row.get("sim_time")
    if stamp in (None, ""):
        stamp = row.get("wall_ts")
    try:
        when = f"{float(stamp):.6f}"
    except (TypeError, ValueError):
        when = "0.0"
    severity = str(row.get("severity") or "other")
    level = row.get("level") or _LEVEL_FOR_SEVERITY.get(severity, "INFO")
    node = row.get("node") or row.get("container") or "?"
    # First line only, and the rest reported as a count. One row must render as exactly one
    # line: a folded traceback is a single event, and letting its frames through would make each
    # of them a "pattern" of its own and inflate every line total that quotes this text.
    message = str(row.get("message", ""))
    head, _, rest = message.partition("\n")
    if rest:
        head = f"{head} (+{rest.count(chr(10)) + 1} more line(s))"
    return f"[{level}] [{when}] [{node}]: {head}"


async def search_run_logs(
    campaign_id: str,
    grep: str = "",
    min_severity: str = "",
    campaign_regex: bool = False,
    max_campaigns: int = _DEFAULT_MAX_CAMPAIGNS,
    config_filter: str = "",
    run_id: int | None = None,
    container: str = "",
    node: str = "",
    t0: float | None = None,
    t1: float | None = None,
    group_by_run: bool = True,
    summarize: bool = False,
    limit: int = 200,
    hide_shutdown: bool = True,
) -> dict:
    """Which runs logged this? Searches the merged per-run log, across runs and campaigns.

    The log is every container's output joined with ``/rosout``, on the run's playback clock, so
    ``t0``/``t1`` are sim-time seconds of the *trial*. For one live or just-finished run use
    ``get_job_log`` (this needs postprocessing); for build/controller phases,
    ``get_campaign_log``. ``grep`` (regex) / ``min_severity`` / ``summarize`` / ``tail`` /
    ``hide_shutdown`` mean the same in all of them, defaults included.

    Args:
        campaign_id: Campaign id or path; a regex over ids when *campaign_regex*.
        max_campaigns: Cap — every campaign added to the search is a full regex scan of its
            merged log; what was left out is named in ``note``.
        config_filter: Glob over configuration names, e.g. ``goal-*``.
        run_id: Restarts at 0 per configuration, so pair it with *config_filter*.
        container: ``main``, ``simulation``, ``sut``, …
        group_by_run: Per-run rollup (default); ``False`` returns the lines.
        summarize: Patterns and counts instead — "what flooded this sweep".

    Returns:
        ``{campaigns, campaigns_skipped}`` plus one of: ``runs`` (per-run ``hits``,
        ``first_sim_time``, ``worst_severity``, ``example`` joined to ``passed``/``status``, and
        ``clock_map_source`` — ``none`` means that run has no ``sim_time`` at all); ``lines`` with
        ``lines_total``/``returned``/``truncated``; or ``patterns`` with ``severity_counts``,
        ``matched_lines`` (rows summarized) and ``lines_total`` (rows that matched at all),
        ``truncated`` naming in ``note`` any campaign whose counts cover only its first
        rows.

    Examples::

        search_run_logs("campaign-x", grep="CRITICAL FAILURE")
        search_run_logs("^basic-nav-", campaign_regex=True, min_severity="error")
    """
    # `source`, `in_window`, `offset` and `top` are deliberately not parameters: the MCP tool
    # surface is a shared token budget, and each argument costs schema whether or not it is used.
    # They cost more than they earn here -- `query_campaign_data_sql` reaches the same columns
    # directly for the rare question that needs them.
    offset, top = 0, log_summary.DEFAULT_TOP
    try:
        campaigns, skip_note = _resolve_campaigns(
            campaign_id, campaign_regex, max(1, int(max_campaigns)))
        terms = _predicates(grep=grep, min_severity=min_severity, config_filter=config_filter,
                            run_id=run_id, container=container, node=node, source="",
                            t0=t0, t1=t1, in_window=None)
    except ValueError as e:
        return {"error": str(e)}
    if not campaigns:
        return {"error": f"no campaign matches {campaign_id!r}"}

    searched: list = []
    skipped: list = []
    rollup: list = []
    lines: list = []
    lines_total = 0
    #: Campaigns whose summary scan reached its cap, so their counts describe a prefix of
    #: the matches rather than all of them. Per campaign because the cap is per campaign:
    #: a sum over campaigns crosses it while every one of them was read whole.
    scan_capped: list = []

    # One query per campaign, not one spanning all of them: the caps below (the summary scan,
    # the returned-row limit) are per campaign, and a single UNION would spend one campaign's
    # budget on another's rows.
    for cid in campaigns:
        # A summary reads far more rows than it returns, because it returns counts.
        scan = _SUMMARY_SCAN if summarize else limit + offset
        scoped = [_campaign_term(cid)] + terms + (
            [_shutdown_term()] if hide_shutdown else [])
        sql = (_rollup_sql(scoped, limit) if group_by_run and not summarize
               else _lines_sql(scoped, scan, 0))
        result = data_access.query(cid, sql, max(1, scan))
        if "error" in result:
            # A campaign with no run_log (postprocessed before the merge existed, or still
            # running) is skipped by name, never counted as "nothing matched".
            skipped.append({"campaign_id": cid, "reason": result["error"][:200]})
            continue
        rows = result.get("rows") or []
        searched.append({"campaign_id": cid, "fetch": result.get("fetch"),
                         "rows": len(rows)})
        for row in rows:
            row["campaign_id"] = cid
        if group_by_run and not summarize:
            rollup.extend(rows)
        else:
            lines.extend(rows)
            if summarize and len(rows) >= scan:
                scan_capped.append(cid)
            counted = data_access.query(cid, _count_sql(scoped), 1)
            total_rows = (counted.get("rows") or [{}])[0].get("n")
            lines_total += int(total_rows) if total_rows is not None else len(rows)

    out: dict = {"campaigns": searched, "campaigns_skipped": skipped}
    notes = [skip_note.lstrip("; ")] if skip_note else []
    if hide_shutdown:
        # Said on every call, not only when it excluded something: unlike the stream
        # tools this cannot count what it dropped (the rows never leave the database), so a
        # silent term would make a trimmed search read as a complete one.
        notes.append("only lines before each run's scenario verdict were searched "
                     "(the shutdown phase is excluded); pass hide_shutdown=false to "
                     "include it")
    if notes:
        out["note"] = "; ".join(notes)

    if summarize or not group_by_run:
        # Rendered back to text so `view_log` applies the *same* tail/summarize/severity rules
        # the other log tools do -- one grammar, not a second one for this table.
        text = "\n".join(_as_line(row) for row in lines)
        view = log_view.view_log(text, summarize=summarize, top=top,
                                tail=0 if summarize else limit)
        if summarize:
            # The counts and the patterns only. `view_log`'s line accounting describes the
            # text it was handed, and here the filtering happened in SQL: its `dropped` and
            # `shutdown_dropped` are structurally zero, and a reported `shutdown_dropped: 0`
            # contradicts the note above, which says the shutdown phase was excluded.
            out.update({k: view[k] for k in
                        ("patterns", "patterns_total", "severity_counts")})
            out["matched_lines"] = view["lines"]
            out["lines_total"] = lines_total
            # Reaching the scan cap means the counts describe a prefix of that campaign's
            # matches, not all of them. Judged per campaign, against the rows that campaign
            # returned: measured against the total across campaigns, a search of five
            # campaigns read whole reported itself truncated as soon as their matches added
            # up past one campaign's cap.
            if scan_capped:
                out["truncated"] = True
                out["note"] = (f"{out.get('note', '')}; counts cover the first "
                               f"{_SUMMARY_SCAN} matching rows of "
                               f"{', '.join(scan_capped)}").lstrip("; ")
            return out
        out["lines"] = lines[offset:offset + limit]
        out["lines_total"] = lines_total
        out["returned"] = len(out["lines"])
        out["offset"] = offset
        out["truncated"] = lines_total > offset + limit
        return out

    for row in rollup:
        row["worst_severity"] = _SEVERITY_BY_RANK.get(
            int(row.pop("worst_severity_rank", 0) or 0), "other")
    rollup.sort(key=lambda r: (-(r.get("hits") or 0), str(r.get("campaign_id")),
                               str(r.get("config_name")), r.get("run_id") or 0))
    out["runs"] = rollup[:limit]
    out["runs_total"] = len(rollup)
    out["hits_total"] = sum(r.get("hits") or 0 for r in rollup)
    out["truncated"] = len(rollup) > limit
    return out


_TOOLS = [search_run_logs]


class RunLogsPlugin:
    """MCP plugin: searching what runs said, joined to how they ended."""

    name = "run_logs"

    def register(self, mcp) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
