.. _streaming-a-backend:

Streaming a backend into RoboVAST
=================================

A simulator, a middleware or a real-robot log joins RoboVAST by **writing files into the run
directory**. Nothing is registered for that: the decoder (``robovast-decode``, plain Python --
mcap, rosbags, pyarrow; no ROS, no execution image) reads what a run wrote and turns it into
tables, and everything above the tables -- SQL, plots, the MCP tools, notebooks, the run view
-- reads the tables. What a backend gets is decided by what it writes, in four levels. Each
level is reached by writing one more kind of file, and a backend stops wherever it likes.

The run directory is ``<campaign>/<config>/<run>/``, mounted in every container of the job
as ``/out/<config>/<run>``. A :ref:`simulator backend <simulators>` tells its simulator where
to write through its ``env`` hook -- roqsim asks for its recording with ``ROQSIM_RECORD``,
relative to that directory -- and a scenario or a postprocessing step writes there directly.

.. list-table::
   :header-rows: 1
   :widths: 6 30 64

   * - level
     - what is written
     - what it buys
   * - 1
     - a ``*.csv`` or ``*.jsonl`` file, flushed row by row
     - a table: SQL, declared plots, the MCP tools, notebooks, a query while the run goes
   * - 2
     - pose and joint rows in the tables the ``scene3d`` panel reads, plus a scene descriptor
     - the run view's 3D replay
   * - 3
     - an mcap recording, ``ros2msg`` channels or roqsim's JSON channels
     - bulk data (camera frames) beside the tables, and a run followed as it records
   * - 4
     - a command that answers for the run's present state
     - "now": what a backend says about a run before anything is written

Level 1: a file is a table
--------------------------

Every ``*.csv`` and ``*.jsonl`` below a run directory is a table, named after the file: its
stem, lower-cased, anything but ``[a-z0-9_]`` turned into ``_``, ``t_`` prefixed to a name
starting with a digit, and a name longer than 63 bytes shortened with a hash of the whole
(``out.csv`` -> ``out``, ``nav-metrics.csv`` -> ``nav_metrics``). Every table carries
``campaign_id``, ``config_name`` and ``run_id`` in its own rows.

**CSV.** The header line names the columns. A ``#`` preamble before it is skipped, which is
where a producer states what its columns mean. Column types are inferred from the values
(integer, real, text; an empty cell is ``NULL`` and contributes no type evidence). A table
carrying ``orientation.x/y/z/w`` gains ``orientation.yaw``.

**JSONL.** The first record's ``format`` field chooses the reader. The one format known is
scenario-execution's behaviour-tree log (``behavior_tree_log``, read into ``behaviors``); a
file of another format is not a table.

**Refused, per table and run, with the reason** (``describe_campaign_data`` reports it under
``failed``): two files of one run claiming one table; a CSV row with more fields than its
header; and a file whose name is a table the run's records already build -- a recording's
tables, ``runs``, ``_recording``, the derived tables, ``run_health`` and
``postprocessing_steps`` -- because its rows and the built ones would be one table twice.
Rename the file.

**Built the first time something names it**, and kept in the campaign's ``.cache/``
(:ref:`built on first use <results-table-cache>`). Something names it when a SQL statement reads it
(``query_campaign_data_sql``, ``POST /data/campaigns/{id}/query``), a declared plot queries it
(``visualization.results.data_browser.plots``), a run-view panel or a notebook reads it
(``open_data(dir).table("out")``, :doc:`analysis_notebooks`), or the campaign-end pass builds
what the campaign declares. ``describe_campaign_data`` lists the table with how many of its
runs it is built for, and its columns once it is built for one.

**While the run goes.** The manifest records the size of the file each run's table was built
from, and a file that has grown since is read again, whole, on the next request; the entry
is complete once the run has its ``test.xml``. So a producer that flushes each row makes it
visible to the next query, plot or panel -- read on request, not pushed: the stream that
follows a run as it records (``GET /data/campaigns/{id}/live``) carries the tables decoded
from its recordings and the derived tables, never a file.

**A pose file joins the pose tables.** Write the :ref:`pose contract <pose-contract>`'s
columns (``frame``, ``timestamp``, ``position.x/y/z``, ``orientation.x/y/z/w``, and
``wall_time`` where the producer can state one) and the file is a pose table like ``poses``
and ``sim_poses``: ``pose_track_view`` sums up its tracks, and ``get_track_deviation``
reads it by naming it as ``source``. ``timestamp`` is the run's one clock
(:ref:`one clock per run <run-clock>`): simulated seconds, the join key of every table of the run.

Level 2: pose and joint rows, and a scene descriptor
----------------------------------------------------

The run view's ``scene3d`` panel reads two things and nothing else (:ref:`scene-descriptor`):

* **motion**, from two tables: a pose table (``sim_poses`` by default: ``timestamp``,
  ``frame``, ``position.x/y/z``, ``orientation.x/y/z/w``, one row per body per sample) and a
  joint table (``joint_states`` by default: ``timestamp``, ``joint``, ``position``, one row
  per joint per sample). The rows address the geometry by **body name** and **joint name**,
  in the joint's own unit, so nothing is listed anywhere; a name matching nothing is
  reported. Poses are in the scene's frame, not a map frame. A campaign binds other tables
  under the panel's ``motion: {poses: <table>, joints: <table>}``.
* **geometry**, the scene descriptor: ``scene.json`` + ``scene.bin`` + a PNG per texture,
  static per world. The service compiles it on first view, in the campaign's own pinned
  image, through the backend's ``scene_export`` hook, and caches it by world identity
  (:ref:`how it is served <scene-descriptor-delivery>`).

Which world a run needs is read from the run's ``sim_recording`` row -- ``world``,
``overrides_json``, ``format_version`` -- by SQL over the campaign's tables (``SELECT * FROM
sim_recording WHERE config_name = ? AND run_id = ?``). The row is therefore whatever gives
that table: roqsim's recording (level 3), or, for a run with no simulator recording, a file
``sim_recording.csv`` with those columns beside the run's ``sim_poses.csv`` and
``joint_states.csv``. A run with no row has nothing to replay and no world to build, and the
panel says so.

Two hooks of the backend decide whether the panel appears at all. ``records_scene_state``
returning true is what makes the backend contribute a ``scene3d`` panel to every campaign's
run view (``default_panels``); a campaign can also declare ``- scene3d:`` under
``visualization.results.run_view.panels`` itself, which is a complete panel. A backend with
no ``scene_export`` is reported by the panel as one that exports no scene descriptor -- the
normal answer for a simulator RoboVAST merely launches -- and the panel names the campaign's
own ``execution.generate`` entry as the way to produce one.

A run whose motion is decoded from its recording is followed as the recording grows; one
whose motion is a file is read as the file is at each query the panel makes.

Level 3: a recording
--------------------

A recording is a directory of ``*.mcap`` segments below the run: ``rosbag2/``, the scenario
recording that ``ros2 bag record`` writes, or ``roqsim_bag/``, the simulator's own, beside or
instead of it. The decoder reads every segment in order, frames every message (the counts and
bytes per topic come from that) and decodes only what some table needs.

**How a channel is typed.** A channel with a schema is typed by the schema's name; one
without, by its message encoding. A ``ros2msg`` or ``ros2idl`` schema whose data carries the
definition is what decodes the channel's CDR bytes -- rosbag2 writes each topic's full
definition into the schema record, so a stack's own message types decode where the types were
never installed. A type the recording does not define is looked for in the
``message_definitions.json`` sidecar beside the bag, then among the distro's own types; a type
none of the three cover is listed in ``_recording`` as recorded and not tabulated, with that
reason. A ``json``-encoded channel needs no definition: its message is the parsed document.

**What becomes a table in** ``rosbag2/``: ``/tf`` + ``/tf_static`` -> ``poses``,
``/behavior_tree_log`` -> ``nav2_behavior_tree``, every occupancy grid -> ``costmaps``, every
action's feedback and status -> ``action_<name>_feedback`` / ``action_<name>_status``, and
every other topic that decodes -> ``rosbag2_<topic>``, one row per message with its fields as
columns. Image, compressed-image, point-cloud and laser-scan topics are **bulk data** and never
rows: a camera's frames are read from the recording itself, by stamp -- ``GET
/data/campaigns/{id}/frame?run=&topic=[&t=]`` and ``.../frame-index`` (:doc:`http_api`), and
the live stream's ``frames=`` -- and ``videos`` exists only where the campaign asks for it
(``rosbags_to_webm``). The full list, per table and column: the tables section of :doc:`results_processing`, and
:ref:`the decoder's configuration <results-decoder-config>` for what a campaign refines.

**What becomes a table in** ``roqsim_bag/``: the channels ``poses``, ``joints`` and ``clock``
-- JSON documents under ``jsonschema`` schemas -- and the metadata records ``roqsim.recording``
and ``roqsim.entities``:

* ``poses`` -> ``sim_poses``: each document is ``{"t": sim s, "w": epoch s, "bodies": {name:
  [x, y, z, qx, qy, qz, qw, vx, vy, vz, wx, wy, wz]}}``, one row per body; a body with other
  than thirteen values fails the table;
* ``joints`` -> ``joint_states``: ``{"t", "w", "q": {joint: position}}``, one row per joint;
* ``clock`` -> ``clock_map``: ``{"wall_ts": epoch s, "sim_ts": sim s}``, decimated to the
  samples that describe a rate change or a pause;
* ``roqsim.recording`` (a metadata record whose ``json`` value is the provenance document:
  ``format_version``, ``world``, ``overrides``, ``seed``, ``packages``, ``capture_fps`` as
  ``[num, den]``, ``timestep``, ``model``) -> ``sim_recording``, one row; written at start and
  again at close, and the last record wins;
* ``roqsim.entities`` (``{"entities": [{"name", "kind", "body", "present"}]}``) ->
  ``sim_entities``, the last roster written.

The ``state`` channel is not a table, and any other channel of that recording is listed in
``_recording`` as having no table defined. A second simulator that writes these channels in this
shape gets the ``Sim*`` tables and the 3D replay with no decoder change; one that writes
another shape reaches the same tables through level 1's files.

**One clock.** ``timestamp`` is the message's receive time in seconds, and every table of a
run joins on it. In roqsim's recording the receive time *is* the simulated time, taken inside
the simulator; in a ROS recording it is the bag's arrival time, and the topic's own stamp is
kept beside it. The wall <-> sim map that puts logs and resource samples on the same clock
comes from ``/clock`` in the job's infrastructure recording or from roqsim's ``clock`` channel
(:ref:`the clock map <clock-map>`); with it, the derived tables (``run_log``, ``scenario_timestamps``,
``resource_usage``, ``system_usage``, ``run_clock``) work for a stepped run with no ROS at all.

**Followed as it records.** A segment still being written is read up to its last complete
record, so the recording is readable from its first chunk; a run is *live* (``runs.live``)
while it has no ``test.xml`` and its campaign no terminal record. For a live run the service's
watcher keeps a session per ``(run, recording)`` that feeds each new record to the same
handlers a whole build uses, hands the rows to subscribers in batches (``GET
/data/campaigns/{id}/live``, which the run view follows) and writes them as parquet parts
under a ``live`` stamp, so a query during the run reads what landed so far. Once the run has
its verdict and the recorder its footer, the parts become the run's one file, equal to what
one pass over the finished recording gives. The derived tables are re-derived whole as the
job's files grow, on the same period. What the recording holds, topic by topic, is the
``_recording`` table (:ref:`its section <results-recording-table>`).

Level 4: now
------------

Everything above is read from files, which is why it needs no simulator installed where it
is read. What a backend says about a run *before* anything is written goes through the
backend itself, as two hooks the service runs inside the run's simulation container:

``health_command(cfg, execution, *, run_dir)``
   A fixed command whose JSON ``findings`` ride on ``get_campaign_status`` and
   ``get_job_state`` (:ref:`what a running campaign says is wrong <mcp-health-findings>`).
   The service polls it while somebody watches.
``tap_command(cfg, execution, *, run_dir, selection) -> list | None``
   The command a reader starts on demand to follow the run's present state: its stdout is
   relayed line by line for a bounded time (``tap_job`` on every surface, the run view's
   **Now** toggle) and recorded against the run as a probe. Argv, never a shell string. The
   ROS shape's default is ``ros2 topic echo`` of the selection; a backend whose recording is
   already the live view answers ``None``, as roqsim does (:doc:`simulators`, "Asking a live
   run").

Beside them ``simulation_screenshot`` re-renders one moment of one run from a chosen
viewpoint. "Now" is a hook of the backend, and everything else is a file.
