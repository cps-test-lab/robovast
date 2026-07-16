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


.. _future-variation-authoring:

Variation-plugin authoring & debugging via MCP
----------------------------------------------

**Motivation.** An LLM using the MCP will increasingly want to *create* custom
variation types, not just compose existing ones. The plumbing already exists:

* the :class:`~robovast.common.variation.base_variation.Variation` base class
  (override ``variation(self, in_configs)``; optional ``CONFIG_CLASS`` pydantic
  model; optional ``get_required_container`` for sidecars);
* a variation is referenced from a ``.vast`` by entry-point name **or** by a
  local ``./path.py:Class`` file ref resolved against the ``.vast`` dir;
* external (out-of-tree) plugins are shipped into the cluster controller pod
  automatically (``controller_launcher.discover_plugin_installs`` builds/collects
  wheels; no image rebuild needed);
* a collect-all checker already exists —
  :func:`robovast.common.variation.loader.validate_variation_plugins` returns
  ``[(name, errors)]``.

**Local-plugin checking — done.** Rather than a dedicated per-type MCP checker,
the collect-all linter now resolves and interface-checks *every* plugin
reference a ``.vast`` carries — variation types, the
``results_processing``/``search`` postprocessing commands, and the search
strategy and extractor — whether installed entry-point names or local
``./path.py:Class`` file refs. It reuses the runtime resolvers
(``resolve_postprocessing_plugin``, ``load_ref``) so validation matches
execution, and it is exposed on both surfaces from one shared core
(:func:`robovast.common.config_validation.validate_project_file`): the
``validate_project`` MCP tool and the ``vast configuration validate`` CLI
command. This turns "write plugin → launch campaign → read a cryptic
controller-pod log" into a fast local edit/validate loop.
Discovery of available plugin types and their interfaces is provided separately
by the ``plugin_metadata`` MCP tools (``list_plugin_groups`` / ``list_plugins`` /
``get_plugin_details``).

**Still open.** A ``scaffold_variation_plugin(name) -> template`` MCP tool that
emits a ready-to-edit ``Variation`` subclass template (with a ``CONFIG_CLASS``
stub and a documented ``variation()`` contract). A **dry-run** of a variation's
``variation()`` on sample parameters (surfacing runtime errors, not just
interface ones) would extend the linter's static checks.

