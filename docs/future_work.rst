.. _future-work:

===========
Future Work
===========

Directions that are designed but not yet implemented. Recorded here so the
intent and the reusable building blocks are not lost.


.. _future-raster-egress:

Visual artifact egress over MCP (video and rasters)
---------------------------------------------------

**Motivation.** Analysis over MCP is increasingly driven by an LLM. Quantitative
questions are well served: per-run metrics are consolidated into
``<campaign>/_execution/data.db`` and queried with read-only SQL via the
``run_data`` tools (``describe_campaign_data`` / ``query_campaign_data_sql``), and
a campaign's author-declared charts are exposed as Vega-Lite specs by
``list_campaign_plots`` (from ``evaluation.plots`` in the snapshot ``.vast``).

What is **not** reachable is any raster or video output. ``get_run_output_file``
deliberately refuses binary files, so ``rosbags_to_webm`` recordings and rendered
PNG plots cannot travel to the model. ``list_campaign_plots`` + SQL covers
*charts* (declarative specs the client renders), not *pixels*: a trajectory
overlay, a costmap, or a landing-scatter image cannot be seen. For a
robotics-simulation tool this is the main remaining analysis gap — 5000 rows of
poses is not how a human notices a robot driving into a wall.

**Open questions** (to decide, not yet decided):

* Should the model receive images as MCP image content, and for which artifacts?
* Is video better delivered as a short-lived artifact/link than inline, given
  size?
* The ``nav`` plugin (``robovast_nav``) already returns images via
  ``draw_map`` / ``display_simulation_screenshot`` — is that the pattern to
  generalize, or is a campaign-level artifact route the right home?


.. _future-llm-analysis:

Server-rendered figures (optional)
----------------------------------

A machine-readable figure layer **now exists**: a campaign's ``evaluation.plots``
declare ``{title, query, vega_lite}`` entries, surfaced over MCP by
``list_campaign_plots`` and over the service by ``GET
/campaigns/{id}/plots``. The web UI can render those Vega-Lite specs directly, and
an LLM can read ``data.db`` through the ``run_data`` SQL tools to *propose* new
ones.

What is *not* built is a server-side ``render_analysis(campaign, spec) ->
figure_json`` that resolves a declarative spec to a figure on the server. It is an
open question whether this is worth building at all: an LLM that can write SQL and
emit a Vega-Lite spec itself may not need the server to render one. Retained here
as a possibility, not a commitment. The ``evaluation.visualization`` Jupyter
notebook path (rendered to a static ``overview_*.html`` via ``nbconvert``) is
unaffected and stays for rich, code-driven analysis.
