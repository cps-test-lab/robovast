.. _results-processing:

Results Processing
==================

Every RoboVAST execution produces a results directory with a well-defined layout.
This page documents the output structure and how to postprocess and merge results
using the ``vast results`` command group.


.. _results-output-structure:

Output Structure
----------------

The results directory path is configured during ``vast init`` and stored in
the ``.robovast_project`` file.

Top-Level Layout
^^^^^^^^^^^^^^^^

.. code-block:: text

   <results-dir>/
   └── <campaign-name>-<timestamp>/          # One per execution (e.g. dynamic_obstacle-2026-03-04-152130)
       ├── metadata.yaml                     # Campaign metadata (auto-generated)
       ├── _config/                          # Campaign-level configuration snapshot
       ├── _execution/                       # Execution metadata
       ├── _transient/                       # Intermediate/preprocessed data
       ├── _jobs/                            # Per-job artifacts (sysinfo, resource usage, logs)
       └── <config-name-1>/                  # One directory per configuration variant
       └── <config-name-2>/

Campaign-Level Directories
^^^^^^^^^^^^^^^^^^^^^^^^^^^

``_config/`` — Configuration Snapshot
""""""""""""""""""""""""""""""""""""""

A copy of all input files used during execution — and the source a **retrigger** reconstructs the
campaign from. To run a campaign again exactly as it ran, use **Retrigger campaign** in the web UI's
campaign actions menu, ``start_campaign(from_campaign=<id>)`` over MCP, or
``POST /campaigns/<id>/retrigger``: all three read this snapshot together with the image recorded in
``_execution/`` and start a new campaign, leaving this one untouched.

Doing it by hand instead re-points a project at the snapshot:

.. code-block:: text

   vast init <campaign-dir>/_config/<config-name>.vast
   vast execution cluster run

That path **rebuilds** the image rather than reusing the one the campaign recorded, so it needs the
sources the ``build:`` section names — which are *not* archived here. It is the escape hatch for when
the recorded image is gone; otherwise prefer the retrigger, which reuses the exact bytes.

The structure inside is domain-specific, but typically includes:

.. code-block:: text

   _config/
   ├── <name>.vast                            # The .vast configuration used
   ├── scenario.osc                           # OpenSCENARIO scenario file
   ├── analysis/                              # Jupyter notebooks for analysis
   │   ├── analysis_run.ipynb
   │   ├── analysis_config.ipynb
   │   └── analysis_campaign.ipynb
   └── <run-files defined within vast-config> # e.g. launch files, models, scripts, parameters

``_execution/`` — Execution Metadata
"""""""""""""""""""""""""""""""""""""

.. code-block:: text

   _execution/
   ├── launch.yaml                           # How the campaign was ASKED FOR (see below)
   ├── execution.yaml
   ├── plugin_install.log                    # ``plugin install`` phase (pip output; only when plugins are declared)
   ├── variation.log                         # ``variation`` phase (config-variation expansion)
   ├── controller.log                        # ``run`` phase — campaign controller log
   ├── postprocessing.log                    # ``postprocessing`` phase (rosbag→CSV + data.db)
   └── share.log                             # ``share`` phase (upload-to-share, when retriggered)

Each pre-/post-run **phase** writes its own log file here; the service concatenates
them in phase order into the single live campaign log the web UI streams. The
``plugin install`` phase (present only when the ``.vast`` declares ``plugins:``) runs
first and captures the ``pip install`` output live, exactly like ``building``,
``variation`` and ``postprocessing``.

**A re-triggered step appends to its own phase file**, so the campaign log shows what the
step you just asked for actually did rather than only what the original run did.
``postprocessing.log`` grows on each ``run_postprocessing``; a re-triggered upload-to-share
writes ``share.log``, which is a phase of its own because an upload is not postprocessing and
a divider naming the wrong step is worse than no divider. (The share that runs *inside* a
campaign is part of the controller's own narrative and stays under ``run``.)

``execution.yaml`` contains:

- ``execution_time``: ISO timestamp of when the execution started
- ``robovast_version``: Git commit hash of the robovast version used
- ``runs``: Number of runs per configuration
- ``execution_type``: ``cluster`` or ``local``
- ``image``: the configured execution image reference (may be a floating tag such as
  ``…:latest``)
- ``image_revision``: the **immutable digest** the run pods actually used
  (``repo@sha256:…``), captured at run time on the cluster backend. Re-postprocessing
  reuses this exact image, so a later re-run deserializes the recorded bags against the
  same image the runs used even if the tag has since moved.
- ``cluster_info``: Node count, labels, CPU manager policies (cluster only)

.. _campaign-launch-record:

``launch.yaml`` records the **request**, where ``execution.yaml`` records what happened:
``config_filter``, ``campaign_name``, ``runs`` (as *requested*), ``postprocess``,
``upload_to_share``, ``show_gui`` and ``backend``. It exists because the request was otherwise
unrecoverable — ``config_filter`` in particular was consumed during config expansion and kept
nowhere — so "was this the full sweep or a one-config pilot?" could not be answered about a finished
campaign, by a person or by a retrigger.

Read the two together for ``runs``: ``launch.yaml``'s ``runs: 0`` means "take the ``.vast``'s
``execution.runs``", so ``0`` beside ``execution.yaml``'s ``runs: 3`` says the ``.vast`` asked for 3,
while ``1`` beside ``1`` on a ``.vast`` declaring 3 says someone piloted it. ``metadata.yaml`` nests
this under ``execution.launch`` so a published campaign is one document. It is written by the service
before the run starts (which is why it is a separate file: ``execution.yaml`` is written *by* the run,
and a campaign that fails before its first batch would otherwise have no record of what it was asked
to do). Campaigns from before this file existed simply have none.

``controller.log`` captures the campaign controller's own log for the whole run —
batch/search progress, backend job dispatch, postprocessing and stopping
decisions. For cluster runs the ``robovast-service`` writes this as it drives the
campaign (and the web UI streams it live); it is preserved in the campaign so a
downloaded or shared campaign is self-documenting without a live cluster.

``_transient/`` — Intermediate Data
"""""""""""""""""""""""""""""""""""""

.. code-block:: text

   _transient/
   ├── configurations.yaml                   # Fully resolved configuration parameters
   ├── entrypoint.sh                         # Generated container entrypoint script
   ├── secondary_entrypoint.sh               # Generated secondary container entrypoint script
   └── collect_sysinfo.py                    # System info collection script

``configurations.yaml`` contains the fully resolved parameter values for every
configuration variant, including internal computed fields like navigation path waypoints
(``_path``), raster points (``_raster_points``), resolved file paths, and
``_variations`` (list of applied variation plugins with name, start time, duration,
and any plugin-specific fields).

Configuration Directory
^^^^^^^^^^^^^^^^^^^^^^^

Each configuration variant gets its own directory:

.. code-block:: text

   <config-name>/
   ├── _config/
   │   ├── config.yaml                       # Configuration identifier hashes
   │   ├── scenario.config                   # Resolved parameter values (YAML)
   │   ├── maps/                             # [navigation only]
   │   │   ├── <name>.pgm                    # 2D occupancy grid image
   │   │   └── <name>.yaml                   # Map metadata (resolution, origin, thresholds)
   │   └── 3d-mesh/                          # [navigation only]
   │       ├── <name>.stl                    # 3D environment mesh
   │       └── <name>.stl.yaml               # Mesh metadata
   ├── _transient/                           # Per-config intermediate files
   └── <run-number>/                         # 0, 1, 2, ... (one per run)

``scenario.config`` contains the actual scenario parameter values used for this
configuration, wrapped in a single key matching the scenario name:

.. code-block:: yaml

   test_scenario:
     growth_rate: 0.5
     initial_population: 50

Run Directory
^^^^^^^^^^^^^

Each run directory holds the **scenario output** for one configuration at one run
number, plus a ``job`` symlink to that run's job-level artifacts:

.. code-block:: text

   <run-number>/
   ├── test.xml                              # JUnit test result (pass/fail, duration)
   ├── job -> ../../_jobs/job-N              # symlink to this run's job artifacts (see below)
   └── <test-specific files>                 # Domain-specific scenario output, e.g. out.csv,
                                             #   or a scenario-recorded rosbag2/ bag directory

Anything the *scenario* itself produces (``test.xml``, scenario-recorded
``rosbag2/``, domain output) stays in the run directory. Infrastructure and
monitoring artifacts (``sysinfo.yaml``, ``resource_usage_*.csv``, the system
log, and the entrypoint's ``/rosout`` + ``/clock`` recording) belong to the **job**
and live under ``_jobs/job-N/`` — reachable via the ``job`` link, e.g.
``<run>/job/sysinfo.yaml`` (see :ref:`job-directory`).

Postprocessing adds two derived files to the run directory: ``run_log.csv``, the job's
container logs joined with ``/rosout`` and sliced to this run (see
:ref:`merged-run-log`), and ``resource_usage.csv``, the job's resource-monitor samples
sliced the same way (see :ref:`per-run-resource-usage`). They live here, rather than beside
the job artifacts they were built from, because a run is what they describe — and because
the ingest that turns data files into tables globs run directories.

A common example of test-specific output is a scenario-recorded ``rosbag2/``
directory (standard ROS 2 bag in MCAP storage, with a ``metadata.yaml`` listing
recorded topics and message counts). It is present only when the scenario
records a bag, and is distinct from the separate, job-level ``/rosout``
recording under ``_jobs/job-N/logs/``.

.. _run-clock:

One clock per run
"""""""""""""""""

**Every table a postprocessing step derives from a bag timestamps its rows with the bag's
receive time.** Under ``bag_record(use_sim_time: true)`` that is the *simulator's* clock, and it
is the right one to key on: it is what the simulator actually stepped, so it is identical across
simulators and independent of how fast the machine ran — the same trial can take 520 s of wall
time on one backend and 100 s on another and still span the same sim seconds.

That shared clock is what lets the poses, the costmaps and the behaviour trees be read against
each other at all, and it is the timeline the web :ref:`Run view <run-view>` scrubs. So a topic
that carries a **clock of its own** must be converted, with its original stamp kept in a
separately-named column rather than used as ``timestamp``. There is a real instance:
``/behavior_tree_log`` is stamped by nav2 from a wall clock even under ``use_sim_time``, roughly
1.8e9 s away from sim time. Keying ``nav2_behavior_tree`` on it made the table unjoinable with
every other one and put each transition outside the run's timeline entirely, so the behaviour-tree
panel had nothing to show at any playback time; it is now the receive time, with nav2's stamp in
``event_timestamp``.

Recording ``/clock`` in the scenario's ``bag_record(...)`` is worth the negligible space: it makes
the sim↔wall mapping recoverable, so a foreign-clock topic can be *related* to sim time afterwards
instead of guessed at.

.. _clock-map:

Wall → sim: the clock map
"""""""""""""""""""""""""

Everything a run *logs* is stamped in wall time — rosout's receive time, and whatever each
container printed. Everything it is *analysed* on is sim seconds. Relating the two is a per-run
**clock map**: a list of ``(wall_ts, sim_ts)`` samples, interpolated piecewise-linearly.

**A single offset is wrong**, which is why this is a sampled map and not a number. Measured on a
recorded run, ``dt/dw = 1.369`` — the simulator ran 1.37× faster than the wall clock, so an offset
taken at the start is ~8 s out by the end of a 29 s run. Sim time can also pause.

Two producers, one format:

* **ROS** — the *entrypoint's own* recorder (``execution.log_topics``, default
  ``[/rosout, /clock]``) writes a bag in **wall** time for the whole container's life, so each
  ``/clock`` message is an exact (wall receive, sim content) pair. Deliberately not the scenario's
  ``bag_record``: that one is sim-time on both axes, so it cannot carry the mapping, and it starts
  mid-run where the interesting failures are already over.
* **rst (non-ROS)** — ``rst.capture`` streams ``<recording>.clock_map.csv`` beside the ``.npz``,
  flushed per sample so a run killed by a timeout still has one.

The samples are **decimated**: one is dropped only when linear interpolation reproduces it within
5 ms, so a steady stretch costs two rows while a pause or a change of real-time factor keeps
exactly the samples that describe it (12000 ``/clock`` messages → 26 rows, measured).

**Outside the sampled range there is no answer, and none is invented.** The samples begin when the
simulator started publishing its clock, typically well after the container did, so a line logged
during image boot has no sim time — a different statement from "we could not compute it".
``runs.clock_map_source`` (``ros_clock_bag`` / ``rst_run_npz`` / ``none``) says which producer
answered, so a reader can tell a *missing* map from a *quiet* one.

.. _merged-run-log:

``run_log`` — everything the run said
""""""""""""""""""""""""""""""""""""""

One row per log **event**, across every container, on the run's own playback clock. Written by
the auto-injected ``run_log`` postprocessing plugin as ``<run>/run_log.csv``, which the usual
one-file-one-table ingest turns into the ``run_log`` table.

It is a **join**, not a concatenation, because a run's output arrives twice: a launch container
forwards its nodes' output to stdout *and* those nodes publish ``/rosout``. On a measured
three-container campaign 473 of 521 rosout rows are the same event as a ``system*.log`` line, so
appending both would report most of the run twice. Matching is exact on ``(node, wall_ns, first
line of message)``, keyed on the **producer's** stamp — keying rosout on the bag's *receive* time
instead matched 0 of 521, because the transport delay puts every pair on a different nanosecond.

The join also supplies what neither source has alone: ``/rosout`` names the node but never the
*container* it ran in, and that is what a reader filters by. It comes from the file the stdout
twin was found in.

Columns: ``sim_time``, ``wall_ts``, ``time_source``, ``in_window``, ``container``, ``node``,
``source``, ``level``, ``severity``, ``message``, ``file``, ``function``, ``line``.

* ``source`` — ``rosout`` or ``stdout``. ``WHERE source = 'rosout'`` is the rosout slice; there is
  no separate ``rosout`` table, which would be a strict subset of this one.
* ``time_source`` — how the row got its wall time: ``stamp`` (the producer's own, ns precision),
  ``inherited`` (a continuation line, e.g. a traceback frame, taking the time of the event it
  belongs to), or ``none`` (nothing stamped anywhere before it).

  **Producers stamp their own lines**, which is why ``stamp`` is the normal case rather than a
  ROS-only luxury: rclpy writes the stamp, and so do the entrypoints' ``log`` helper and
  scenario-execution's logger. There is no capture step anywhere — a helper mounted in every
  container used to timestamp each line as it was read, which meant a second copy of every log
  line on disk (119 KB against a 108 KB ``system.log``, measured) to describe output that was
  RoboVAST's own and could simply say when it spoke.

  Third-party output that stamps nothing (a gz warning, a vanilla sidecar) is **never dropped**:
  it gets a row per line, inheriting from a neighbouring event where one exists and reporting
  ``none`` where it does not. An untimed row is honest about being untimed; it is deliberately
  not backfilled from the next stamp, which would render exactly like a real time and claim the
  container booted at whatever second the first node came up.
* ``in_window`` — 0 for a line outside this run's own wall window. In a packed multi-run job those
  are the simulator being reset between runs: real output, attributed to the nearest run rather
  than dropped, and flagged so a query can tell "during the trial" from "getting ready for it".
* ``severity`` — from ``common.log_summary.severity_of``, the same definition the status verdict
  and the MCP log tools use.

Read it from the web :ref:`Run view <run-view>`'s ``log`` panel and the Explorer's **Log** tab, or
from an agent with ``search_run_logs``. What it makes possible that a log *stream* cannot:

.. code-block:: sql

   -- which runs logged this, and did they fail?
   SELECT r.config_name, r.run_id, r.passed, count(*) AS hits, min(l.sim_time) AS first_at
   FROM run_log l JOIN runs r USING (config_name, run_id)
   WHERE l.message LIKE '%CRITICAL FAILURE%'
   GROUP BY 1, 2 ORDER BY hits DESC;

The raw streams stay files and stay the record of what was printed, and ``get_campaign_log`` /
``get_job_log`` remain the way to read a campaign that is still running, since ``data.db`` does not
exist yet.

Those files now carry a prefix on every line, including the infrastructure ones: ``Running as UID:
1000`` reads ``[INFO] [1786264427.117714] [entrypoint]: Running as UID: 1000``. That is the cost of
each line being placeable in time and attributable, and it is the shape most of the file already
had — 563 of 570 lines in a measured ROS run were stamped by their producer. Anything matching
``^Running as UID`` needs updating; the live log panel is unaffected mechanically (it pages bytes)
but shows the prefixes too.

.. _per-run-resource-usage:

``resource_usage`` — what the run cost
"""""""""""""""""""""""""""""""""""""""

One row per container per process **name** per ~1 s sample, on the run's own playback clock.
Written by the auto-injected ``resource_usage`` plugin as ``<run>/resource_usage.csv`` from
the job's ``resource_usage_<container>.csv`` files, and ingested as the ``resource_usage``
table like any other per-run CSV.

Why it is a table and not just those files: a lane gives a job a fixed number of cores, so a
simulator that starves the stack changes what the stack does. That is a competing
explanation for any behavioural result, and it can only be ruled out in the same query as
the behaviour.

.. code-block:: sql

   -- did anything run out of CPU during the trial?  (cpu_percent is PER-CORE:
   -- one saturated core is 100, so the ceiling is 100 * available_cpus)
   SELECT u.container, MAX(u.cpu) AS peak, 100.0 * r.available_cpus AS saturation
   FROM (SELECT config_name, run_id, container, wall_ts, SUM(cpu_percent) AS cpu
         FROM resource_usage WHERE in_window = 1 GROUP BY 1, 2, 3, 4) u
   JOIN runs r USING (config_name, run_id)
   GROUP BY 1, 3;

Three things differ from ``run_log``, each deliberate:

* **Ticks are partitioned, not shared.** ``run_log`` gives every run of a packed job all of
  the job's lines, flagged — a line printed during another run is still evidence about this
  one. A *sample* is not: another run's CPU is not this run's. Each tick is therefore claimed
  by exactly one run (the gap between two runs falls to the one starting up), so ``SUM`` over
  a job's runs is what that job consumed. Copying instead would make every aggregate over a
  packed campaign report a multiple of the truth, with nothing raising an error.
* **Rows are keyed by process name, not pid.** Pids churn — a respawned node is a new pid and
  the same program — and no pid is comparable across runs. ``num_pids`` records how many
  shared a name in that tick.
* **A run of a packed job with no ``test.xml`` claims nothing** and gets an empty table
  rather than the whole job's samples. It cannot be placed on the wall clock, and a table
  saying "no data" is honest where one stating another run's numbers is not. A *single*-run
  job in that state still gets its whole trace — there is no other run to confuse it with,
  and a run killed mid-flight is the one whose trace matters most.

``cpu_percent`` and ``memory_rss_bytes`` are sums over the processes sharing a name, and both
carry a ``_column_notes`` entry that ``describe_campaign_data`` shows: CPU is per-core, and
summed RSS double-counts pages shared with forks.

.. _scenario-verdict:

``scenario_timestamps`` — where the trial ended
""""""""""""""""""""""""""""""""""""""""""""""""

One row per run: ``config_name``, ``run_id``, ``timestamp`` (sim seconds), ``wall_ts``,
``status`` (``succeeded`` / ``failed``) and the ``message`` itself. Built while ingesting
``run_log``, from the first line scenario-execution's own logger wrote announcing a verdict —
``Scenario '<name>' succeeded.``, or the failure line ``add_result`` logs. The recognition lives
in one module, :mod:`robovast.common.scenario_markers`, and runs **here and nowhere else**: every
later reader queries this table instead of matching the log text again, which is what keeps the
web UI, ``search_run_logs`` and the playback clock from disagreeing about where a run ended.

**Both clocks, because they answer different questions.** ``timestamp`` is what the playback
timeline is measured in. ``wall_ts`` is what ``run_log`` is *ordered* by, and it is the one the
log is cut on — the clock map does not extrapolate, so a run whose ``/clock`` stopped during
shutdown has NULL ``sim_time`` on every line after the verdict, sometimes including the verdict
itself. A sim-time comparison would keep exactly the lines a reader wanted rid of.

Everything after ``wall_ts`` is **shutdown**, not the trial: nodes being killed, lifecycle
transitions failing because their peer is already gone, TF errors from a publisher that has
stopped. That is what the run view's :ref:`shutdown toggle <shutdown-toggle>` and the log tools'
``hide_shutdown`` cut, both on by default.

``status`` is the **scenario's** verdict and can legitimately disagree with the run's
``test.xml`` verdict in ``run_view.status``; comparing the two finds a scenario that reported
success while the harness failed, or the reverse. A NULL row is a run that reached no verdict —
killed by its deadline, say — and is left untrimmed rather than trimmed to a guess.

``test.xml`` — JUnit Test Result
"""""""""""""""""""""""""""""""""

Standard JUnit XML format with scenario execution results:

.. code-block:: xml

   <testsuite errors="0" failures="0" name="scenario_execution" tests="1" time="49.03">
     <testcase classname="tests.scenario" name="test_scenario" time="49.03">
       <properties>
         <property name="start_time" value="1772634122.583653"/>
       </properties>
     </testcase>
   </testsuite>

Each run's ``test.xml`` is the runner's contract for that run's outcome. The
controller mirrors it into ``campaign.db``'s ``run`` table at record time (status,
pass/fail, errors/failures, duration, start time), so per-run outcomes are
queryable live and the postprocessed ``data.db`` ``runs`` view is built from those
rows rather than by re-parsing every ``test.xml`` — see
:ref:`the campaign store schema <campaign-store>`.

.. note::

   ``metadata.yaml`` is **not** how a caller reads a campaign's results. It is written
   only by postprocessing, so nine MCP tools that parsed it answered "run postprocessing
   first" for campaigns whose outcomes were already recorded in ``campaign.db``; they were
   replaced by read-only SQL over ``run_view`` (see :ref:`mcp-analysis`). The file remains
   as the campaign's self-contained metadata document — what the FAIR/PROV-O export and
   the publication zip read.

.. _job-directory:

Job Directory
^^^^^^^^^^^^^

``_jobs/job-N/`` holds the artifacts of one *job* — the unit of dispatch (one
Kubernetes Job, or one local ``docker compose`` run). With the default
``runs_per_job: 1`` there is one job per run; with ``runs_per_job > 1``
several runs share a job (and therefore share these artifacts). Each
run links to its job via ``<run>/job`` (e.g. ``<run>/job/sysinfo.yaml``).

.. code-block:: text

   _jobs/job-N/
   ├── sysinfo.yaml                          # Hardware info (platform, CPU, memory) — stable
   ├── resource_usage_main.csv               # Main container CPU/memory over the job
   ├── resource_usage_<secondary>.csv        # Per secondary container [if multi-container]
   └── logs/
       ├── system.log                        # Main container system log
       ├── system_<secondary>.log            # Secondary container log [if multi-container]
       └── rosout_bag/                       # /rosout recording [ROS mode]

``resource_usage_*.csv`` files have columns ``timestamp`` (wall epoch seconds), ``pid``,
``name``, ``cpu_percent`` and ``memory_rss_bytes``, one row per process per ~1 s, one file
per container. For a packed job these span the whole job; the ``resource_usage``
post-processing step slices them to each run (see :ref:`per-run-resource-usage`).


.. _reading-result-files:

Reading these files
-------------------

Every path in the tree above has one **address**, and it is the same string
whether you type it at the CLI, pass it to an MCP tool, or ``GET`` it from the
service::

    /results/<campaign_id>/<path>

The path after the campaign id is exactly the path in the tree — ``_execution/
outcome.json``, ``<config-name>/<run>/test.xml`` — so what a listing shows is what
you can read. Campaign results are **read-only**: they are the record of a run,
and on the cluster they are object-store objects that a local write could not
change. Workspace *inputs* live in the writable half of the same address space,
``/sources/<workspace_id>/<path>`` (see :ref:`web-ui-config`).

.. code-block:: bash

   vast files ls  /results/nav-2026-03-04-152130/            # _config/ _execution/ …
   vast files ls  /results/nav-2026-03-04-152130/ -r         # whole tree, files only
   vast files cat /results/nav-2026-03-04-152130/_execution/outcome.json
   vast files cat /results/nav-.../hospital-1-42/0/logs/system.log --lines 50 --offset 200
   vast files get /results/nav-.../hospital-1-42/0/rosbag2/bag.mcap ./bag.mcap

``ls`` lists one level at a time — a campaign holds a directory per configuration
and per run, so a recursive listing of the root is thousands of entries; it
reports ``total`` when it truncates. ``cat`` pages text and refuses binary;
``get`` writes raw bytes, which is how you fetch one artifact without downloading
the whole campaign archive (that is ``vast results download``, below).

The same addresses work over HTTP (``curl <service>/results/<campaign>/<path>``)
and from an LLM through the ``read_file`` / ``list_files`` MCP tools — see
:ref:`mcp-files`. Reading a campaign on this machine needs no running service;
against a cluster service the read fetches that one object, not the campaign.

If the service runs on your own machine, ``get_service_info`` also reports a
``results_root`` you can open directly with your own tools; it is absent whenever
that would be a path you cannot actually read.


.. _results-metadata:

``metadata.yaml`` — Campaign Metadata
--------------------------------------

Every campaign directory contains a ``metadata.yaml`` file that is
automatically generated after postprocessing completes.  It aggregates
structural and domain-specific metadata about the entire campaign into a
single file.

The file is produced by a three-phase pipeline:

1. **Generic metadata** — collected by ``MetadataGenerator``
   (``robovast.common.metadata``).  This includes configurations, test
   results (pass/fail, timing, output files, sysinfo), execution metadata,
   run files, and the scenario file reference.

2. **Variation-plugin metadata** — each variation plugin used during
   configuration generation can contribute additional metadata by overriding
   the ``collect_config_metadata`` classmethod
   defined on the ``Variation`` base class.  For example,
   ``FloorplanGeneration`` overrides ``collect_config_metadata`` to load map
   and mesh YAML metadata from ``_config/``.  The ``variations`` field in
   each configuration entry lists all variation plugins that were applied,
   together with their execution timing (``name``, ``started_at`` as ISO
   timestamp, ``duration`` in seconds).

3. **User-defined metadata processors** — custom plugins registered under
   the ``robovast.metadata_processing`` entry-point group and configured
   in the ``.vast`` file (see below).

Example structure of ``metadata.yaml``:

.. code-block:: yaml

   configurations:
     - name: config-1
       config:
         growth_rate: 0.5
         initial_population: 100
       config_files: []
       created_at: '2026-03-04T16:15:03.212496'
       variations:
         - name: FloorplanGeneration
           started_at: '2026-03-04T16:14:55.123456+00:00'
           duration: 3.217
         - name: PathVariationRandom
           started_at: '2026-03-04T16:14:58.340789+00:00'
           duration: 1.842
       test_results:
         - dir: config-1/0
           success: 'true'
           start_time: '2026-03-04T16:16:00+00:00'
           end_time: '2026-03-04T16:16:49'
           output_files:
             - config-1/0/sysinfo.yaml
             - config-1/0/logs/system.log
           sysinfo: { ... }
           postprocessing: {}
   metadata: {}
   run_files:
     - _config/files/growth_sim.py
   scenario_file: scenario.osc
   execution:
     execution_time: '2026-03-04T16:15:02'
     robovast_version: abc123
     runs: 2
     execution_type: cluster
     image: ghcr.io/example:latest

The ``metadata:`` block of the ``.vast`` file is passed through verbatim into
``metadata.yaml`` and is used to configure PROV-O generation (see below).

See :ref:`extending-metadata-processing` and :ref:`extending-variation-metadata`
for how to add custom metadata processing plugins and variation metadata hooks.


.. _results-prov-metadata:

``metadata.prov.json`` — PROV-O Provenance Graph
-------------------------------------------------

After ``metadata.yaml`` is written, RoboVAST automatically generates a
`W3C PROV-O <https://www.w3.org/TR/prov-o/>`_ provenance graph as
``<campaign-dir>/metadata.prov.json`` (JSON-LD format) and an optional
``metadata.pdf`` visualization (requires Graphviz ``dot``).

The graph captures the full execution lineage of the campaign as a
cyber-physical system test:

- **Software agents** — RoboVAST and Scenery Builder with version info
- **Campaign activity** — execution type, start time, number of runs
- **Scenario entities** — abstract (``.osc``) and concrete per-configuration scenarios
- **Config-generation activity** — links the ``.vast`` file to the generated configs
- **Per-run activities** — success/failure, timing, sysinfo, output files
- **Domain-specific nodes** — contributed by variation plugins (e.g. map/mesh
  entities for navigation, goal counts, obstacle counts); see
  :ref:`extending-prov-metadata`

**Configuring the provenance graph**

The ``metadata:`` section of the ``.vast`` file controls campaign-level
provenance properties:

.. code-block:: yaml

   metadata:
     dataset_iri: https://purl.org/robovast/datasets/my-dataset/
     # Optional: list of CPS agents (robots, manipulators, etc.) involved
     # in the campaign.  Omit entirely for agent-free campaigns.
     agents:
       - id: turtlebot4
         type: robot
         # Where this agent came from. One IRI, or several, or a mapping when
         # the source carries a version.
         derived_from: https://github.com/turtlebot/turtlebot4/tree/jazzy
         # Files that configure this agent, relative to the configuration root.
         configuration_files:
           - files/nav2_params.yaml
       - id: ur5
         type: manipulator
         derived_from:
           - source: https://github.com/UniversalRobots/Universal_Robots_ROS2_Description
             version: 2.1.0

``dataset_iri``
   Base IRI for the dataset namespace used in the provenance graph.
   All campaign, config, and run IRIs are constructed relative to this
   prefix.  Defaults to ``https://purl.org/robovast/datasets/default/``.

``agents``
   List of `PROV Agent <https://www.w3.org/TR/prov-o/#Agent>`_ nodes
   representing the physical systems under test (robots, manipulators,
   sensors, etc.).  Each entry must have an ``id`` (used as the IRI
   fragment; ``name`` is accepted as a legacy spelling) and may carry
   arbitrary additional properties, which become properties of the agent
   node.  If omitted, no agent nodes are added to the graph — suitable for
   software-only or simulation-only campaigns.

   Two keys are interpreted rather than copied through:

   ``derived_from``
      What the agent was derived from, as ``prov:wasDerivedFrom``. Write a
      single IRI, a list of them, or — when the source has a version worth
      recording — a mapping with ``source`` and optional ``version``. An
      entry that names no source is skipped with a warning rather than
      dropping the campaign's provenance graph.

   ``configuration_files``
      Paths, relative to the configuration root, of the files that configure
      this agent. Each is matched against the campaign's recorded
      ``run_files`` and aggregated into one plan entity per agent. A path
      with no match warns and is skipped, since a plan pointing at a file
      the campaign never carried would misdescribe the run.

Domain-specific provenance nodes (e.g. navigation map/mesh entities) are
contributed automatically by variation plugins that implement
``collect_prov_metadata``; no manual configuration is required.


.. _results-postprocessing:

Postprocessing
--------------

Postprocessing transforms raw run output (e.g. ROS bags, custom binary files) into
analysis-friendly formats (e.g. CSV).  Commands are defined in the
``results_processing.postprocessing`` section of the ``.vast`` file and executed by plugins
(see :ref:`extending-postprocessing` for how to write your own).

.. code-block:: bash

   vast results postprocess [OPTIONS]

**Options**

.. option:: -r, --results-dir PATH

   Directory containing the run results (parent of campaign directories).
   When omitted the value configured with ``vast init`` is used.

.. option:: -f, --force

   Bypass the postprocessing cache and re-run all commands even if the
   results directory has not changed since the last postprocessing run.

.. option:: -o, --override VAST_FILE

   Use the given ``.vast`` file instead of the one stored in
   ``<campaign-name>-<timestamp>/_config/``.  See :ref:`results-override` for details.

Postprocessing is **cached** by a hash of the results directory.  When the
directory is unchanged the step is skipped automatically.  Use ``--force`` (or
``-f``) to bypass the cache, for example after updating a postprocessing script:

.. code-block:: bash

   vast results postprocess --force


.. _results-retrigger:

Re-running a finished campaign's post-run steps
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The two steps that run *after* a campaign's scenarios finish — analysis
**postprocessing** and the **upload-to-share** — can each be re-triggered on a
finished campaign, and each works **from the stored campaign alone**: no live
campaign process is required, so a re-trigger is available even after the
``robovast-service`` (``vast serve``) was restarted. Under the web UI's *Monitor*
each finished campaign's actions menu offers *Retrigger postprocessing* and
*Retrigger upload-to-share*; the same operations are exposed as the MCP tools
``run_postprocessing`` and ``run_share``.

A re-trigger through the service is **dispatched in the background and returns
immediately** — postprocessing can take minutes to hours, so the campaign simply
re-enters the ``postprocessing`` (or ``sharing``) phase and you follow its progress and
log in the campaign view, exactly like the original run; it returns to ``finished`` when
done. The web *Retrigger postprocessing* dialog therefore closes as soon as you click
*Run*. A second re-trigger is refused while one is already running. (The
``vast results postprocess`` CLI is the exception: it runs postprocessing locally and
synchronously, streaming to the console.)

Because a post-run step is separate from the runs themselves, a **failure of one of
these steps does not fail the campaign**. The campaign stays ``finished`` (its runs
are the deliverable and remain downloadable) and the failure is recorded on its own
field — ``postprocessing_error`` or ``share_error`` — which is durable (it survives a
service restart) and is surfaced as a warning badge in the UI. Re-running the step
successfully clears it. This is distinct from a *run* failure, which reports the
campaign phase as ``failed``.

Editing the postprocessing parameters before re-running **overwrites the
``results_processing.postprocessing`` block of the campaign's own ``_config/<name>.vast``
in place** — it is config, not captured data, so there are no override files or
revisions, and the raw rosbags and the as-ran ``configuration``/``execution`` are left
untouched. The edited config applies on both the local and the cluster backend. For the
**upload-to-share**, the target provider is taken from the service environment
(``ROBOVAST_SHARE_TYPE`` and its credentials); adjust it and re-trigger to upload the
same campaign to a different provider.

Custom postprocessing plugins that need third-party Python packages — an
entry-point postprocessing command, or the dependencies a local
``./file.py:Class`` plugin imports — declare those packages in the ``.vast``'s
top-level ``plugins:`` list (see :ref:`configuration`). They are installed into the
campaign's ``.robovast_plugins/`` and put on ``sys.path`` before postprocessing
runs, including on a re-run in a fresh process.


.. _results-publish:

Publishing Results
------------------

Publication packages or distributes the results directory using plugins defined
in the ``results_processing.publication`` section of the ``.vast`` file.  Unlike
postprocessing (which operates per campaign run folder), publication plugins
receive the full results directory as input and are intended for tasks like
creating zip archives for upload or hand-off.

.. code-block:: bash

   vast results publish [OPTIONS]

**Options**

.. option:: -r, --results-dir PATH

   Directory containing the run results (parent of campaign directories).
   When omitted the value configured with ``vast init`` is used.

.. option:: -o, --override VAST_FILE

   Use the given ``.vast`` file instead of the one stored in
   ``<campaign-name>-<timestamp>/_config/``.

.. option:: -f, --force

   Overwrite existing output files (e.g. zip archives) without prompting.
   Equivalent to setting ``overwrite: true`` on every publication plugin.
   Without this flag, plugins that find an existing output file will ask the
   user interactively (default answer: yes / overwrite).

**Example:**

.. code-block:: bash

   # Publish using the project-configured results directory
   vast results publish

   # Publish and overwrite any existing archives without prompting
   vast results publish --force

   # Publish a specific results directory with an override config
   vast results publish --results-dir /path/to/results --override my_project.vast


.. _results-publication-plugins:

Listing Publication Plugins
---------------------------

.. code-block:: bash

   vast results publish-commands

Lists all available publication plugins, their descriptions, and parameters.
Useful for discovering which plugins can be used in the
``results_processing.publication`` section of the ``.vast`` file.


.. _results-merge:

Merging Results
---------------

.. code-block:: bash

   vast results merge-campaigns MERGED_CAMPAIGN_DIR [OPTIONS]

Merges campaign-directories with identical configs into one ``merged_campaign_dir``.
Groups ``campaign-directory/config-directory`` by ``config_identifier`` from ``config.yaml``.
Run folders (0, 1, 2, …) from all campaigns are renumbered and copied.
Original campaign-directories are not modified.

**Arguments**

``MERGED_CAMPAIGN_DIR``
   Target directory where the merged campaign will be written.

**Options**

.. option:: -r, --results-dir PATH

   Source directory containing campaign directories.  When omitted the value
   configured with ``vast init`` is used.


.. _results-postprocess-commands:

Listing Postprocessing Plugins
-------------------------------

.. code-block:: bash

   vast results postprocess-commands

Lists all available postprocessing command plugins, their descriptions, and
parameters.  Useful for discovering which commands can be used in the
``results_processing.postprocessing`` section of the ``.vast`` file.


.. _results-override:

Using ``--override`` to Supply a Local ``.vast`` File
------------------------------------------------------

By default ``vast results postprocess`` reads the ``.vast`` configuration from the
**campaign snapshot** stored in
``<results-dir>/<campaign-name>-<timestamp>/_config/<name>.vast``.  This snapshot is copied
at execution time and may be out of date.

``--override`` (short form ``-o``) lets you point to any ``.vast`` file on disk,
for example your current working copy:

.. code-block:: bash

   # Use a local/updated .vast file
   vast results postprocess --override my_project.vast

**When to use ``--override``**

- You want to apply updated postprocessing scripts to existing results without
  triggering a new execution campaign.
- The results were produced in a different directory and the campaign snapshot
  points to stale paths.
- You want to bypass the snapshot and always use the latest ``.vast`` during
  iterative postprocessing development.

.. note::

   When ``--override`` is supplied, the same ``.vast`` file is used for
   **every** campaign folder found under the results directory.  The
   config directory of the override file (its parent folder) is used to
   resolve relative paths.
