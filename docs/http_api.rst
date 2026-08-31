.. _http-api:

========
HTTP API
========

Everything RoboVAST does remotely goes through one HTTP service
(:mod:`robovast.service.app`, FastAPI). The CLI, the web UI and the MCP server are all
clients of it: ``HTTPTransport`` (:mod:`robovast.service.http_client`) is a method-per-route
mirror of this table, and ``LocalTransport`` implements the same interface in process for a
local run. So the routes below are not a second API — they are the one interface
:class:`~robovast.service.interface.RobovastInterface` describes, over HTTP.

The service also serves its own OpenAPI at ``/docs`` (and ``/openapi.json``), which is
authoritative for request and response *schemas*, and mounts the MCP server itself at
``/mcp`` (see :ref:`mcp`) — all on the one port. This page covers what ``/docs`` cannot:
the conventions the route table assumes.

Who may call it
===============

**Every request needs the shared access token** — there is no unauthenticated mode.
A browser exchanges it for a session cookie at ``/login``; the CLI and MCP send
``Authorization: Bearer``. A local ``vast serve`` binds ``127.0.0.1`` and mints a token
if none is configured; a deployed one is published over an Ingress with TLS. See
:doc:`deployment` for the boundary and ``vast login``.

Two consequences show up in the table. ``GET /version`` redacts ``results_root`` and
``sources_root`` for any caller that is not on the same machine, because those are
filesystem paths only useful — and only safe — to one that is; a forwarded request
counts as remote, since behind a proxy the peer address is the proxy. And the file routes
serve real paths on the service host, which is the point of the address space below.

Addressing files
================

File routes are not one resource per scope; they are :ref:`one address space
<file-address-space>`, which that section documents in full. The parts that matter when
reading the route table:

* ``/results/<campaign_id>/<path>`` is campaign output, and is **read-only by
  registration** — no write route exists, so a ``PUT`` or ``DELETE`` there is a
  router-level ``405``, not a permission check that could be got wrong.
* ``/sources/<workspace_id>/<path>`` is editable project input: ``GET``, ``PUT``,
  ``POST`` (substring edit) and ``DELETE``.
* A trailing ``/`` means "the directory", and listings page server-side.

Large uploads take the side channel instead: ``POST /uploads`` grants a token, and
``PUT /uploads/{token}`` streams the bytes.

A **campaign archive** has its own channel rather than an address in that space, because
``/sources`` needs workspaces configured (a ``501`` otherwise) and an archive is not project
input, while ``/results`` is read-only by registration and punching a write into it would cost
exactly the property that section describes. So: ``POST /campaigns/archives`` grants a token,
``PUT /campaigns/archives/{token}`` streams the bytes — **streamed to disk, not buffered**,
unlike the ``/uploads`` PUT whose payload is a ``.vast`` — and it answers with where they
landed. It stops there. ``POST /campaigns/import`` is the import, for that upload and for a
path put on the host by any other means, so the operation has one implementation rather than
one per entry point; an archive the *service* staged is removed once imported, a path the
caller named is not.

An upload that was never imported is **not** removed when the import refuses it: the answer to
the commonest refusal — a campaign of that id is already here — is to import the same staged
archive again with ``force``, and cleaning up on refusal would turn that retry into a second
multi-gigabyte upload. They are swept by age instead, on the next grant. Age rather than
liveness because the grant is consumed when the PUT begins, so an unreferenced staging file
cannot be told apart from one still arriving.

Status codes
============

Handlers delegate to the interface and map its exceptions in one place (``_guard``), so
the meaning of a status is uniform across every route:

.. list-table::
   :header-rows: 1
   :widths: 12 88

   * - Code
     - Meaning
   * - ``400``
     - ``ValueError`` — a malformed or rejected argument (an unknown lane, a path
       escaping its namespace, a non-``SELECT`` query).
   * - ``404``
     - ``KeyError`` — no such campaign, workspace, build or file.
   * - ``409``
     - ``RuntimeError`` — the request conflicts with current state (stopping a campaign
       that is not running; importing over a campaign that is already here, or one that
       is busy with another operation).
   * - ``422``
     - A notebook or visualization failed to render.
   * - ``501``
     - Workspaces are not configured on this service.

Streaming
=========

Four routes stream instead of returning a body. The two ``.../stream`` log routes and
``GET /campaigns/events`` are **server-sent events**; they are resumable, so a client that
drops sends ``Last-Event-ID`` and continues from the line after the one it last saw rather
than replaying the whole log. ``GET /campaigns/{id}/archive`` streams a tar.gz of the
campaign — tarred from the object store's objects as they are fetched on a cluster
service, from the campaign directory on a local one. Both lanes answer it: refusing on a
local service with a ``409`` ("the results are already on this host's filesystem") asserts
something true of a caller on that host and false of everyone else.

Every tick of an SSE stream that had nothing to report sends a ``heartbeat`` event. It is a
named event rather than the SSE comment such keepalives usually are, because a comment is
invisible to ``EventSource`` — it holds proxies open and tells the client nothing. Without
a frame the client can see, a stream that is merely quiet is indistinguishable from one
whose socket died in a suspended laptop or a torn-down ``kubectl port-forward``: no error is
raised, ``readyState`` stays ``OPEN``, and no further byte ever arrives. A client should
therefore treat a gap of several heartbeats as a dead connection and open a new
``EventSource``; the web UI does exactly that (see :doc:`web_ui`).

What may ride on a polled payload
=================================

``GET /campaigns/{id}/status`` and ``GET /campaigns/events`` are **hot fan-out payloads**, and
that governs what may be put on them. The web UI renders every campaign in the list as a card;
each card polls the status every 1.5 seconds, and the list stream re-lists every campaign once a
second for as long as any tab is open. So the cost of a field there is multiplied by campaigns on
screen, by polls, and by open tabs — and served over HTTP/2, where no connection limit throttles a
page-load burst the way it once did.

Four tiers, and the question to ask of any new data is which one it is in:

.. list-table::
   :header-rows: 1

   * - Kind
     - Example
     - Transport
   * - An origin, for anything time-dependent
     - ``phase_since``, ``batch_since``, ``search_since``
     - on the polled ``Status``, written **once**
   * - Bounded state, and cursors
     - phase, run counters, ``batches_done``, ``best_objective``
     - on the polled ``Status``, written when it changes
   * - A series, read by whoever is looking at it
     - a search's per-batch objective trajectory
     - its own route, fetched lazily, keyed on a cursor
   * - High-rate telemetry from a running run
     - a future live run view
     - its own stream, per run

The **series** row is the one that gets this wrong. ``Status`` carried a ``batch_history`` — one entry
per batch, growing for the whole run — that **nothing ever read**, on the payload polled most
often in the system. It was replaced by ``GET /campaigns/{id}/search/history``, which is requested
only while something is displaying it and re-requested only when ``batches_done`` (a single integer
on the status) moves. A series is almost never so small that it belongs on the status; if it grows
with batches, runs, or time, it does not.

The **origin** row is the cheapest tier and the one most often missed. A value that is a pure
function of wall-clock plus one stored origin is transported as the *origin*, never as the value:
the reader already has a clock. A ``time`` budget's elapsed seconds is the case that established
this. Its ``current`` comes from ``stop.progress()``, which the controller calls once per batch, so
on the wire it steps per round rather than ticking — and the obvious fix, having the progress poller
rewrite it every few seconds, is wrong twice over. It pays for the value on every poll forever, and
it breaks stall detection: ``ControllerState._progress_signal`` includes each budget row's
``current``, so a row rewritten from wall-clock advances the progress signal continuously and no
time-budgeted search can ever be reported stalled again. That is the same trap the signal already
avoids by not being ``updated_at``. What ships instead is ``search_since``, published once, with
every reader deriving elapsed through ``budget_positions`` (and its TS mirror ``budgetPosition``).

Hence the invariant behind it, which is not about transport at all: **a derived value never enters
the progress signal.** That tuple may contain only facts whose change *is* evidence the campaign
advanced. Wall clock advancing is not one.

Two worked examples of the tiers, for the search criteria specifically. A criterion's comparison
sense is **tier one** — static config, written once, never changing — and is on the status as
``BudgetItem.op`` for exactly that reason: without it no reader can render a ``stopping`` row
correctly, and a bare ``current / limit`` pair silently asserts a ``>=`` the criterion may not use.

A strategy's ``report().extra`` is **tier two's opposite**: an open dict of unbounded size (it carries ``elites``, ``measure_names``, ``best_elite``), so putting it on the
status to surface a QD ``coverage`` figure would recreate ``batch_history`` exactly. It belongs on a
route keyed on ``batches_done``, like the trajectory above.

The **telemetry** row is deliberately a *separate* stream rather than another event type on
``/campaigns/events``. A run's telemetry and a campaign list have different lifetimes
(per-run-while-viewing versus always-on), different rates, and different failure semantics;
multiplexed together, one slow consumer stalls the other and a run view's reconnects disturb the
campaign list.

Paths are defined once
======================

:class:`robovast.service.interface.Routes` holds the canonical path strings and the
builders for parameterized ones. Both the app and ``HTTPTransport`` use it so the two
bindings cannot drift — a route renamed in one place is renamed for the client too.

The table below is **generated from the running application**, not maintained by hand: it
is what the service registers, including routes added by installed endpoint plugins. A
hand-written endpoint list is exactly how the retired synthetic run-file route came to look
documented while matching no directory on disk.

Routes
======

.. http-routes::
