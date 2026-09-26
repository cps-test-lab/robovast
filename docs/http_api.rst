.. _http-api:

========
HTTP API
========

Everything RoboVAST does remotely goes through one HTTP service
(:mod:`robovast.service.app`, FastAPI). The CLI, the web UI and the MCP server are all
clients of it: ``HTTPTransport`` (:mod:`robovast.service.http_client`) is a method-per-route
mirror of this table, and the service implements the same interface in process. So the
routes below are not a second API — they are the one interface
:class:`~robovast.service.interface.RobovastInterface` describes, over HTTP.

The service also serves its own OpenAPI at ``/docs`` (and ``/openapi.json``), which is
authoritative for request and response *schemas*, and mounts the MCP server itself at
``/mcp`` (see :ref:`mcp`) — all on the one port. This page covers what ``/docs`` cannot:
the conventions the route table assumes.

Who may call it
===============

**Every request needs the shared access token** — there is no unauthenticated mode.
A browser exchanges it for a session cookie at ``/login``; the CLI and MCP send
``Authorization: Bearer``. ``vast cluster setup`` mints the token and ``vast service
token`` prints it; a published service sits behind an Ingress with TLS, and a one-node
one is reached over a port-forward on ``127.0.0.1``. See :doc:`deployment` for the
boundary and ``vast login``.

The gate is ASGI middleware (:class:`robovast.service.auth.AuthMiddleware`), not a FastAPI
dependency, so a new route is covered automatically: a dependency would miss the mounted
``/mcp`` sub-app, which does not run the parent app's dependencies, and
``BaseHTTPMiddleware`` would buffer the streaming responses. It resolves a
:class:`~robovast.service.auth.Principal` rather than a boolean — read identity from that
rather than re-parsing headers, so exchanging the shared secret for an identity provider
replaces one resolver instead of every route.

A **scoped token** is the second kind of credential the gate accepts, and the only one a
pod is ever given. It is ``<scope>.<hmac>`` -- an HMAC of the scope under the shared secret
(:func:`robovast.service.auth.scoped_token`) -- and it reaches exactly the data-plane routes
of the one campaign or staged slot the scope names
(:func:`~robovast.service.auth.scope_allows`); anything else is a ``403``, distinct from the
``401`` an unauthenticated caller gets so the pod is not sent to log in with a token that
already proved itself. It needs no registry: every process holding the secret verifies it,
a restart forgets nothing, and a scope stops mattering the moment nothing answers for it.
Handing a pod the shared secret instead would let any container in the cluster start
campaigns.

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

The **data plane** is the third namespace, ``/data``: every route that moves a campaign's
or a staged slot's bytes as one tar stream. ``GET /data/campaigns/{id}/archive`` is the
whole campaign's records as a tar.gz -- postprocessed if they have been, raw if not --
and never its ``.cache/`` table cache; ``GET /data/campaigns/{id}/exports/{export_id}`` is
a finished export's tar.gz (:ref:`results-export`), a ``404`` until it is done and a ``409``
naming the reason once it failed; ``GET .../inputs`` is what a job pod extracts into its
``/config``, with the campaign's ``_config/`` and ``_transient/`` flattened, only the
named jobs' own documents (``job=<tag>``, required) taken from the per-job ones, and a
cell's own files (``config_file=<config>:<rel>``) landing on top; ``PUT .../outputs`` takes a
pod's output tree into the campaign, last writer wins, with what the driver owns -- the
campaign's own store, its logs -- refused per member and named in the reply; and
``GET``/``PUT /data/staged/{slot}`` move the scratch trees the service stages for a build
or exec pod. Every stream a pod reads is a plain tar, and an upload may be plain or
gzipped: the reader detects it. These are **control routes, not writes under** ``/results``, so that space
keeps having no write verb at all. Streamed both ways, never buffered: a download is
tarred as it is read and an upload is extracted as it arrives, through a bounded queue,
so a slow disk holds the socket back rather than the body piling up in memory. In the
cluster Deployment they are answered by their own process behind the front
(:doc:`deployment`); a ``vast serve`` started by hand mounts them into its one app, at the
same paths.

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
     - ``ValueError`` — a malformed or rejected argument (an unknown backend, a path
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
     - ``UnsupportedOperation`` — the operation exists and the implementation answering does
       not offer it. The ``detail`` is one sentence naming the operation and the
       implementation, so it cannot be mistaken for bad input, a conflict, or a bug. Also:
       workspaces are not configured on this service.
   * - ``503``
     - A dependency did not answer, so the request could not be attempted: the object
       store, or the exec path into a container. Worth retrying, unlike the
       codes above.
   * - ``507``
     - The service is out of disk space, or low enough that it declines new work (see
       :ref:`deployment-disk-reserve`). Never reported as bad input or a conflict: the
       request itself was fine, and is worth retrying once space is freed.

A call that acts on several things answers per thing, not with one status. ``POST
/campaigns/delete`` takes ``{"campaign_ids": [...]}`` and returns ``200`` with one result per
id — its ``outcome`` (``deleted``, ``not_found``, ``partial``, ``running``, ``invalid``) and its
own message — because a running campaign among the ids is a fact about that id, and a ``409`` for
the whole call would hide that the others were deleted. Only a request naming no campaign is
refused as a whole (``422``). The single-campaign ``DELETE /campaigns/{id}`` keeps the codes
above: ``400`` for an id that is not a campaign id, ``409`` for a running campaign.

A refusal whose *class* a caller must act on rather than print also carries an
``x-robovast-error`` header naming that class: ``exec_path_unavailable``, for a deployment
where no command can be run in a container at all, and ``unsupported_operation``, for an
operation this service does not offer (a client neither retries it nor blames its input). The
exception type is what an
HTTP boundary drops, and a client that has to *behave* differently (report the deployment
rather than the image, degrade a check to "unchecked") would otherwise have to match on the
sentence, which then nobody may reword. ``ServiceError.code`` carries it; the body stays
FastAPI's ``{"detail": ...}`` for every refusal, coded or not.

Streaming
=========

Several routes stream instead of returning a body. The three ``.../stream`` log routes (a
campaign's, a job's, and the service's own under ``/admin``) and ``GET /campaigns/events``
are **server-sent events**. The log streams are resumable, so a client that drops sends
``Last-Event-ID`` and continues from the line after the one it last saw rather than
replaying the whole log; the list stream sends the whole list again on reconnect, which is
the client's initial state anyway.

``GET /campaigns/{id}/job-tap?job_name=&selection=a,b&max_seconds=`` is server-sent events
with no pull form: a **tap** on a running job, the simulator's own following command
(:meth:`~robovast.common.simulators.SimulatorBackend.tap_command`) started in the job's
simulation container and its stdout relayed as ``line`` events (``{"t_wall", "line"}``) for
at most ``max_seconds``, capped at the service's bound of two minutes. ``eof`` carries
``{"exit_code", "timed_out"}`` -- ``124`` and true when the bound cut it, ``null`` when the
reader closed the stream first, which ends the tap. A refusal is ``streamerror`` then
``eof``: the job is not running, the simulator has no tap (named), or a tap is already open
on that job. It is not resumable -- a relay of the moment, not a record -- and it is
**recorded against the run as a probe** before it starts, exactly as ``POST .../job-exec``
is: a process the service started runs in the simulator's container while it lasts.

``GET /data/campaigns/{id}/live?run=<config>/<run>&tables=a,b`` is server-sent events too: a
run's tables as they are decoded while it records. A ``batch`` event carries ``{"table":
name, "rows": [...]}``, at most 2000 rows, so one decoded batch may be several events; a
table's batches add up to what a query of the finished run gives, and a table the recording
does not carry yet starts when a topic that gives it appears. ``eof`` follows the run's
verdict once its recordings are closed and read to their end; a run that is not live --
``runs.live`` is false: it has its ``test.xml``, or the campaign its terminal record -- gets
``eof`` at once, because its rows are all there for ``POST .../query``. ``streamerror`` then
``eof`` names a campaign or run that is not here, a run key or table list that is not one,
and a client that fell too far behind: batches keep coming at the recorder's pace, and a
reader that does not keep up is dropped rather than buffered without bound. It is not
resumable; a client that reconnects reads what landed so far from the SQL and follows from
there. On the data plane rather than the control plane because the watcher behind it must
run where the pods' deliveries land, which is where a recording can be followed as it is
appended to (:ref:`the data plane <data-plane>`); a pod's scoped token does not reach it.
With ``&frames=<topic>,...`` the same stream also carries a ``frame`` event per named image
topic -- ``{"topic", "t", "jpeg_base64"}``, the newest frame, at most every 250 ms and only
while it changes -- because a camera's messages never become rows. The frames themselves
are two plain routes beside it: ``GET /data/campaigns/{id}/frame?run=&topic=[&t=]`` answers
``image/jpeg`` with the last frame at or before ``t`` (the newest without it), no wider
than 640 px, its stamp in ``X-Frame-Time``; with ``&full=1`` it answers the whole frame
instead -- a raw image as its pixels in numpy's ``.npy`` format (``application/x-npy``, the
encoding in ``X-Frame-Encoding``), a compressed image as recorded -- which is what
``robovast-data`` reads. ``GET .../frame-index?run=&topic=`` lists every frame's stamp as
``{"topic", "times"}``. ``GET .../points?run=&topic=[&t=][&after=1]`` answers one point
cloud as an Arrow IPC stream, one column per field, the cloud at or before ``t`` or with
``after`` the first one after it, so a reader steps through the topic. All of them read the
recording itself: a live run's through the watcher following it, a finished run's through an
index built on first request and kept per run and topic. A run without the topic, or with
no frame of it yet, is a ``404`` that says so. Rows leave the control plane as
``GET /campaigns/{id}/query.csv?sql=`` (text) or ``POST /campaigns/{id}/query.arrow`` (an
Arrow IPC stream, typed, the request's ``tables`` -- Arrow streams in base64 -- registered
under their names for the query). ``GET /data/campaigns/{id}/archive`` streams a tar.gz of the
campaign, tarred from the campaign directory as it is read. Every service answers it:
refusing because "the results are already on this host's filesystem" would assert
something true of a caller on that host and false of everyone else.
``GET /workspaces/{id}/archive`` is the same for a workspace's project files, under a
single top-level directory. It is a control-plane route rather than a data one, because a
workspace is not on the results volume the data routes serve.

An **export** is the campaign's tables as files, with its records and, if asked, its
recordings, built for one request (:ref:`results-export`). ``POST /campaigns/{id}/exports``
takes the request (``tables``, ``format``, ``bags``, ``records``) and answers at once with
the export's id and the data-plane route its file will be at; a table the campaign's
catalog does not have is a ``400`` before anything is built, and a campaign still running
a ``409``. ``GET /campaigns/{id}/exports/{export_id}`` is its status -- ``done``, ``error``,
``bytes`` and the row count of every table written so far -- answered from the export's
own record on disk once it is finished, so it survives a restart. The file is
``GET /data/campaigns/{id}/exports/{export_id}``, which a token scoped to the campaign may
fetch like its archive.

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
each card polls the status every 1.5 seconds, and the list stream re-lists the newest hundred
campaigns once a second for as long as any tab is open. So the cost of a field there is multiplied by campaigns on
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
     - a run's tables as it records, ``GET /data/campaigns/{id}/live``
     - its own stream, per run

The **series** row is the one that gets this wrong. ``Status`` carried a ``batch_history`` — one entry
per batch, growing for the whole run — that **nothing ever read**, on the payload polled most
often in the system. It was replaced by ``GET /campaigns/{id}/search/history``, which is requested
only while something is displaying it and re-requested only when ``batches_done`` (a single integer
on the status) moves. A series is almost never so small that it belongs on the status; if it grows
with batches, runs, or time, it does not.

``GET /admin/events`` is the **series** row done the way that row prescribes: its own
cursor-keyed route, requested by whatever is displaying it and resumed from ``next_seq``, rather
than a field on a payload every open tab re-fetches once a second.

It is also the one durable thing this service keeps about itself. ``/admin/log`` is this
process's recent stderr and dies with it, and the usage samples say the same about themselves —
both answer "what is it doing *now*". The events worth keeping are the ones a restart destroys,
which is why they are in SQLite on a mounted volume rather than a third ring.

What it records is what this service otherwise says once and forgets. **Every refusal a client
was sent** — composed in the request that refused it, rendered once, and then gone; recorded in
the exception handlers, so a route that raises its own error, a request that never parsed, and
a failure nobody caught are all in it rather than only the calls that happen to be wrapped. And
**each campaign's lifecycle** — started, finished, failed, stopped, uploaded, postprocessed —
which otherwise reaches a phone over ntfy and a tab as a toast, neither of which anybody can
read back. What is *not* in it: successful requests, so it stays a record of what happened and
what would not rather than a request trace; a URL matching no route, which is a caller's
mistake about the address space and unbounded from anything that scans; and the hourly
heartbeat, which says a campaign is alive *now* and is worthless once it is over.

Identical refusals inside a one-minute window collapse to one row carrying ``repeated``: a
panel polling an endpoint that cannot answer it would otherwise push a month of everything
else past the log's row bound within the hour.

``GET /admin/mcp-tools``, ``GET /admin/mcp-calls`` and ``GET /admin/mcp-calls.csv`` follow the
same row for the MCP surface: what the tools were asked and what they answered, its own routes,
requested by the panel displaying them and never carried on a polled payload. The ranking is an
aggregate **over** the call log rather than a counter maintained beside it, so the two cannot
drift; the CSV is the same rows as a download.

Their record is a SQLite file of its own, ``mcp_calls.db`` on the workspaces volume beside
``events.db``, rather than rows in the event log, which is the one place these depart from the
events above: the rows carry a truncated copy of each call's arguments and answer, so they are
bulky and they age out on their own bounds (30 days, or 200 000 calls, whichever bites first),
apart from the event log's tighter ones (30 days, or 20 000 rows). Both bounds are reported on
the ranking's response
(``max_age_s``, ``max_rows``), because a reader told "a month" during a burst that emptied it in
a day would be told a wrong thing.

A page of ``/admin/mcp-calls`` reports the same way. It carries ``total``, ``truncated`` and the
``limit``/``offset`` it was actually read with, because a page that reported none of them read as
the whole record — and beside a ranking summarizing a month, a page holding an afternoon is a
disagreement nothing announced. ``offset`` walks the rest. The CSV export is bounded only by what
is retained, since it streams rather than being held in one response; a download has no field to
report a bound in, so an export that did not reach the end of the record says so in the filename
it arrives under, which is the part of a saved file a reader still has later.

Each row names its caller twice, because the two answer different questions. ``actor`` is the
resolved principal: the name it gave and the source it authenticated by. ``session`` is
``"<client>/<session>"`` as the transport reports them, which is what separates two agents
sharing one token. Either is empty where the transport resolves none, which is absent rather
than anonymous.

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
is what the service registers, including routes added by installed endpoint plugins, so a
route that exists is listed and one that does not is not.

A campaign's tables have two routes of their own, neither needed for an answer — a query
builds what it names: ``POST /campaigns/{id}/tables/build`` builds a finished campaign's tables
for every run in the background (a ``tables`` list narrows it; progress is the campaign log's
``TABLES`` section), and ``DELETE /campaigns/{id}/tables`` removes them to free storage,
refused while the campaign runs or its tables are being built.

Routes
======

.. http-routes::
