.. _future-work:

===========
Future Work
===========

Directions that are designed but not yet implemented. Recorded here so the
intent and the reusable building blocks are not lost.


.. _future-llm-analysis:

LLM-authored analysis and a web-UI figure layer
-----------------------------------------------

**Motivation.** The MCP server is increasingly driven by an LLM rather than a
human. Analysis today has two insertion points, both aimed at people:

* a **postprocessing plugin** (``robovast.postprocessing_commands`` or a local
  ``./file.py:Class`` ref) that turns rosbags into per-run CSVs, which
  :func:`generate_data_db` consolidates into ``<campaign>/_execution/data.db``;
  that database is already queryable over MCP via the ``run_data`` tools
  (``list_run_data_tables`` / ``query_run_data_table`` / ``inspect_run_data_table``).
* an ``evaluation.visualization`` **Jupyter notebook** that reads an injected
  ``DATA_DIR`` and is rendered to a static ``overview_*.html`` with matplotlib
  via ``nbconvert`` (``result_analyzer/widgets/jupyter_widget.py``).

There is **no machine-readable figure layer** — no plotly, no Vega-Lite, no JSON
chart description anywhere in the codebase. That is the gap for both an LLM and
the planned web UI.

**Proposed direction.** A declarative, per-project analysis mechanism that
queries ``data.db`` (plus ``test.xml`` pass/fail via
:func:`read_test_result`) and emits **plotly / Vega-Lite JSON figure
descriptions** rather than pre-rendered images. Benefits:

* Machine-readable and diff-able; the web UI can render the JSON directly and
  interactively (zoom, hover, toggle series) instead of embedding a static PNG.
* LLM-authorable and LLM-inspectable — the model can already read ``data.db``
  through the ``run_data`` tools, so it can *propose* a figure spec, and a tool
  could *validate/execute* one against a campaign.
* Sits alongside the notebook path (which stays for rich, code-driven analysis);
  the two are complementary.

**Sketch.** An ``analysis`` spec (declarative: table + filter/aggregate +
encoding) resolved to a query over ``data.db`` → a plotly figure JSON. Exposed
as (a) an ``evaluation.visualization`` variant referencing a ``.json`` spec
file, and (b) an MCP tool ``render_analysis(campaign, spec) -> figure_json`` for
the web UI / LLM loop. Natural data sources: ``_execution/data.db`` and the
JUnit ``test.xml`` per run.

