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

The service binds ``127.0.0.1`` by default and assumes a trusted caller: there is no
authentication. Reaching a remote one is an SSH tunnel, not a public bind — see
:doc:`deployment` for the boundary and ``vast serve --attach``.

Two consequences show up in the table. ``GET /version`` redacts ``results_root`` and
``sources_root`` for a non-loopback client, because those are filesystem paths that are
only useful — and only safe — to a caller on the same machine. And the file routes serve
real paths on the service host, which is the point of the address space below.

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
       that is not running; asking a *local* service for a cluster archive).
   * - ``422``
     - A notebook or visualization failed to render.
   * - ``501``
     - Workspaces are not configured on this service.

Streaming
=========

Four routes stream instead of returning a body. The two ``.../stream`` log routes and
``GET /campaigns/events`` are **server-sent events**; they are resumable, so a client that
drops sends ``Last-Event-ID`` and continues from the line after the one it last saw rather
than replaying the whole log. ``GET /campaigns/{id}/archive`` streams a tar.gz of a cluster
campaign's results as they are fetched from the object store.

Every tick of an SSE stream that had nothing to report sends a ``heartbeat`` event. It is a
named event rather than the SSE comment such keepalives usually are, because a comment is
invisible to ``EventSource`` — it holds proxies open and tells the client nothing. Without
a frame the client can see, a stream that is merely quiet is indistinguishable from one
whose socket died in a suspended laptop or a torn-down ``kubectl port-forward``: no error is
raised, ``readyState`` stays ``OPEN``, and no further byte ever arrives. A client should
therefore treat a gap of several heartbeats as a dead connection and open a new
``EventSource``; the web UI does exactly that (see :doc:`web_ui`).

Paths are defined once
======================

:class:`robovast.service.interface.Routes` holds the canonical path strings and the
builders for parameterised ones. Both the app and ``HTTPTransport`` use it so the two
bindings cannot drift — a route renamed in one place is renamed for the client too.

The table below is **generated from the running application**, not maintained by hand: it
is what the service registers, including routes added by installed endpoint plugins. A
hand-written endpoint list is exactly how the retired synthetic run-file route came to look
documented while matching no directory on disk.

Routes
======

.. http-routes::
