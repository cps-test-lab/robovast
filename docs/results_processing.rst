.. _results-processing:

Results Processing
==================

**A campaign's directory is its database.** Everything a campaign recorded — its rosbags, its
containers' logs, its resource samples, the files its runs wrote, and ``campaign.db`` — stays in
that directory as the record of what happened, and every table anybody queries is built from
those records. The tables themselves are a cache beside them (``<campaign>/.cache/``): deleting
it loses nothing but the time to build them again.

This page describes what a campaign records, which tables its records give and how each is
built, how a campaign's ``.vast`` shapes them, what runs when a campaign ends, and how to query
the result — from the web UI, an agent, or a notebook on your own machine.


.. _results-output-structure:

Output Structure
----------------

The results directory is named with ``--results-dir``: on the serve command the
deployment runs for where campaigns land, and on each ``vast results`` verb for which
tree to read.

Top-Level Layout
^^^^^^^^^^^^^^^^

.. code-block:: text

   <results-dir>/
   └── <campaign-name>-<timestamp>/          # One per execution (e.g. dynamic_obstacle-2026-03-04-152130)
       ├── campaign.db                       # The campaign's store: units, runs, jobs, batches
       ├── metadata.yaml                     # Campaign metadata (written by postprocessing)
       ├── _config/                          # Campaign-level configuration snapshot
       ├── _execution/                       # Execution metadata and phase logs
       ├── _transient/                       # Resolved configurations and generated scripts
       ├── _jobs/                            # Per-job artifacts (sysinfo, resource usage, logs)
       ├── .cache/                           # Built tables; rebuilt from the records on demand
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

Doing it by hand instead pushes the snapshot as a workspace and runs that:

.. code-block:: text

   vast workspace init <campaign-dir>/_config --name replay
   vast workspace run replay <config-name>.vast

That path **rebuilds** the image rather than reusing the one the campaign recorded, so it needs the
sources the ``build:`` section names — which are *not* archived here. It is the escape hatch for when
the recorded image is gone; otherwise prefer the retrigger, which reuses the exact bytes.
All three launches are gated by the same pre-flight over these records; see
:ref:`results-retrigger-preflight`.

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
   ├── tables.yaml                           # The decoder's configuration (see Configuring the decoder)
   ├── plugin_install.log                    # ``plugin install`` phase (pip output; only when plugins are declared)
   ├── variation.log                         # ``variation`` phase (config-variation expansion)
   ├── controller.log                        # ``run`` phase — campaign controller log
   ├── postprocessing.log                    # ``postprocessing`` phase (steps, campaign-end pass)
   ├── tables.log                            # a table build asked for ahead of use
   ├── share.log                             # ``share`` phase (export to share, when re-run)
   ├── sections/                             # earlier runs of the repeatable phases
   ├── import.log                            # ``importing`` phase (only on an imported campaign)
   └── import.json                           # per-stage import report (only on an imported campaign)

Each pre-/post-run **phase** writes its own log file here; the service concatenates
them in phase order into the single live campaign log the web UI streams. The
``plugin install`` phase (present only when the ``.vast`` declares ``plugins:``) runs
first and captures the ``pip install`` output live, exactly like ``building``,
``variation`` and ``postprocessing``.

**Postprocessing, the upload to the share and a table build can each run again**, any number of
times and in any order, so each has its own section: the campaign log's ``POSTPROCESSING``,
``SHARE`` and ``TABLES``. A run of one starts an empty file, and the finished one is kept under
``_execution/sections/``, numbered in the order the runs happened — so the campaign log shows
what the step you just asked for did, after what the earlier ones did.

``import.log`` is the same idea for the one phase that can *precede* everything else:
a campaign taken in from an archive or the share writes it while the bytes are still
arriving — which is why the campaign's ``_execution/`` directory is created before the
extraction rather than by it, so the import's account of itself covers the download too.

``execution.yaml`` contains:

- ``execution_time``: ISO timestamp of when the execution started
- ``robovast_version``: Git commit hash of the robovast version used
- ``runs``: Number of runs per configuration
- ``execution_type``: ``cluster`` or ``local``
- ``image``: the configured execution image reference (may be a floating tag such as
  ``…:latest``)
- ``image_revision``: the **immutable digest** the run pods actually used
  (``repo@sha256:…``), captured at run time on the cluster backend, which is what a retrigger
  starts the new campaign from.
- ``cluster_info``: Node count, labels, CPU manager policies (cluster only)

.. _campaign-launch-record:

``launch.yaml`` records the **request**, where ``execution.yaml`` records what happened:
``config_filter``, ``campaign_name``, ``runs`` (as *requested*), ``postprocess``,
``upload_to_share`` and ``backend``. It answers "was this the full sweep or a one-config
pilot?" about a finished campaign, for a person and for a retrigger — ``config_filter`` in
particular is consumed during config expansion and recorded nowhere else.

Read the two together for ``runs``: ``launch.yaml``'s ``runs: 0`` means "take the ``.vast``'s
``execution.runs``", so ``0`` beside ``execution.yaml``'s ``runs: 3`` says the ``.vast`` asked for 3,
while ``1`` beside ``1`` on a ``.vast`` declaring 3 says someone piloted it. ``metadata.yaml`` nests
this under ``execution.launch`` so a published campaign is one document. It is written by the service
before the run starts, which is why it is a separate file: ``execution.yaml`` is written *by* the run,
and a campaign that fails before its first batch would otherwise have no record of what it was asked
to do. A campaign without the file has no launch record.

``controller.log`` captures the campaign controller's own log for the whole run —
batch/search progress, backend job dispatch and stopping decisions. For cluster runs the
``robovast-service`` writes this as it drives the campaign (and the web UI streams it live);
it is preserved in the campaign so a downloaded or shared campaign is self-documenting without a
live cluster.

``_transient/`` — Resolved Configuration
"""""""""""""""""""""""""""""""""""""""""

.. code-block:: text

   _transient/
   ├── configurations.yaml                   # Fully resolved configuration parameters
   ├── job_links.yaml                        # Which job each run belongs to
   ├── postprocessing.yaml                   # Provenance record, written last by postprocessing
   ├── entrypoint.sh                         # Generated container entrypoint script
   ├── secondary_entrypoint.sh               # Generated secondary container entrypoint script
   └── collect_sysinfo.py                    # System info collection script

``configurations.yaml`` contains the fully resolved parameter values for every
configuration variant, including internal computed fields like navigation path waypoints
(``_path``), raster points (``_raster_points``), resolved file paths, and
``_variations`` (list of applied variation plugins with name, start time, duration,
and any plugin-specific fields).

``job_links.yaml`` maps each ``<config>/<run>/job`` to its job directory. It is written before
the first job starts — the ``job`` symlink beside a run appears only once its batch ends — so it
is what the decoder resolves a run's job through, including for a run still going.

``postprocessing.yaml`` names what each postprocessing step wrote and from what. It is written
**last**, and its presence is what marks a campaign as postprocessed (see
:ref:`results-postprocessing`).

Configuration Directory
^^^^^^^^^^^^^^^^^^^^^^^

Each configuration variant gets its own directory:

.. code-block:: text

   <config-name>/
   ├── _config/
   │   ├── config.yaml                       # Configuration identifier hashes
   │   ├── scenario.config                   # Resolved scenario parameters (YAML)
   │   ├── sim.config                        # Resolved sim block [if the config has one]
   │   ├── sut.config                        # Resolved sut block [if the config has one]
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

One file per variation channel, and each holds what that channel resolved to for this
configuration: ``scenario.config`` the parameters the scenario file declares, ``sim.config``
the simulator's whole resolved block, ``sut.config`` the flat ``<source>.<path>: value``
block the system under test was configured with. ``config.yaml`` holds none of them — it carries
only the identifier hashes that group configurations across campaigns.

The last two are **records**, not inputs: what a run reads is the mounted overrides file
(``sim``) and the rewritten config copies (``sut``). They sit beside the configuration so
that what each channel was given is readable without diffing two copies of a stack's
configuration. The campaign-level ``_transient/configurations.yaml`` carries the same values
for every configuration in one document, and every varied factor is a column of the ``runs``
table whichever channel it was written on — see :ref:`channel-param-columns`.

Run Directory
^^^^^^^^^^^^^

Each run directory holds the **scenario output** for one configuration at one run
number, plus a ``job`` symlink to that run's job-level artifacts:

.. code-block:: text

   <run-number>/
   ├── test.xml                              # JUnit test result (pass/fail, duration)
   ├── behaviors.jsonl                       # scenario-execution's behaviour-tree log
   ├── rosbag2/                              # the run's bag, recorded by RoboVAST [ROS mode]
   ├── job -> ../../_jobs/job-N              # symlink to this run's job artifacts (see below)
   └── <test-specific files>                 # Domain-specific output, e.g. out.csv

Anything the *scenario* itself produces (``test.xml``, ``behaviors.jsonl``, domain output) and
the run's own ``rosbag2/`` stay in the run directory. Infrastructure and monitoring artifacts
(``sysinfo.yaml``, ``resource_usage_*.csv``, the containers' logs, and the entrypoint's
``/rosout`` + ``/clock`` recording) belong to the **job** and live under ``_jobs/job-N/`` —
reachable via the ``job`` link, e.g. ``<run>/job/sysinfo.yaml`` (see :ref:`job-directory`). The
links are made when a batch ends, so the runs of a batch that was stopped have none; which job
each run belongs to is recorded in ``_transient/job_links.yaml`` from before the first job
starts, and that is what RoboVAST resolves a run's job through.

**Every** ``*.csv`` **and** ``*.jsonl`` **file below a run directory is a table**, named after
the file (see :ref:`results-tables`). Nothing derived is written back here: the tables built
from a run's recording live in the campaign's ``.cache/``. The one exception is a video, which
the decoder encodes into the run directory because a browser plays it from there (see
:ref:`the videos table <videos-table>`).

``rosbag2/`` is the run's bag, and RoboVAST records it: the job's entrypoint starts
``ros2 bag record`` before the scenario and stops it after, holding what the ``.vast``'s
:ref:`recording: <recording-config>` block says (everything, by default). It is a standard
ROS 2 bag in MCAP storage, written **through** rather than cached and split into a new segment
every 10 s, so a running run's bag is readable as it grows: a closed segment is read whole, the
open one up to its last complete record. The storage plugin flushes in 4 KiB steps, so the newest messages
of a quiet topic wait at most until the split closes the segment. ``metadata.yaml``, listing the
recorded topics and message counts, appears only when the recorder stops -- a bag is complete
exactly when it is closed. It is distinct from the job-level ``/rosout`` recording under
``_jobs/job-N/logs/``.

A write-through recorder's I/O pattern differs from a cached one's -- more, smaller writes
while the run is going instead of a burst at the end. A metric sensitive to the recorder's
own load (a control-loop rate, a latency measured on the recording host) is therefore not
compared across that boundary without saying so.

Every bag directory also holds ``message_definitions.json``: the full definition of each type
it recorded, written by the run's own container at its end, where the types are installed.
rosbag2 embeds most definitions in the recording itself but none for an action-derived type
(an action's ``_FeedbackMessage``), so this file is what lets such a topic be decoded where the
system under test is not installed — by ``robovast-decode``, on any machine with Python. A type
neither the recording, this file nor the ROS distribution's standard set defines is reported
in the ``_recording`` table with that reason rather than skipped.

.. _job-directory:

Job Directory
^^^^^^^^^^^^^

``_jobs/job-N/`` holds the artifacts of one *job* — the unit of dispatch (one
Kubernetes Job, or one local ``docker compose`` run). There is one job per run,
and each run links to its job via ``<run>/job`` (e.g. ``<run>/job/sysinfo.yaml``).

.. code-block:: text

   _jobs/job-N/
   ├── sysinfo.yaml                          # Hardware info (platform, CPU, memory) — stable
   ├── resource_usage_main.csv               # Main container CPU/memory over the job
   ├── resource_usage_<secondary>.csv        # Per secondary container [if multi-container]
   ├── system_usage_main.csv                 # Main container cgroup counters over the job
   ├── system_usage_<secondary>.csv          # Per secondary container [if multi-container]
   └── logs/
       ├── system.log                        # Main container system log
       ├── system_<secondary>.log            # Secondary container log [if multi-container]
       └── rosout_bag/                       # /rosout and /clock recording, wall time [ROS mode]

These are the sources of the **derived tables** (:ref:`results-derived-tables`): the logs and
the ``rosout_bag/`` recording give ``run_log`` and ``scenario_timestamps``, the
``resource_usage_*.csv`` files ``resource_usage``, the ``system_usage_*.csv`` files
``system_usage``. They span the whole job, bring-up and teardown included, and the derivation
marks what falls inside the run's trial.

``resource_usage_*.csv`` files have columns ``timestamp`` (wall epoch seconds), ``pid``,
``name``, ``cpu_percent``, ``memory_rss_bytes``, ``shm_used_bytes`` and ``shm_total_bytes``,
one row per process per ~1 s, one file per container. Both files span every *instance* of the
container as well: a container the kubelet restarts runs the sampler again against the same
file, which continues the record under its one header rather than replacing it, so the samples
of the instance that died — the high-water mark climbing towards its limit — are kept beside the
ones of the instance that came after. The seam shows as the cumulative counters going backwards:
the calibration reader drops that tick as a cgroup replaced, and a run whose container crashed is
``invalid`` in the intervention ledger (:doc:`architecture`), so its per-run aggregates are never
compared with a run that kept one instance throughout.


.. _results-tables:

Tables
------

A table is a set of parquet files under ``<campaign>/.cache/tables/``, one per run, cataloged by
``.cache/MANIFEST.json``. Every
table carries ``campaign_id``, ``config_name`` and ``run_id`` in its own rows, so it joins to
``runs`` and to every other table on ``(config_name, run_id)``.

Where the tables come from
^^^^^^^^^^^^^^^^^^^^^^^^^^

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Kind
     - Tables
     - Built from
   * - **Bag tables**
     - ``poses``, ``nav2_behavior_tree``, ``costmaps``, ``action_<name>_feedback`` /
       ``_status``, ``rosbag2_<topic>``, ``videos``
     - a run's own recording, ``<run>/rosbag2/``
   * - Infrastructure recording
     - ``rosout``, ``clock_map``
     - the job's wall-time recording, ``_jobs/…/logs/rosout_bag/``
   * - Simulator recording
     - ``sim_poses``, ``joint_states``, ``clock_map``, ``sim_recording``, ``sim_entities``
     - roqsim's own recording, ``<run>/roqsim_bag/roqsim.mcap``, beside the scenario recording
       or, for a stepped run, instead of it
   * - **Derived tables**
     - ``run_log``, ``scenario_timestamps``, ``resource_usage``, ``system_usage``,
       ``run_clock``
     - a job's container logs, resource samples and infrastructure recording, cut to each run
   * - **Authored files**
     - one per file stem: ``behaviors``, ``nav2_behaviors``, a scenario's ``out``, …
     - every ``*.csv`` and ``*.jsonl`` below a run directory
   * - ``_recording``
     - one
     - every recording of the run: what it holds, and what is not a table and why
   * - Campaign tables
     - ``run_health``, ``postprocessing_steps``
     - written once for the whole campaign by its campaign-end pass
       (:ref:`results-campaign-end`)
   * - The campaign's record
     - ``runs``; the ``campaign`` schema (``campaign.campaign``, ``campaign.unit``,
       ``campaign.run``, ``campaign.job``, ``campaign.batch``, ``campaign.node``,
       ``campaign.container_failure``)
     - ``campaign.db``, read whole on every query — never cached

**Bag tables.** Every recorded topic is a table unless there is a reason it is not. A campaign
that configures nothing gets, from its scenario recording:

* ``/tf`` + ``/tf_static`` → ``poses``, every frame that resolves against ``map``
  (:ref:`pose-contract`);
* ``/behavior_tree_log`` → ``nav2_behavior_tree``, one row per status change
  (``timestamp, node_name, uid, previous_status, current_status, event_timestamp``);
* every ``nav_msgs/msg/OccupancyGrid`` topic → ``costmaps``, one row per grid with its geometry
  and its int8 cells zlib-compressed and base64-encoded, a ``topic`` column keeping the layers
  apart;
* every action's ``/<name>/_action/feedback`` and ``status`` → ``action_<name>_feedback`` and
  ``action_<name>_status``, flattened;
* every other topic → ``rosbag2_<topic>`` (``/collision`` → ``rosbag2_collision``), one row per
  message with its fields as columns — nested fields joined with ``.``, a numeric array as one
  encoded cell — and ``timestamp`` in nanoseconds.

Not tabulated, with the reason in ``_recording``: images, compressed images, point clouds and
laser scans, which are bulk data read from the recording where they are wanted; the scenario
recording's ``/clock`` and ``/rosout``, which come from the infrastructure recording; and
``/parameter_events``. The ``videos`` table exists only where the campaign asks for it
(``rosbags_to_webm``, see :ref:`results-decoder-config`), because encoding a camera costs far more
than tabulating it.

A recording is read once per build, and only the messages some requested table needs are
deserialized; custom message types decode from the definitions embedded in the bag and its
``message_definitions.json``, so no ROS installation is involved.

**Simulator recording.** roqsim records a run as one mcap, ``<run>/roqsim_bag/roqsim.mcap``,
whose channels are JSON documents sampled at the capture rate and whose provenance travels as
mcap metadata. It is decoded like a bag, and is the only recording a **stepped** (ROS-less)
run has:

* ``poses`` → ``sim_poses``, one row per named body per sample, in the :ref:`pose contract
  <pose-contract>`'s simulator shape: ``timestamp`` (exact sim seconds), ``wall_time``,
  ``frame``, position, quaternion ``(x, y, z, w)`` and world-frame twist;
* ``joints`` → ``joint_states``: ``timestamp``, ``wall_time``, ``joint``, ``position``, one row
  per hinge or slide joint per sample;
* ``clock`` → ``clock_map``, the same ``(wall_ts, sim_ts)`` samples the infrastructure recording
  gives, so ``run_clock`` and the derived tables work unchanged for a stepped run. A run whose
  job also has the infrastructure recording's ``/clock`` keeps that one, and the channel is
  listed in ``_recording`` with that reason;
* the ``roqsim.recording`` metadata → ``sim_recording``, one row per run: ``format_version``,
  ``world``, ``overrides_json``, ``seed``, ``packages_json``, ``capture_fps`` (``num/den``),
  ``timestep``, ``model_json`` -- the last record of the name wins;
* the ``roqsim.entities`` metadata → ``sim_entities``: ``name``, ``kind``, ``body``,
  ``present``, the rows of the last roster the run wrote.

The ``state`` channel, the raw simulator state roqsim itself reads back, is not a table and says
so in ``_recording``. The recording is closed once its writer wrote the footer -- a killed run's
file ends where its last chunk did, and is decoded to there.

**Authored files.** A run's own data file is a table with no registration: a scenario's metrics
file, a simulator's pose stream, a postprocessing step's output, scenario-execution's
``behaviors.jsonl``. The table is the file's stem, lower-cased, with anything but ``[a-z0-9_]``
turned into ``_``. Column types are inferred from the values; a ``#`` preamble before a CSV's
header is skipped; a table carrying a quaternion gains ``orientation.yaw``. Refused, for that
table and run with the reason: two files claiming one table, a CSV row with more fields than its
header, and a file whose name is a table the run's records already give (rename the file).

.. _results-recording-table:

**The** ``_recording`` **table** is one row per topic of every recording of the run —
``recording``, ``topic``, ``type``, ``messages``, ``bytes``, and either the ``table`` it went to
or the ``reason`` it is not one. An undecodable topic is a row here naming its type and why,
rather than a table that is silently missing::

   SELECT topic, type, reason FROM _recording WHERE reason IS NOT NULL

.. _results-table-cache:

Built on first use, and kept
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

**A table is built for a run the first time something names it.** Something names it when:

* a SQL query reads it — the tables its statement names, and the tables any view it names
  reads, built for the runs in scope. A top-level ``WHERE`` that restricts a table to some runs
  with ``config_name = …`` / ``run_id = …`` or ``IN (…)`` narrows the build to those runs;
* a notebook or script calls ``table()`` (:ref:`results-notebooks`);
* a run-view panel or an endpoint reads it;
* the campaign ends, and its campaign-end pass builds what it declares
  (:ref:`results-campaign-end`).

The build is done by ``robovast-decode``, a pure-Python distribution (mcap, rosbags, pyarrow):
no ROS install, no execution image, no container. The service, a notebook on a laptop and the
``robovast-decode`` command line build the same rows from the same records.

``.cache/MANIFEST.json`` records, per table and run, the files that make it up, their schema,
the source bytes they were built from, the decoder version that built them — and, for a run that
has no rows for a table, the reason. A later request builds only what is missing: a table not yet
asked for, a run whose records have grown, or anything a different decoder version wrote. A run
that has not finished — no ``test.xml`` yet, or a recording still open — is looked at again on
the next request, so **SQL works while a campaign is running** and follows it as it goes.

A run being followed *as it records* (:mod:`robovast_decode.live`) is the exception: a session
reads each new record of the growing bag, flushes the handlers' rows in batches, and writes them
as parquet **parts** the manifest names under the run's entry with a ``live`` stamp. A query reads
every part written so far and leaves the entry to the session while the stamp is fresh; once the
run has its verdict and the recorder closed the bag, the parts are merged into the run's one
file and the entry is complete. A stamp that has gone stale belongs to a session that died, and
the table is built whole from the recording like any other. The :ref:`derived tables
<results-derived-tables>` of a run being followed are not read in parts: they are derived
again, whole, from the job's files as they are, on the same period, written with the same
``live`` stamp, and recorded as a build records them once the run has its verdict and its
recordings are closed.

**The cache is disposable.** Clearing it (:ref:`results-tables-ahead`) loses nothing but the time
to rebuild; archives and downloads leave it out, and an imported or downloaded campaign builds its
tables from its records the first time they are named.

.. _run-clock:

One clock per run
^^^^^^^^^^^^^^^^^

**Every table built from a recording timestamps its rows with the bag's receive time.** Under
``recording.ros2.use_sim_time: true`` that is the *simulator's* clock, and it is the right one to key
on: it is what the simulator actually stepped, so it is identical across simulators and independent
of how fast the machine ran — the same trial can take very different wall times on two backends
and still span the same sim seconds.

That shared clock is what lets the poses, the costmaps and the behaviour trees be read against
each other at all, and it is the timeline the web :ref:`Run view <run-view>` scrubs. So a topic
that carries a **clock of its own** keeps its original stamp in a separately-named column rather
than using it as ``timestamp``. nav2's ``/behavior_tree_log`` is one: nav2 stamps its events from
a wall clock even under ``use_sim_time``, so ``nav2_behavior_tree`` is keyed on the receive time
and carries nav2's stamp as ``event_timestamp``.

Seconds everywhere, except in a topic's own ``rosbag2_<topic>`` table, whose ``timestamp`` is in
nanoseconds.

Keeping ``/clock`` in the run's bag -- it is there unless ``recording.ros2`` leaves it out -- is
worth the negligible space: it makes the sim↔wall mapping recoverable, so a foreign-clock topic can
be *related* to sim time afterwards instead of guessed at.

.. _pose-contract:

The pose contract
^^^^^^^^^^^^^^^^^

A pose table answers one question — *where was this thing, and when* — and more than one producer
can answer it. ``poses`` comes from ``/tf`` in a rosbag; ``sim_poses`` is decoded from the
simulator's own recording, taken inside the simulator during the run, and is the only pose data a
**stepped** (non-ROS) run has, since there is no bag to derive anything from. A stack on some
other middleware, a motion-capture ingest, or a real-robot log joins them by writing a file with
the same columns into the run directory; nothing has to be registered, because every data file
of a run is a table named after the file.

One table per producer, sharing the schema. That keeps provenance 1:1 and stops a panel
filtering ``frame: base_link`` from silently plotting two interleaved series; a query that wants
both writes one ``UNION ALL``.

.. list-table::
   :header-rows: 1
   :widths: 26 74

   * - Column
     - Meaning
   * - ``frame``
     - The named entity, in the producer's own vocabulary: a TF child frame, a MuJoCo body, a
       motion-capture rigid body.
   * - ``timestamp``
     - The join key described above, and never re-keyed. What it *measures* depends on the
       producer: **arrival** time for a table built from a transport, which no derivative may
       be taken from; the **exact simulated** time for one the simulator wrote itself.
   * - ``stamp``
     - **Measurement** time: when the pose was true, from the producer itself. NULL when it cannot
       state one (a latched ``/tf_static`` transform). Present only where ``timestamp`` is an
       arrival time — a producer whose ``timestamp`` is already the measurement omits the column
       rather than duplicating it.
   * - ``wall_time``
     - Unix epoch seconds for the same sample, where the producer can state one. Not a pose clock:
       it exists to join this table to what is stamped in wall time (``run_log``,
       ``resource_usage``) on a run with no rosbag to relate them otherwise, and it advances with
       the host rather than with the simulation.
   * - ``position.x/y/z``
     - Meters.
   * - ``orientation.x/y/z/w``
     - Quaternion, and the only attitude a producer emits.
   * - ``orientation.yaw``
     - Derived when the table is built; see below.
   * - ``twist.linear.*``, ``twist.angular.*``
     - World-frame velocity, empty when the producer cannot know it (TF carries none).

**World coordinates, as an invariant rather than a column.** Every row is in the run's single
global frame, so there is no ``reference_frame`` to read or to get wrong. The TF handler resolves
every frame against ``map`` and fails loudly when a required frame yields no map-relative pose,
the ROS launches make ``map`` identical to the simulator's world by an identity edge, and
MuJoCo's ``xpos``/``xquat`` are world-frame by construction. A producer that cannot express world
coordinates does not satisfy the contract.

**Difference** ``stamp``, **join** ``timestamp``. Arrival time is only as fine as the ``/clock``
grid the recorder's own clock advances on, and is jittered by delivery on top — neither of which
is the interval the robot moved over. The failure is not subtle and does not look like noise: a
pose published at a period the ``/clock`` grid does not divide arrives at alternating intervals,
so a robot at constant speed reads as alternating between two speeds while the displacement
between samples is identical. Making the grid divide the period removes that systematic alias
but not the delivery jitter; only ``stamp`` removes both. ``calculate_speeds_from_poses`` picks
the base for you and reports which it used in ``time_base``, so a comparison can assert both
sides took their derivative from the same column. Read that column together with the table it
came from: ``time_base: timestamp`` is the *exact* base on a simulator-written table and the
*degraded* one on a transport-derived table that carries no ``stamp``, and the two are not
comparable despite the identical label.

**Every table that follows the contract gets a track summary,** ``pose_track_view``. One row per
track, meaning one ``frame`` of one run as one table recorded it: ``points``, ``length_m``,
``duration_s``, ``avg_speed_m_s``, ``max_speed_m_s``, ``max_step_m``, start and end pose, and the
bounding box. It is computed over every recorded pose, on each table's measurement clock
(``stamp`` where the table has one, else ``timestamp``), with ``position.z`` in the length where
the table carries it. ``source`` names the table, since the same entity recorded by two producers
is two tracks. A sample with no measurement time, such as a latched ``/tf_static`` transform, is
not a point on a track. A reposition counts as travel: a body spawned at the origin and then
placed at its start pose adds that jump to ``length_m``, and ``max_step_m`` (the largest single
step) is where it shows. The view covers every pose-contract table of the campaign, so a new
producer's table appears in it without being registered.

.. code-block:: sql

   SELECT source, frame, points, length_m, duration_s, max_speed_m_s
   FROM pose_track_view
   WHERE config_name = '<config>' AND run_id = 0;

**Quaternion in, yaw out.** Producers emit a quaternion and nothing else: roll/pitch/yaw is lossy
the moment a body pitches or rolls, which rules out a drone, a tilting arm, or a robot on a ramp.
The build then derives ``orientation.yaw`` for any table that has the quaternion columns and no
yaw, because the 2D consumers — the costmap panel's heading marker, the nav MCP tools, the
notebooks — all want a heading and none of them should reimplement quaternion math in SQL,
JavaScript and pandas separately. It is a projection: correct for a body in the plane, and the
quaternion is the one to read for a body that has left it.

.. _clock-map:

Wall → sim: the clock map
^^^^^^^^^^^^^^^^^^^^^^^^^

Everything a run *logs* is stamped in wall time — rosout's receive time, and whatever each
container printed. Everything it is *analyzed* on is sim seconds. Relating the two is a per-run
**clock map**: a list of ``(wall_ts, sim_ts)`` samples, interpolated piecewise-linearly.

**A single offset is wrong**, which is why this is a sampled map and not a number. A simulator
running faster or slower than the wall clock drifts away from an offset taken at the start, and
sim time can also pause.

Two producers, one format:

* **ROS** — the *entrypoint's own* recorder (``/rosout`` and ``/clock``) writes a bag in **wall**
  time for the whole container's life, so each ``/clock`` message is an exact (wall receive, sim
  content) pair; its ``clock_map`` table holds the samples. Deliberately not the run's own bag:
  under ``use_sim_time`` that one is sim-time on both axes, so it cannot carry the mapping, and it
  is recorded for single-run jobs only.
* **roqsim (non-ROS)** — the ``clock`` channel of its own recording, one sample per capture
  step, in a file that is flushed at least once a second so a run killed by a timeout still has
  its map up to the last closed chunk.

The samples are **decimated**: one is dropped only when linear interpolation reproduces it within
5 ms, so a steady stretch costs two rows while a pause or a change of real-time factor keeps
exactly the samples that describe it.

**Outside the sampled range there is no answer, and none is invented.** The samples begin when the
simulator started publishing its clock, typically well after the container did, so a line logged
during image boot has no sim time — a different statement from "we could not compute it". The
``run_clock`` table says, per run, which producer answered (``clock_map_source``:
``ros_clock_bag`` / ``roqsim`` / ``none``), how many samples it has and the wall and sim
spans they cover, so a reader can tell a *missing* map from a *quiet* one;
``clock_map_sim_span_s / clock_map_wall_span_s`` is the run's realtime factor.

.. _results-derived-tables:

Derived tables
^^^^^^^^^^^^^^

A job's container logs, its resource samples and its infrastructure recording are written once
per job, and a job runs one run. The derived tables are built from them for that run, on its
clock and marked against its trial window. They are built for every run of every campaign; no
configuration asks for them. A campaign whose job-link manifest points several runs at one job
is refused rather than read: which of them a job's row belongs to cannot be read off the
records.

.. _merged-run-log:

``run_log`` — everything the run said
"""""""""""""""""""""""""""""""""""""""

One row per log **event**, across every container, on the run's own playback clock.

It is a **join**, not a concatenation, because a run's output arrives twice: a launch container
forwards its nodes' output to stdout *and* those nodes publish ``/rosout``, so most rosout rows
are the same event as a ``system*.log`` line, and appending both would report most of the run
twice. Matching is exact on ``(node, wall_ns, first line of message)``, keyed on the
**producer's** stamp — the bag's *receive* time puts every pair on a different nanosecond.

The join also supplies what neither source has alone: ``/rosout`` names the node but never the
*container* it ran in, and that is what a reader filters by. It comes from the file the stdout
twin was found in.

A run whose job artifacts cannot be located gets no rows, and the build says so;
``get_job_log`` is the whole-container view.

Columns: ``seq``, ``sim_time``, ``wall_ts``, ``time_source``, ``in_window``, ``container``,
``node``, ``source``, ``level``, ``severity``, ``message``, ``file``, ``function``, ``line``.

* ``seq`` — the merge's own order within the run: ``ORDER BY seq``, never by storage order.
* ``source`` — ``rosout`` or ``stdout``. ``WHERE source = 'rosout'`` is the rosout slice.
* ``time_source`` — how the row got its wall time: ``stamp`` (the producer's own, ns precision),
  ``inherited`` (a continuation line, e.g. a traceback frame, taking the time of the event it
  belongs to), or ``none`` (nothing stamped anywhere before it).

  **Producers stamp their own lines**, which is why ``stamp`` is the normal case rather than a
  ROS-only luxury: rclpy writes the stamp, and so do the entrypoints' ``log`` helper and
  scenario-execution's logger. Every line of a container's log therefore carries a prefix, the
  infrastructure's own included: ``[INFO] [1786264427.117714] [entrypoint]: Running as UID:
  1000``.

  Third-party output that stamps nothing (a gz warning, a vanilla sidecar) is **never dropped**:
  it gets a row per line, inheriting from a neighboring event where one exists and reporting
  ``none`` where it does not. An untimed row is honest about being untimed; it is deliberately
  not backfilled from the next stamp, which would render exactly like a real time and claim the
  container booted at whatever second the first node came up.
* ``in_window`` — 0 for a line outside this run's own wall window: its bring-up, its verdict, its
  teardown. Real output, kept rather than dropped, and
  flagged so a query can tell "during the trial" from "getting ready for it" and "cleaning up
  after it".

  It is **not** the boundary of the trial, and must not be used as one. A run's ``test.xml``
  duration closes when its scenario stops, but the verdict line can be logged just after that,
  so filtering ``in_window = 1`` can drop the verdict of a failing run. Where the trial ended is
  :ref:`scenario_timestamps <scenario-verdict>`.
* ``severity`` — the same definition the status verdict and the MCP log tools use. The table
  holds every line; filter by severity where it is read (every log surface takes a minimum
  severity).

Read it from the web :ref:`Run view <run-view>`'s ``log`` panel and the Explorer's **Log** tab, or
from an agent with ``search_run_logs``. What it makes possible that a log *stream* cannot:

.. code-block:: sql

   -- which runs logged this, and did they fail?
   SELECT r.config_name, r.run_id, r.passed, count(*) AS hits, min(l.sim_time) AS first_at
   FROM run_log l JOIN runs r USING (config_name, run_id)
   WHERE l.message LIKE '%CRITICAL FAILURE%'
   GROUP BY 1, 2, 3 ORDER BY hits DESC;

The raw streams stay files and stay the record of what was printed. ``get_campaign_log`` /
``get_job_log`` read them directly, including while the job is still writing them.

.. _per-run-resource-usage:

``resource_usage`` and ``system_usage`` — what the run cost
""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""

``resource_usage`` is one row per container per process **name** per ~1 s sample, on the run's
own playback clock, from the job's ``resource_usage_<container>.csv`` files. Columns:
``timestamp`` (sim), ``wall_ts``, ``in_window``, ``container``, ``name``, ``cpu_percent``,
``memory_rss_bytes``, ``num_pids``, ``shm_used_bytes``, ``shm_total_bytes``.

Why it is a table and not just those files: the cluster gives a job a fixed number of cores, so a
simulator that starves the stack changes what the stack does. That is a competing
explanation for any behavioral result, and it can only be ruled out in the same query as
the behaviour.

.. code-block:: sql

   -- did anything run out of CPU during the trial?  (cpu_percent is PER-CORE:
   -- one saturated core is 100, so the ceiling is 100 * available_cpus)
   SELECT u.container, MAX(u.cpu) AS peak, 100.0 * r.available_cpus AS saturation
   FROM (SELECT config_name, run_id, container, wall_ts, SUM(cpu_percent) AS cpu
         FROM resource_usage WHERE in_window = 1 GROUP BY 1, 2, 3, 4) u
   JOIN runs r USING (config_name, run_id)
   GROUP BY 1, 3;

Two things are deliberate:

* **Rows are keyed by process name, not pid.** Pids churn — a respawned node is a new pid and
  the same program — and no pid is comparable across runs. ``num_pids`` records how many
  shared a name in that tick.
* **A run with no** ``test.xml`` **still gets its whole trace**, every tick in-window: a run
  killed mid-flight is the one whose trace matters most.

``cpu_percent`` and ``memory_rss_bytes`` are sums over the processes sharing a name: CPU is
per-core, and summed RSS double-counts pages shared with forks.

**The run's shared-memory pool** is in ``shm_used_bytes`` / ``shm_total_bytes`` — one pool for
the whole run, repeated on each tick's rows, so ``MAX`` is the only aggregate over them that
means anything. A run's peak is ``MAX(shm_used_bytes)`` over its rows, bring-up included; NULL
means the pool was not sampled, which is unmeasured rather than unused. Sizing it:
:ref:`configuration` under ``shm_size``.

``system_usage`` is the sibling for figures belonging to the **container as a whole** rather
than to a process — one row per ~1 s, no ``pid``, from the job's ``system_usage_<container>.csv``
files. It is separate because ``resource_usage`` is per-process *by contract*: every reader
aggregates it that way, so a container-level figure written there would be summed as though it
were one process among many. Its columns are whatever the node could answer, so a column may be
absent entirely rather than empty — a runtime that cannot report and a container that never
stalled are different facts, and a zero would make the first indistinguishable from the second:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Columns
     - What they answer
   * - ``nr_periods``, ``nr_throttled``, ``throttled_usec``
     - Did the kernel STOP this container because it hit **its own** CPU limit? Throttling
       does not fail a run, it just makes it slower, so nothing else records it.
   * - ``memory_current``, ``memory_peak``, ``memory_max``
     - Memory as the KERNEL accounts it, which the per-process rows cannot give: RSS counts a
       shared page once per process, so summing it over a stack of ROS nodes sharing
       libraries and a DDS shared-memory segment over-reports badly. ``memory_max`` is absent
       rather than huge when no limit is in force.
   * - ``memory_anon``, ``memory_file``, ``memory_shmem``, ``memory_slab``
     - What that memory is MADE OF, which decides how much of it must be reserved:
       ``anon + shmem + slab`` survives reclaim, ``file`` is page cache the kernel drops
       under pressure. Sizing a limit from ``memory_current`` reserves the cache too.
   * - ``cpu_usage_usec``
     - CPU time the kernel billed the cgroup — exact, where summing ``cpu_percent`` over the
       processes is a sampled estimate that misses anything short-lived.
   * - ``cpu_stall_some_usec`` / ``_full_usec``, and the same for ``memory`` and ``io``
     - PSI: how long tasks were runnable but **not running** — the container crowded out,
       which the throttle counters cannot show. ``full`` (every task waiting) is the figure
       that carries a finding; ``some`` is high in normal operation for a container running
       many processes against few cores.
   * - ``node_cpu_stall_some_usec``
     - The whole **machine's** pressure. A node fact repeated on every row of every
       container, so never sum it across a pod — it is what separates "this pod asked for too
       little" from "this node is oversubscribed".
   * - ``memory_events_max``, ``_oom``, ``_oom_kill``
     - Allocations the kernel refused, and processes it killed for them. Memory is sized on
       the peak because exceeding it is a kill rather than a slowdown; this is the only place
       a mid-trial death names its cause.

Everything above except the memory gauges is a **monotonic counter**, so read a delta within the
trial window, never a ``SUM`` and never a bare ``MAX`` (``memory_current`` is a gauge,
``memory_peak`` a high-water mark, and the ``memory.stat`` breakdown gauges; those are read as
they come). Prefer ``run_validity_view``, which takes that delta and applies the thresholds for
you. It derives two flags, and they are **opposite diagnoses with opposite remedies**:
``quota_bound`` means the container exhausted the quota its own ``limits.cpu`` buys — raise the
limit; ``contended`` means it was runnable and got no CPU *without* having hit that ceiling —
other work took cores it had not reserved, so raise its ``resources.cpu`` request, or put fewer
jobs on the node. A container can be both, and the ceiling is reported first because that remedy
is a line in the campaign's own ``.vast``.

.. _scenario-verdict:

``scenario_timestamps`` — where the trial ended
""""""""""""""""""""""""""""""""""""""""""""""""

One row per run: ``config_name``, ``run_id``, ``timestamp`` (sim seconds), ``wall_ts``,
``status`` (``succeeded`` / ``failed``) and the ``message`` itself — the first line of the run's
``run_log`` in which scenario-execution's own logger announced a verdict (``Scenario '<name>'
succeeded.``, or the failure line ``add_result`` logs). The recognition lives in one module,
``robovast_decode.scenario_markers``, and runs **here and nowhere else**: every later reader
queries this table instead of matching the log text again, which is what keeps the web UI,
``search_run_logs`` and the playback clock from disagreeing about where a run ended.

"The first verdict in the log" is the right answer because a job runs one run, so the
``run_log`` rows are that run's own.

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
success while the harness failed, or the reverse. A run with no row reached no verdict — killed
by its deadline, say — and is left untrimmed rather than trimmed to a guess.

A run recorded more than once
"""""""""""""""""""""""""""""

A recorder that restarts mid-trial writes a second bag beside the first, because
``ros2 bag record``'s default name carries a timestamp. **The last attempt is the run**:
everything else in the directory exists once — one run log, one verdict, one set of videos —
so tabulating an earlier bag would put a different attempt's trajectory under this run's
outcome. The bag tables are built from the last attempt, ordered by the start time in each
bag's own ``metadata.yaml``, and by name where that is missing; the earlier attempts stay on
disk and are not tabulated.

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
queryable live and the ``runs`` table and ``run_view`` are built from those
rows rather than by re-parsing every ``test.xml`` — see
:ref:`the campaign store schema <campaign-store>`.

.. _stopping-one-job:

A run somebody stopped: ``killed``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``run_view.status`` is ``passed``, ``failed``, ``error``, ``unknown`` — or ``killed``,
which means an operator ended that run's job by hand while the campaign was running (the
web UI's per-job **Stop**, the ``stop_job`` MCP tool, or ``vast campaign stop-job``).

**A killed run is not a trial failure.** Nothing was learned from it about the system
under test, so it is a *missing measurement*: RoboVAST counts it apart from the failures
everywhere it reports them — ``num_killed`` beside ``num_failed`` in the campaign counts
and ``get_campaign_summary``, its own tally in the web UI's Details panel, and
``runs.killed`` on the live status. Treat it the same way in your own analysis::

   SELECT config_name, COUNT(*) FILTER (WHERE status = 'passed') AS passed,
          COUNT(*) FILTER (WHERE status IN ('failed','error')) AS failed
   FROM run_view WHERE status <> 'killed' GROUP BY config_name

``failure_message`` on a killed run names the surface that stopped it and the reason its
operator gave (``manually stopped via webui: stuck in nav recovery``), which is the only
record of *why* — so it is worth giving one.

``killed`` replaces ``unknown`` and **only** ``unknown``: a run of a killed job that had
already written a valid ``test.xml`` keeps its real verdict: a run can finish before its stop
lands, and its result is measurement, never overwritten.

.. _results-unreadable-rosbag:

Its recording was cut off
"""""""""""""""""""""""""

Stopping a job kills its recorder mid-write, so its bag is never finalized: no
``metadata.yaml`` sidecar, and an mcap file with no summary section. The same is true of a job
that ends abruptly for reasons nobody chose — an evicted pod, an OOM-kill. **Such a recording is
read up to its last complete record**: the decoder walks an mcap file record by record and needs
no summary, so the data the run did record becomes rows like any other run's, and the topics are
listed from the file's own channel records where the sidecar is missing.

What the cut costs is the tail: whatever the recorder had not yet written. A table built from a
recording whose run has no ``test.xml`` or whose bag has no sidecar is entered as not final, so it
is looked at again on the next request rather than frozen as it was first seen.


A configuration that produced nothing: ``missing``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Every status above belongs to a run. ``missing`` belongs to a *configuration*: the campaign
was composed with it and its results never reached the tree — never dispatched, or lost
between the cluster and the results root. It appears in ``run_view`` as one row with
``run_id`` NULL, exactly as ``composition_failed`` does for a search draw that could not be
built, because a join through ``run`` would otherwise drop it.

**This is what makes a shortfall visible at all.** The results tree states what came back;
only the composition record (``_transient/configurations.yaml``) states what was asked for,
and without comparing the two a sweep that lost a share of its cells is indistinguishable
from a smaller sweep that ran perfectly. ``get_campaign_summary`` therefore counts
``num_configs`` over the *declared* set and reports ``num_missing_configs``,
``missing_configs`` and a note when they differ; the aggregates beside them are over a
partial design, which is the one thing a summary must not leave unsaid.

::

   SELECT config_name FROM run_view WHERE status = 'missing'

A campaign whose archive has no ``_transient`` cannot be checked this way, and says so in
the log rather than reporting a complete design it cannot vouch for.


A trial the runner threw away: ``invalid``
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``invalid`` means a container the trial ran against **crashed and was restarted under it**.
The simulator (or the system under test) took the run's state with it; the container the
kubelet starts in its place runs no workload, and the runner ends the job as soon as it reads
the restart off the pod. Whatever verdict the scenario reached in between describes a trial
that had already lost its process.

**It is the one status that overrides a written verdict**, and that inverts the rule stated
for ``killed`` just above. The inversion is the whole reason it is a separate kind rather
than another kill:

* a *killed* run's ``test.xml`` was written **before** the intervention landed, so it is
  real measurement and overwriting it would destroy data;
* an *invalid* run's ``test.xml`` was written **by** the trial the restart broke — it is
  the confidently wrong result the detection exists to prevent, and it is at its most
  dangerous when it says ``passed``, because nothing else about the run looks wrong.

Discarding is not destroying. The ``test.xml`` stays on disk, and the verdict being
overridden is recorded in the ledger entry, so the override is auditable and reversible.

Like ``killed``, it is **not** a trial failure — it is an infrastructure fault, not evidence
about the system under test — so it is counted apart (``num_invalid``) and excluded from
pass rates::

   SELECT config_name, COUNT(*) FILTER (WHERE status = 'passed') AS passed,
          COUNT(*) FILTER (WHERE status IN ('failed','error')) AS failed
   FROM run_view WHERE status NOT IN ('killed','invalid') GROUP BY config_name

A cell that loses *every* run this way scores ``no_sample`` rather than a fabricated 0.0,
and the search carries on: one crashed sidecar costs its own runs, never the campaign's
completed batches.

What killed it: ``container_failure_view``
""""""""""""""""""""""""""""""""""""""""""

The post-mortem is captured at the moment of the restart — while the pod still exists,
because it is about to be deleted — and lands in **two** places, deliberately:

``_execution/container_failures.json``
   the record: every field of the container's termination state, plus the last 400 lines of
   the **dead instance's own log** (``kubectl logs --previous``).

``campaign.db`` → ``container_failure_view``
   the same failures as rows, so the question is one query. It is written by the controller as the
   campaign runs, so it answers for a campaign that dies mid-batch — exactly the campaign
   that needs explaining — without anything built after it.

::

   SELECT run_key, node_label, container, role, exit_code, signal_name, reason, memory_limit
   FROM container_failure_view ORDER BY run_key

``signal_name`` is usually the answer: ``exit_code`` 135 is ``128 + 7``, i.e. ``SIGBUS``, and
137 is ``SIGKILL`` (an OOM kill). A ``NULL`` ``memory_limit`` means no limit was declared at
all, which is itself a finding — such a container is told by the downward API that it has
the whole node, and the pod's shared ``/dev/shm`` is sized the same way. Join back onto
``run_view`` on ``config_name || '/' || run_id = run_key``.

For a ``SIGBUS`` the sizing half of the answer is in ``resource_usage``: the run's peak
``shm_used_bytes`` against the ``shm_total_bytes`` in force says whether it ran out or was never
that big::

   SELECT config_name, run_id, MAX(shm_used_bytes) AS shm_peak, MAX(shm_total_bytes) AS shm_limit
   FROM resource_usage GROUP BY config_name, run_id

An invalidated job's recording is cut off for the same reason a stopped job's is (it is
deleted at ``grace_period_seconds=0``), and is read the same way
(:ref:`results-unreadable-rosbag`).

The kills themselves are recorded in ``_execution/interventions.json``, which exists only for a
campaign somebody intervened in. **One ledger holds every kind of intervention**, each entry
carrying a ``kind`` — ``killed`` for a job stopped by hand, ``probed`` for a run somebody read
into while it was going — because "what was done to this run?" is one question and answering it
should not mean knowing to ask twice. What *follows* differs by kind and that is why the readers
do: a kill becomes a run status, while ``probed`` is a separate column of the ``runs`` table and
never touches the verdict. Putting an intervention into the measured outcome is the same mistake
that keeping ``killed`` out of ``num_failed`` avoids.

.. warning::

   **Two unrelated things are called a probe.** ``runs.probed`` above is a *campaign* run
   somebody looked inside while it was going, which is why it is recorded and excluded. A
   **calibration probe** (:ref:`cluster-node-calibration`) is a different object entirely: an
   extra run that measures one node before the campaign places work there, which writes to the
   reserved ``_calibration/`` directory and so never becomes a run at all. Nothing in ``runs``
   or in ``interventions.json`` ever refers to one.

.. note::

   ``metadata.yaml`` is **not** how a caller reads a campaign's results: it is written only by
   postprocessing, while a campaign's outcomes are in ``campaign.db`` from the moment each run
   ends. Query ``run_view`` instead (see :ref:`mcp-analysis`). The file is the campaign's
   self-contained metadata document — what the FAIR/PROV-O export and the publication zip read.


.. _results-decoder-config:

Configuring the decoder: the ``rosbags_*`` entries
--------------------------------------------------

A campaign that says nothing gets every table its recordings can give, with the defaults above.
The ``rosbags_*`` entries of ``results_processing.postprocessing`` (and ``search.postprocessing``)
**refine** those defaults for the topics they name; they run nothing. They are written, as the
decoder's configuration, to ``_execution/tables.yaml`` when the campaign's config is frozen at
launch and again whenever its postprocessing runs, so a copy of the campaign builds the same
tables anywhere:

.. code-block:: yaml

   # _execution/tables.yaml
   groups:
   - bag_dir: rosbag2
     plugins:
     - {type: tf_to_csv, frames: all, require: [base_link]}
     - {type: action_to_csv, action: navigate_to_pose}
   containers: [robovast, simulation, sut]

``groups`` is the entries grouped by recording (``rosbag2`` for the run's own, ``logs/rosout_bag``
for the job's); ``containers`` names the containers the campaign runs, which is how a container
that recorded nothing is reported rather than silently absent from ``run_log`` and
``resource_usage``. A configured handler replaces the default for its topics, and the rest of the
recording keeps its defaults.

.. list-table::
   :header-rows: 1
   :widths: 26 16 58

   * - Entry
     - Recording
     - What it configures
   * - ``rosbags_tf_to_csv``
     - ``rosbag2``
     - ``poses``: ``frames`` (a list of child frames, or ``all``; default ``[base_link]``),
       ``require`` (frames that must yield a map-relative pose, or the table fails for that run
       naming the transforms present; an explicit ``frames`` list requires itself),
       ``csv_filename`` (the table's name, default ``poses``).
   * - ``rosbags_to_csv``
     - ``rosbag2``
     - ``rosbag2_<topic>`` for the listed ``topics``, including one not tabulated by default —
       a ``LaserScan``, say, whose ranges then become one encoded cell per message.
   * - ``rosbags_action_to_csv``
     - ``rosbag2``
     - ``action_<action>_feedback`` / ``_status`` for ``action``; ``filename_prefix`` renames
       both.
   * - ``rosbags_nav2bt_to_csv``
     - ``rosbag2``
     - ``nav2_behavior_tree`` from ``/behavior_tree_log``. No parameters.
   * - ``rosbags_costmap_to_csv``
     - ``rosbag2``
     - ``costmaps`` for the listed ``topics`` (any ``OccupancyGrid`` topic is tabulated by
       default; listing them fixes which).
   * - ``rosbags_to_webm``
     - ``rosbag2``
     - ``videos``: a ``CompressedImage`` ``topic`` (default ``/camera/image_raw/compressed``)
       encoded to WebM in the run directory. The rate is derived from the frames' own stamps as
       ``(n-1)/duration``; ``fps`` (default 30) is used only when the frames span no time. One
       entry per camera; all of them are one ``videos`` table.
   * - ``rosbags_rosout_to_csv``
     - ``logs/rosout_bag``
     - ``rosout``: ``min_level`` (``DEBUG`` … ``FATAL``) keeps only lines at or above it.
   * - ``rosbags_clock_to_csv``
     - ``logs/rosout_bag``
     - ``clock_map``: ``tolerance_s`` (default 0.005), how far the decimated map may mispredict.
   * - ``rosbags_process``
     - any
     - the same handlers written by ``type`` per recording: ``groups: [{bag_dir, plugins}]``, or
       ``plugins`` with an optional ``bag_dir`` for one recording.

Every entry also takes ``bag_dir`` to point it at a recording other than its default. The full
reference, with the parameters each takes, is under ``postprocessing`` in :ref:`configuration`.

**Encoding a video needs** ``ffmpeg`` **where the table is built**; without it the ``videos``
table fails for that run with that reason rather than coming out empty.


.. _results-postprocessing:

Postprocessing
--------------

Postprocessing is what runs when a campaign's runs end. It runs **in the service process** —
no Kubernetes Job, no container, no execution image — in four parts:

1. **The campaign's own steps**: the non-``rosbags_*`` entries of
   ``results_processing.postprocessing``, in order — plugins named by entry point
   (``robovast.postprocessing_commands``) or by ``./path.py:Class`` beside the config (see
   :ref:`extending-postprocessing`). A step's output files are tables like any other run file.
   Built in: ``command`` (run a script) and ``compress`` (a tarball per campaign, leaving out
   ``.cache``); ``robovast-nav`` adds ``nav2_bt_tree``, which reads the ``nav2_behavior_tree``
   table and writes each run's ``nav2_behaviors.csv``. List them with
   ``vast results postprocess-commands``.
2. **The campaign-end pass** (:ref:`results-campaign-end`).
3. **The provenance record**, ``_transient/postprocessing.yaml``, written **last** among the
   derived data: its presence is what makes a campaign read as postprocessed (``postprocessed``
   in the status, the *(postprocessed)* archive variant). A pass that is cancelled or fails before
   it never writes it.
4. **Metadata**: ``metadata.yaml`` and the provenance graph (:ref:`results-metadata`).

It runs automatically when a campaign's runs finish. To run it again — after editing a
postprocessing step, or on a campaign imported raw — name the campaign:

.. code-block:: bash

   vast campaign postprocess CAMPAIGN

**Options**

.. option:: -f, --force

   Clear the campaign's built tables first, so what it declares is built again — by this
   decoder, from the records. The steps are passed ``force`` too.

.. option:: --replay

   Clear the campaign's built tables and build **every table its records can give, for every
   run** — not only the declared ones — before the campaign-end pass. The invariant it rests
   on: a replay yields the rows a live watcher wrote as the runs went. The tables a run's
   recordings were decoded into as it recorded, and the derived tables its job's files were
   derived into whole, are the same decoder over the same records, so the campaign reads the
   same whether it was followed live or replayed afterwards. ``replay`` on the MCP
   ``run_postprocessing`` tool and on the service's request body is the same switch.

.. option:: --skip PLUGIN

   Skip a postprocessing step (repeatable), e.g. ``--skip nav2_bt_tree``.

It is **dispatched, not awaited**: the campaign re-enters its ``postprocessing`` phase and the
command returns. Follow it exactly as after a launch:

.. code-block:: bash

   vast campaign postprocess my-campaign-2026-03-20-153630
   vast campaign wait my-campaign-2026-03-20-153630

The web UI's **Retrigger postprocessing** and the MCP ``run_postprocessing`` tool are the same
operation.

.. _results-campaign-end:

The campaign-end pass
^^^^^^^^^^^^^^^^^^^^^

Most tables are built when first asked for. The campaign-end pass builds the ones the campaign
**declares**, for every run, so what it says it shows is ready when it finishes:

* the derived tables (``run_log``, ``scenario_timestamps``, ``resource_usage``,
  ``system_usage``, ``run_clock``), which every run view and every log surface read;
* the tables its declared plots query (``visualization.results.data_browser.plots``) — a plot
  over a view builds the tables that view reads;
* ``videos``, when a ``rosbags_to_webm`` entry asks for one.

Then it writes two campaign-level tables:

``run_health``
   The campaign's **health checks**, declared under ``results_processing.health_checks``
   (entry-point names in ``robovast.health_checks``, or ``./path.py:Class``). Each is called
   ``check(conn, campaign_id)`` with a read-only connection to the campaign's tables — its
   ``execute(sql, params)`` builds what the statement names and runs it with DuckDB — and returns
   rows of ``config_name, run_id, check, level`` (``ok`` / ``warn`` / ``error``), ``detail``,
   ``value``, ``unit`` — the table's ``check_name``, ``level``, ``detail``, ``value``, ``unit``
   and ``source``. Health grades a run and never decides pass/fail; ``ok`` is a row, and a
   run with no row for a check was *not checked*. The table is written even when no check had
   anything to say. See :ref:`configuration` under ``health_checks``.
``postprocessing_steps``
   How each table was made: one row per file a step wrote (``plugin``, ``output``,
   ``table_name``, ``sources_json``, ``params_json``), and one per table the decoder built, with
   its version.

A table that could not be built for some run **fails postprocessing**, naming the table, the run
and the reason: "postprocessed" means what the campaign declares is there. The records are
untouched, so running it again builds again.

**A failure here does not fail the campaign.** Its runs are the deliverable and remain
downloadable; the campaign stays ``finished`` and the failure is recorded on its own durable
field, ``postprocessing_error``, surfaced as a warning badge in the UI. Re-running the step
successfully clears it. This is distinct from a *run* failure, which reports the campaign phase as
``failed``.

.. _results-retrigger:

Re-running a finished campaign's post-run steps
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The two steps that run *after* a campaign's scenarios finish — **postprocessing** and the
**upload-to-share** — can each be re-triggered on a finished campaign, and each works **from the
stored campaign alone**: no live campaign process is required, so a re-trigger is available even
after the ``robovast-service`` (``vast serve``) was restarted. Under the web UI's *Monitor* each
finished campaign's actions menu offers *Retrigger postprocessing* and *Export to share*; the same
operations are ``vast campaign postprocess`` and ``vast share export -i <campaign-id>`` on the
command line and the MCP tools ``run_postprocessing`` and ``run_share``.

A third operation shares their shape without being a *re*-run: **importing** a
campaign this deployment never ran, from an archive
(``vast campaign import <archive>``) or from the share (``vast share import
<campaign-id>``). It is dispatched the same way, enters the ``importing`` phase, and
— when what arrived is a raw archive, carrying no postprocessing record — rolls straight
on into ``postprocessing``. Its per-stage verdicts land in ``_execution/import.json`` and its
narrative in ``_execution/import.log``. A *degraded* import is usable-but-incomplete
rather than a failure.

A genuine failure is **kept, as a failed campaign**, and the refusal names what was
missing rather than which check noticed. Registering the campaign is what makes it visible
while it arrives, so the entry outlives the failure, and keeping the directory keeps the
``import.log`` and ``import.json`` that explain it. Remove it with ``vast campaign delete``, or
import again with ``--force``.

The mirror of that check runs on the way **out**: an export refuses a campaign with no
frozen ``_config/`` instead of writing an archive whose only possible future is an ingest
refusal on somebody else's service, after a full transfer, with the source out of reach.

A re-trigger is **dispatched in the background and returns immediately** — the campaign
re-enters the ``postprocessing`` (or ``sharing``) phase and you follow its progress and log in
the campaign view, exactly like the original run; it returns to ``finished`` when done. The web
*Retrigger postprocessing* dialog therefore closes as soon as you click *Run*. A second
re-trigger is refused while one is already running.

Because it re-enters that phase as a tracked campaign, a re-trigger can also be **stopped**
like one: ``stop`` cancels it, the campaign returns to ``finished``, and
``postprocessing_error`` says it was cancelled rather than that it failed. Nothing already built
is lost and nothing claims to be built that is not: a table is recorded in the manifest only once
its files are written, and the provenance record that says a campaign is postprocessed is written
last, so a cancelled campaign simply reads as not postprocessed (see :doc:`architecture`).

Editing the postprocessing parameters before re-running **overwrites the
``results_processing.postprocessing`` block of the campaign's own ``_config/<name>.vast``
in place** — it is config, not captured data, so there are no override files or
revisions, and the recordings and the as-ran ``configuration``/``execution`` are left
untouched. Re-running rewrites ``_execution/tables.yaml`` from the edited block, so a changed
``rosbags_*`` entry shapes the tables built from then on; pass ``--force`` to rebuild the ones
already built. For the **upload-to-share**, the target provider is taken from the service
environment (``ROBOVAST_SHARE_TYPE`` and its credentials); adjust it and export again to upload
the same campaign to a different provider. The archive is named for what it is — a campaign
exported after postprocessing goes up as ``.postprocessed.tar.gz``, one exported before it as
``.raw.tar.gz`` — read off postprocessing's own provenance record
(``_transient/postprocessing.yaml``), so the campaign-end upload and a later export agree by
construction. Neither carries ``.cache/``.

Custom postprocessing plugins that need third-party Python packages — an
entry-point postprocessing command, or the dependencies a local
``./file.py:Class`` plugin imports — declare those packages in the ``.vast``'s
top-level ``plugins:`` list (see :ref:`configuration`). They are installed into the
campaign's ``.robovast_plugins/`` and put on ``sys.path`` before postprocessing
runs, including on a re-run in a fresh process.


.. _results-tables-ahead:

Building ahead, and clearing
----------------------------

**Never needed for an answer**: every table is built the first time something names it. Building
ahead moves that cost to now, for a finished campaign about to be analyzed at length; clearing
frees the storage, and the next use builds again.

.. list-table::
   :header-rows: 1
   :widths: 18 41 41

   * - Surface
     - Build a finished campaign's tables
     - Clear them
   * - Web UI
     - **Build all tables** in the campaign's actions menu
     - the admin page's **Service cache** panel (``table cache``), for every campaign
   * - CLI
     - ``vast campaign tables build <id> [--table NAME ...]``
     - ``vast campaign tables clear <id>``
   * - MCP
     - ``build_campaign_tables(campaign_id, tables=None)``
     - ``clear_campaign_tables(campaign_id)``
   * - HTTP
     - ``POST /campaigns/{id}/tables/build``
     - ``DELETE /campaigns/{id}/tables``

A build runs in the background, on the service; its progress is the campaign log's ``TABLES``
section (``_execution/tables.log``). Clearing is refused while the campaign runs or its tables
are being built. ``describe_campaign_data`` and the Data browser report each table as built for
M of N runs; describing builds nothing.

Without a service, ``robovast-decode`` builds a campaign directory's tables in place:

.. code-block:: bash

   robovast-decode tables <campaign-dir>                      # what the records can give, and what is built
   robovast-decode build <campaign-dir> [--table NAME] [--run CONFIG/RUN] [--force]


.. _results-querying:

Querying
--------

SQL over a campaign is answered by an in-process **DuckDB** engine (the ``robovast-data``
distribution) over views defined per query from the campaign's manifest — no database server, no
import step. The same engine answers the web UI's Data browser and run-view panels, the MCP
``query_campaign_data_sql`` tool, the HTTP ``POST /campaigns/{id}/query`` route (and its CSV twin,
``query.csv``), health checks, and a notebook's ``.sql()``.

**What a query can name:**

* every table (:ref:`results-tables`), unqualified;
* ``runs`` — one row per run: ``status``, ``passed``, ``duration_s``, ``errors``, ``failures``,
  ``objective``, ``start_time``, ``end_time``, the host (``instance_type``, ``node_label``,
  ``cpu_name``, ``available_cpus``, ``available_mem_bytes``), ``probed``, and every varied
  factor as a typed ``param_*`` column — plus one run-less row per unit that produced no run;
* the campaign's record as the ``campaign`` schema: ``campaign.campaign``, ``campaign.unit``,
  ``campaign.run``, ``campaign.job``, ``campaign.batch``, ``campaign.node``,
  ``campaign.container_failure``;
* the views: ``run_view`` (one row per run with its unit's ``params_json`` and
  ``channels_json``, batch and host, plus run-less units), ``config_view`` (the resolved
  configuration as one row per node, ``fullkey`` ``$.a.b``), ``container_failure_view``,
  ``run_validity_view`` and ``pose_track_view``.

``describe_campaign_data`` lists all of them with their columns and ``kind`` (table, view or
record) — a table's columns once it is built for some run.

**DuckDB's dialect.** ``CAST(x AS DOUBLE)``, ``x::JSON`` with ``->`` / ``->>``, ``unnest``,
``median``, ``quantile_cont``, ``regexp_matches``. Two macros keep SQL written for other
engines meaning what it meant: ``PERCENTILE(value, p)`` with ``p`` from 0 to 100, and
``REGEXP(pattern, value)`` as a search. A TEXT column orders lexicographically, so cast before
comparing it.

.. code-block:: sql

   SELECT r.param_speed, median(m.path_length) AS path_length
   FROM runs r JOIN nav_metrics m USING (config_name, run_id)
   WHERE r.status = 'passed'
   GROUP BY 1 ORDER BY 1;

   SELECT config_name, sysinfo_json::JSON ->> 'cpu_name' AS cpu FROM run_view;

**What a query builds.** The tables it names, and the tables the views it names read, for the
runs in scope that lack them (:ref:`results-table-cache`). A top-level ``WHERE`` that restricts a
table with ``config_name`` / ``run_id`` equality or ``IN`` builds only those runs; anything else
builds every run. What could not be built is reported with the answer, by table and run.

**What a query may do.** One ``SELECT``, checked on DuckDB's own parse. File access is limited to
the campaign's ``.cache/tables/``, external access is off, the configuration is locked, and a
query that runs past its time is interrupted. **One campaign per query** by default; the HTTP
query route takes further campaign ids in ``campaigns``, and then every view is the union of
theirs, with ``campaign_id`` in every row.

``run_id`` restarts at 0 in every configuration, so join on ``(config_name, run_id)``, never
``run_id`` alone.

.. _channel-param-columns:

Which channel a factor was written on, and its column
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A campaign varies its factors on three channels (:ref:`the destination reference
<config-variation-destination>`), and the ``runs`` table gives each one a ``param_*`` column
so that a question about a factor is the same query whichever channel it came from.

The name differs, because the destinations do:

.. list-table::
   :widths: 20 40 40
   :header-rows: 1

   * - Channel
     - Destination
     - Column
   * - ``scenario:``
     - ``speed``
     - ``param_speed``
   * - ``sim:``
     - ``components.floorplan.floor.friction``
     - ``param_sim_friction``
   * - ``sut:``
     - ``nav2.….inflation_layer.inflation_radius``
     - ``param_sut_inflation_radius``

A scenario parameter keeps its own name. A ``sim:`` or ``sut:`` destination is a *path*, and its
whole path makes no column anybody can type — the ``sut:`` example does not fit in an identifier
at all, and an XPath destination is not identifier-shaped anywhere but its end. So the column is
named from the **end** of the destination, prefixed by its channel, and grows leftwards only as
far as it must to stay unambiguous: two ``friction`` keys under different components become
``param_sim_floor_friction`` and ``param_sim_wall_friction`` rather than quietly sharing one
column.

Uniqueness is decided over the **whole campaign**, not one configuration, because the table is
one shape for every row in it.

.. note::

   **Do not guess a column name — read it from the table.** The suffix rule depends on what
   else the campaign varies, so the same destination is ``param_sut_inflation_radius`` in one
   campaign and ``param_sut_inflation_layer_inflation_radius`` in another that also varies the
   global costmap's. ``describe_campaign_data`` lists what a campaign actually has.

Every destination's value is in ``run_view.channels_json`` regardless, under the channel name
the ``.vast`` writes it on — including one whose name would collide with a scenario parameter,
or would not fit an identifier even at full length. Those get no column and are logged at
warning level when ``runs`` is built, naming the destination: a column silently missing is the
same wrong answer as a column silently shared.

.. _non-finite-values:

A measurement with no finite value
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A metric can come out infinite or undefined and still be right: a range sensor reports no
return, a trial produced no trajectory so its path length is infinite, a ratio had no
denominator. Such a value is stored as a number — a double's ``inf``, ``-inf`` or ``nan`` — in a
column that stays numeric, however the file spelled it (``inf``, ``-inf``, ``infinity``, ``nan``,
any case or sign). A value nobody measured is ``NULL``, so the two never have to be told apart
by guesswork::

    SELECT AVG(path_length) FROM nav_metrics;                 -- inf if any trial was censored
    SELECT * FROM nav_metrics WHERE isinf(path_length);       -- the censored trials
    SELECT * FROM nav_metrics WHERE isnan(path_length);       -- the undefined ones
    SELECT * FROM nav_metrics WHERE path_length IS NULL;      -- and the unmeasured ones

In DuckDB ``NaN`` equals itself and sorts above every number, infinity included, so an
``ORDER BY`` or a ``MAX`` over such a column puts it last.

Inside a JSON-encoded value — a container-valued ``param_*`` column, ``channels_json`` —
the same three appear as the JSON strings ``"inf"``, ``"-inf"`` and ``"nan"``, so
``param_gaps::JSON ->> 1`` is ``nan`` rather than a token that would make the cast fail,
and ``CAST(param_gaps::JSON ->> 1 AS DOUBLE)`` is the number.


.. _results-notebooks:

Notebooks and your own machine: ``robovast-data``
-------------------------------------------------

``robovast-data`` is the same engine as a library. It reads a campaign directory — or a
downloaded ``.tar.gz``, extracted beside it on first use — builds each table on first use into
that campaign's own ``.cache/`` with the decoder the service uses, and returns pandas frames. No
service, no ROS and no container image are needed:

.. code-block:: python

   from robovast_data import Campaign, Corpus, open_data, read_table, read_runs

   c = Campaign("~/Downloads/<campaign>")          # or the .tar.gz
   c.runs                                          # the runs table
   c.tables                                        # what can be built, and for how many runs
   poses = c.table("poses", with_params=True)      # built on first use, then read
   one = c.table("poses", config="<config>", run=0)
   c.sql("SELECT config_name, avg(duration_s) FROM runs GROUP BY 1")

   Corpus("~/Downloads/nav-*").table("action_navigate_through_poses_status")

``open_data(path)`` answers for whatever the path selects — the campaign, one configuration's
directory, or one run's — which is how a notebook cell reads the same on a laptop and in the web
UI's Results Explorer. Writing those notebooks: :doc:`analysis_notebooks`.


.. _reading-result-files:

Reading these files
-------------------

Every path in the tree above has one **address**, and it is the same string
whether you type it at the CLI, pass it to an MCP tool, or ``GET`` it from the
service::

    /results/<campaign_id>/<path>

The path after the campaign id is exactly the path in the tree — ``_execution/
outcome.json``, ``<config-name>/<run>/test.xml`` — so what a listing shows is what
you can read. Campaign results are **read-only**: they are the record of a run, and a
rewritten record is one nobody can check the figures drawn over it against. Workspace
*inputs* live in the writable half of the same address space,
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
the whole campaign archive (that is ``vast campaign download <campaign-id>``, which
writes one ``.tar.gz`` of the campaign's records, without ``.cache/``).

The same addresses work over HTTP (``curl <service>/results/<campaign>/<path>``)
and from an LLM through the ``read_file`` / ``list_files`` MCP tools — see
:ref:`mcp-files`. Reading a campaign on this machine needs no running service; against a
cluster service the read serves that one file off the service's results volume, not the
campaign.

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

The file is produced by a four-phase pipeline:

1. **Generic metadata** — collected by ``MetadataGenerator``
   (``robovast.results_processing.metadata``).  This includes configurations, test
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

4. **Derivation** — each configuration entry gets a ``derived_from`` field
   naming the ``.vast`` configuration it was expanded from, which is also the
   ``prov:wasDerivedFrom`` edge from the cell to that configuration in
   ``metadata.prov.json``.  A configuration that records no parent carries no
   such field.

A campaign whose recorded outcome says it ended early — ``stopped``, ``failed`` or
``crashed`` — is described with the runs it has: ``execution.ended_early`` names the phase,
a configuration may have fewer run directories than ``execution.runs`` planned, and the
campaign may have no run with a verdict, since the ending cuts runs short before they reach
one. Any other campaign with fewer runs than it planned, or none with a verdict, is refused
as a broken input — a shortfall on its own is also what an accidental gap looks like. More
runs than planned is refused however the campaign ended.

Example structure of ``metadata.yaml``:

.. code-block:: yaml

   configurations:
     - name: config-1
       config:
         growth_rate: 0.5
         initial_population: 100
       config_files: []
       created_at: '2026-03-04T16:15:03.212496'
       derived_from: config
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


.. _results-retrigger-preflight:

Re-running a campaign: the pre-flight
-------------------------------------

Every re-run — **Retrigger campaign** in the web UI, ``vast campaign rerun <id>``,
``start_campaign(from_campaign=<id>)`` over MCP, ``POST /campaigns/<id>/retrigger`` — is answered
by the service walking the campaign's records first, and refusing one that cannot work as
recorded: a launch that could only fail in the backend is refused before it starts. Five axes,
which fail independently and are all reported together:

``config``
   the frozen ``.vast`` is readable, and at a version the migration ladder can carry forward.
``host``
   this robovast still speaks the recorded image's container protocol.
``images``
   a new run can start from the images the campaign recorded. A container whose image the campaign
   *built* cannot be replaced, since the build context is not archived; one it merely declared is
   resolved again at launch.
``plugins``
   third-party ``plugins:`` resolved to something re-installable.
``providers``
   which asset-provider distributions supplied the campaign.

Only ``blocked`` refuses. ``unknown`` does not: a campaign whose records lack a given field is
exactly what a re-run of an old campaign is, and refusing it for a record nobody wrote would
defeat the purpose. Every blocking verdict names the artifact and how to obtain it.

Read the report without launching anything — it stages nothing and starts no container — with
``vast campaign rerun --check <id>``, ``get_campaign_summary``'s ``retrigger`` key, or
``GET /campaigns/<id>/retrigger/check``. Override it, for an axis you have decided you understand,
with ``vast campaign rerun <id> --force``, ``start_campaign(from_campaign=<id>, force=True)``,
**Re-run anyway** in the web UI's dialog, or ``force`` on the POST body.


.. _results-publish:

Publishing Results
------------------

Publication packages or distributes the results directory using plugins defined
in the ``results_processing.publication`` section of the ``.vast`` file.  Unlike
postprocessing (which operates on one campaign), publication plugins
receive the full results directory as input and are intended for tasks like
creating zip archives for upload or hand-off.

.. code-block:: bash

   vast results publish --results-dir PATH [OPTIONS]

**Options**

.. option:: -r, --results-dir PATH

   Directory containing the run results (parent of campaign directories).
   Required: there is no project file to take it from.

.. option:: -i, --campaign CAMPAIGN

   Publish only this campaign directory; without it, every campaign is published.

.. option:: -o, --override VAST_FILE

   Use the given ``.vast`` file instead of the one stored in
   ``<campaign-name>-<timestamp>/_config/``.

.. option:: -f, --force

   Overwrite existing output files (e.g. zip archives) without prompting.
   Equivalent to setting ``overwrite: true`` on every publication plugin.
   Without this flag, plugins that find an existing output file will ask the
   user interactively (default answer: yes / overwrite).

.. option:: --skip-postprocessing

   Run only the publication plugins, not postprocessing first.

.. option:: --skip-upload

   Run only packaging plugins (e.g. ``zip``); skip upload plugins (e.g. ``zenodo``).

.. option:: --allow-opaque

   Publish even when an input cannot be identified. The exemption is recorded in the dataset,
   so it is visible to whoever reads it.

**Example:**

.. code-block:: bash

   # Publish every campaign under a results directory
   vast results publish --results-dir /path/to/results

   # Publish and overwrite any existing archives without prompting
   vast results publish --results-dir /path/to/results --force

   # Publish one campaign with an override config
   vast results publish --results-dir /path/to/results -i my-campaign-2026-03-20-153630 \
       --override my_project.vast


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

   Source directory containing campaign directories.


.. _results-postprocess-commands:

Listing Postprocessing Plugins
-------------------------------

.. code-block:: bash

   vast results postprocess-commands

Lists all available postprocessing step plugins, their descriptions, and
parameters.  Useful for discovering which steps can be used in the
``results_processing.postprocessing`` section of the ``.vast`` file. The ``rosbags_*``
entries are not steps and are not listed; they are described under
:ref:`results-decoder-config`.


.. _results-override:

Using ``--override`` to Supply a Local ``.vast`` File
------------------------------------------------------

By default ``vast results publish`` reads the ``.vast`` configuration from the
**campaign snapshot** stored in
``<results-dir>/<campaign-name>-<timestamp>/_config/<name>.vast``.  This snapshot is copied
at execution time and may be out of date.

``--override`` (short form ``-o``) lets you point to any ``.vast`` file on disk,
for example your current working copy:

.. code-block:: bash

   # Use a local/updated .vast file
   vast results publish --results-dir /path/to/results --override my_project.vast

**When to use ``--override``**

- You want to publish existing results with updated publication settings without
  triggering a new execution campaign.
- The results were produced in a different directory and the campaign snapshot
  points to stale paths.
- You want to bypass the snapshot and always use the latest ``.vast`` during
  iterative development of a publication.

.. note::

   When ``--override`` is supplied, the same ``.vast`` file is used for
   **every** campaign folder found under the results directory.  The
   config directory of the override file (its parent folder) is used to
   resolve relative paths.
