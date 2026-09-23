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

"""``LocalTransport`` — the execution lane over Docker on this host.

The lane behind ``vast serve --backend local``: one campaign at a time in containers this
process starts, and a screen to open a window on when the host has one. Everything the
lanes share lives in :class:`~robovast.service.service_base.ServiceBase`; what is here is
the local answer to each of its hooks, and the operations this lane declines -- a rank or
a hold, rolling a Deployment -- each refused by name with
:class:`~robovast.service.interface.UnsupportedOnLane`.

``client`` re-exports ``LocalTransport`` so existing imports keep working.
"""

import contextlib
import os
import subprocess
from pathlib import Path
from typing import Optional

from robovast.client.safe_path import safe_join
from robovast.common.config import SCENARIO_CONTAINER
from robovast.common.host_display import require_host_display
from robovast.execution.control_server import (STOP_ALREADY_OVER, STOP_RUNS,
                                               STOP_SCOPE_MESSAGES, Phase, stop_checker,
                                               stop_scope_for_phase)
from robovast.service.interface import (ActionResult, DiskSpace, ImageBuildRef, JobCounts,
                                        JobSummary, ListJobsResponse, LogChunk,
                                        ResourceUsage, UnsupportedOnLane, UpgradeInfo,
                                        VersionInfo)
from robovast.service.service_base import (_PROBE_LIMIT_S, NO_CAMPAIGN_QUEUE, ServiceBase,
                                           logger, require_scheduling_change)


class LocalTransport(ServiceBase):
    """In-process implementation over the local Docker backend.

    A campaign always runs a **workspace's** ``.vast``: ``workspace_id`` is the only
    project binding this service accepts (see :meth:`_resolve_project`), and
    ``config_path``/``vast_path`` selects among several ``.vast`` files in that
    workspace. Nothing ambient selects what the service runs; the results root is
    named by ``vast serve --results-dir`` (see
    :func:`~robovast.common.results_root.local_results_root`).
    """

    def __init__(self, store=None, workspace_dir=None, results_dir=None):
        super().__init__(store=store, workspace_dir=workspace_dir, results_dir=results_dir)
        # Prime psutil's non-blocking CPU sampler so the first resource_usage()
        # reading reflects real load instead of the 0.0 a cold sampler returns.
        import psutil  # pylint: disable=import-outside-toplevel
        psutil.cpu_percent(interval=None)

    LANE = "local"
    #: Local Docker is single-flight — the backend hardcodes container name
    #: ``robovast``, so two concurrent local campaigns would collide.
    _CONTAINER_NAME = "robovast"
    #: How long to wait, on shutdown, for a stopped campaign's worker thread to
    #: run its container teardown before we exit anyway. A hair over the backend's
    #: SIGTERM grace (``_STOP_GRACE_SECONDS`` = 15s) so the trap can complete.
    _SHUTDOWN_JOIN_SECONDS = 20

    def _postprocess_campaign(self, campaign_id: str, campaign_dir: Path, *,
                              force: bool = False, skip=(), state=None) -> tuple:
        """Run the campaign's own postprocessing pipeline; return ``(ok, message)``.

        One call for both callers -- the ``run_postprocessing`` retrigger and the chain an
        import starts -- so a raw archive taken in is postprocessed exactly the way asking
        for it later would be.

        ``campaign`` scopes the work to this campaign; with no ``vast_file`` the run reads
        the campaign's own ``_config/<name>.vast``. ``output_callback`` is what puts the
        step-by-step narrative ("[2/4] Executing: …", "✓ …") into whichever campaign log
        handler the caller opened. Without it those lines default to ``print`` and land on
        the service's stdout, so the phase file held only what modules logged themselves --
        the campaign log looked empty for the run you had just asked for.

        With *state*, those same lines also become the live ``stage`` marker -- see
        :func:`~robovast.execution.control_server.stage_output_callback`. Both callers have
        one, and both are watched from the campaign view, so a re-run narrates itself there
        exactly as an auto-chained run does; without it a retrigger was the case where the
        view showed ``postprocessing`` and nothing else for the whole run.
        """
        from robovast.execution.control_server import \
            stage_output_callback  # pylint: disable=import-outside-toplevel
        from robovast.results_processing.postprocessing import \
            run_postprocessing  # pylint: disable=import-outside-toplevel
        return run_postprocessing(
            results_dir=str(campaign_dir.parent), campaign=campaign_id,
            force=force, skip=list(skip),
            output_callback=stage_output_callback(state, logger.info),
            # A re-run is a tracked campaign like any other while it is going, so
            # ``stop_campaign`` reaches it -- and with this, ends it.
            should_stop=stop_checker(state))
    def version(self) -> VersionInfo:
        # The filesystem roots are advertised because this lane *is* local disk, so a
        # caller on the same host can read results with its own tools instead of
        # relaying every byte through the interface. This answers only half the
        # contract -- whether the *service* has openable paths. ``app.py``'s version
        # route answers the other half and blanks them for a non-loopback caller,
        # which a transport cannot see.
        # `can_build_images=True` unconditionally, and deliberately **without probing
        # Docker**. This lane builds with `docker buildx --load` into the local daemon:
        # there is no registry, no Ingress and nothing an operator can misconfigure, so
        # the capability is a property of the lane rather than of this deployment. A dead
        # daemon is *liveness* — `resource_usage`, the run preflight and `vast doctor`
        # each answer that — and asking the daemon means shelling out with a timeout,
        # which is the last thing this call should ever wait on. `_api_server_url` in the
        # cluster lane's version() refuses to dial for the same reason.
        return self._version_info(backend="docker", can_build_images=True,
                                  # The same predicate ``_admit_scheduling`` refuses on, so
                                  # what a client is offered and what the service accepts
                                  # cannot disagree.
                                  can_schedule=self._queues_campaigns(),
                                  results_root=str(self._campaigns_root()),
                                  sources_root=str(self.store.registry.root))
    def upgrade_info(self) -> UpgradeInfo:
        """The live campaigns, and a refusal: there is no Deployment here to roll.

        The refusal names how this deployment *is* updated rather than reporting a
        capability it does not have: a local service is however it was installed and
        started. Each lane composes its own answer over :meth:`_active_campaigns`, so a
        lane that cannot roll always carries a *reason* of its own, never one borrowed from
        another lane and corrected.
        """
        return UpgradeInfo(
            supported=False,
            unsupported_reason=(
                "this service is not a Kubernetes Deployment, so it has nothing to roll. "
                "Update it the way it was installed and restart it."),
            active_campaigns=self._active_campaigns())
    def upgrade_service(self, force: bool = False) -> ActionResult:
        """Refuse: see :meth:`upgrade_info`.

        :class:`UnsupportedOnLane` -> 501. Not the 409 the live-campaign refusal uses: that
        one is a conflict the caller can resolve and retry, this one is a request that does
        not apply to this lane at all.
        """
        del force  # a refusal about the lane; forcing does not make a lane something else
        raise UnsupportedOnLane("upgrade_service", self.LANE,
                                hint=self.upgrade_info().unsupported_reason)
    def _compute_resource_usage(self) -> ResourceUsage:
        """Local host capacity + live utilization via ``psutil``.

        ``cpu_percent(interval=None)`` is non-blocking — it averages CPU load since
        the previous call rather than sleeping per request. The first reading after
        the process starts is ``0.0`` (no prior sample); the TTL cache means that is
        replaced by a real value on the next window. Overridden by
        :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`.

        This lane fills the ``*_measured`` fields and leaves ``*_reserved`` ``None``: it
        starts run containers without cpu/memory limits, one at a time, so it has a
        measurement and no reservation. See :class:`ResourceUsage`.
        """
        import psutil  # pylint: disable=import-outside-toplevel
        vm = psutil.virtual_memory()
        cores = psutil.cpu_count(logical=True)
        # Read ONCE and reused below. `cpu_percent(interval=None)` is stateful: it averages
        # since the previous call, so asking twice in one reading answers the second with
        # roughly zero -- `cpu_used` and `cpu_measured` are the same reading, not two.
        cpu_measured = cores * psutil.cpu_percent(interval=None) / 100.0
        jobs_running, jobs_pending = self._scenario_job_tally()
        disk, disk_unavailable = self._disk_space()
        return ResourceUsage(
            backend="docker",
            cpu_capacity=float(cores),
            cpu_used=cpu_measured,
            memory_capacity_bytes=vm.total,
            memory_used_bytes=vm.used,
            # This lane MEASURES, and reserves nothing: run containers are started without
            # cpu or memory limits and one at a time, so there is no reservation to report.
            # `cpu_reserved` stays None rather than echoing the measurement, which would
            # label consumption as a commitment -- the chart then draws one series here,
            # which is the truth about this lane and not missing data.
            cpu_measured=cpu_measured,
            memory_measured_bytes=vm.used,
            parallel_runs=False,   # Docker backend is single-flight: runs are sequential
            jobs_running=jobs_running,
            jobs_pending=jobs_pending,
            disk=disk,
            disk_unavailable=disk_unavailable,
            # `store` stays None: this lane's results store IS the filesystem `disk`
            # already reports, and a second identical meter would say nothing.
        )
    def _disk_space(self) -> "tuple[Optional[DiskSpace], Optional[str]]":
        """This host's results filesystem, or the reason it could not be read.

        The filesystem holding :meth:`_campaigns_root`, not ``/``: a campaign writes its
        rosbags and CSVs there, and where that is a separate mount -- a data disk, an NFS
        export -- ``/`` can look comfortable while the disk the next campaign needs is
        full. (The Docker graph dir is the other thing that fills, from image pulls; it is
        normally the same device, and a second disk would need a second meter.)

        Resolved to the nearest existing ancestor because ``_campaigns_root`` is a pure
        path resolver -- the directory is materialized lazily on the first run, so a
        service that has never run a campaign would otherwise fail to read the very disk it
        is about to write to. Same filesystem either way, unless the missing component is
        itself an unmounted mountpoint.

        Capacity is ``used + free`` rather than the filesystem's total, as on the cluster
        lane: a filesystem holds blocks back for root, and ``total - used`` would count those
        as room a campaign's writes could use. What is left is what ``free`` says.
        """
        import psutil  # pylint: disable=import-outside-toplevel
        path = self._campaigns_root()
        while not path.exists() and path != path.parent:
            path = path.parent
        try:
            usage = psutil.disk_usage(str(path))
        except OSError as e:
            logger.debug("could not read disk usage for %s: %s", path, e)
            return None, f"could not read the results filesystem: {e}"
        return DiskSpace(capacity_bytes=usage.used + usage.free, used_bytes=usage.used), None
    def _scenario_job_tally(self) -> "tuple[int, int]":
        """``(running, pending)`` scenario runs across this lane's live campaigns.

        Read from the controller snapshot, not from disk: :meth:`list_jobs` discovers
        runs as ``<config>/<run>/`` directories and calls any without a ``test.xml``
        ``running`` while the campaign is live, so a run that died without writing one
        would be reported as still executing for the rest of the campaign. The
        snapshot is what the controller actually believes, and costs no I/O.

        ``running`` is 0 or 1 by construction, not by clamping: this lane is
        single-flight (``parallel_runs=False`` — the Docker backend hardcodes one
        container name), so a batch has at most one run executing, and the phases
        before ``running`` (``initializing``/``building``/``variation``) have none.
        ``pending`` is the rest of the current batch — accepted work that is not
        executing, the same population the cluster lane's ``waiting``+``pending`` Jobs
        are, so a consumer can read the pair without knowing which lane answered.

        Summed over live campaigns even though :meth:`_guard_new_campaign` admits one
        at a time, so this stays correct if that guard ever relaxes.
        """
        with self._lock:
            entries = [e for e in self._campaigns.values() if not self._is_done(e)]
        running = pending = 0
        for entry in entries:
            snap = entry.state.snapshot()
            active = 1 if snap.phase == Phase.RUNNING else 0
            total = snap.runs.total if snap.runs else 0
            done = snap.runs.completed if snap.runs else 0
            running += active
            pending += max(0, total - done - active)
        return running, pending
    def _guard_new_campaign(self) -> None:
        """Reject a launch this deployment cannot run concurrently.

        Local Docker is single-flight (the backend hardcodes the ``robovast``
        container name), so two concurrent local campaigns would collide. The
        cluster service overrides this to a no-op: its campaigns are I/O-bound
        drivers whose compute lives in Kubernetes Jobs, so they run in parallel.
        """
        with self._lock:
            if any(not self._is_done(c) for c in self._campaigns.values()):
                raise RuntimeError(
                    "A local campaign is already running (local Docker is "
                    "single-flight). Stop it before starting another.")
    def _build_backend(self, state):
        """The :class:`ExecutionBackend` this deployment runs campaigns on."""
        from robovast.execution.backends import DockerBackend
        return DockerBackend(state=state)
    def _admit_show_gui(self, request) -> None:
        """Admit ``show_gui``: this lane's ``docker`` process sits at the serve host's
        display, so a window can open -- provided there is a display to open it on.

        Called at request admission, before an image build or a campaign directory exists,
        so a refusal leaves nothing behind. Accepting it and rendering nowhere is the
        failure this prevents: the run looks fine and simply never draws. A missing display
        is the environment, not the lane, so it stays a ``ValueError``.
        """
        if not getattr(request, "show_gui", False):
            return
        require_host_display(what="show_gui")
    def _queues_campaigns(self) -> bool:
        """False: this lane executes one campaign at a time, so there is nothing to order."""
        return False

    def _admit_scheduling(self, request) -> None:
        """Refuse a rank or a hold at launch: this lane runs one campaign at a time, so it
        has no queue to apply either to.

        Called at request admission, before a campaign directory exists, so a refusal leaves
        nothing behind. Accepting it would be the worse failure: the campaign would run at the
        ordinary time and nothing would ever say that the rank it was given did nothing.

        The default asks for nothing and is admitted everywhere, which is what keeps a launch
        that never mentions scheduling working on both lanes.
        """
        asked = [name for name in ("priority", "paused") if getattr(request, name, None)]
        if asked and not self._queues_campaigns():
            raise UnsupportedOnLane(" and ".join(asked), self.LANE, hint=NO_CAMPAIGN_QUEUE)
    def _run_options(self, request) -> "RunOptions":  # noqa: F821
        from robovast.execution.backends import RunOptions

        # Local backend: upload_to_share just writes a tar.gz to _archives/ (no
        # external provider). Honour the toggle so it works for a local run too.
        # ``show_gui`` -> ``gui`` is the one place the request's outward name meets the
        # run machinery's: the generated run.sh's flag is ``--no-gui`` and cannot be
        # renamed with it, so the boundary is here rather than spread over both.
        return RunOptions(gui=bool(getattr(request, "show_gui", False)),
                          upload_to_share=bool(getattr(request, "upload_to_share", False)),
                          image_project=getattr(request, "image_project", "") or None,
                          image_project_tag=getattr(request, "image_project_tag", "") or None)
    def _aux_runner_context(self, tag: str, project, *, hold: bool = False,
                            should_stop=None):
        """How this lane provides a variation's auxiliary container, for one span.

        A context manager: entered in the thread that composes, because the factory it
        installs is a ContextVar and must be scoped to exactly that composition. *tag*
        names the span (a campaign id, or a digest identifying a previewed project) so a
        lane that creates something per span can name it.

        *hold* is who owns the container's death. False — a campaign — means the span does:
        it is torn down when the run ends, which is also what makes per-campaign cleanup
        able to find it. True — an interactive caller such as ``preview_configurations`` —
        means the container outlives the span and is reaped on idleness instead, because an
        authoring loop composes the same file repeatedly and would otherwise pay a cold
        start every time. An unbounded span is the one that needs a reaper.

        No factory is installed locally, and deliberately: with none,
        ``_make_container_runner`` falls back to an ephemeral ``docker run`` on the service
        host, which is what a local service — and the CLI, which has no transport at all —
        already wants, and where holding would buy about a second. The cluster lane
        overrides this, having no ``docker`` in the pod and a pull to amortize.

        What the span does register is *should_stop*, when a campaign passes one: the
        fallback hands it to the runner it builds, so a campaign stopped while a variation
        is waiting on a helper image removes that container instead of waiting it out. A
        preview passes none and its containers run to their own end.
        """
        del hold
        if should_stop is None:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def _span():
            from robovast.common.config_generation import set_aux_stop_predicate
            token = set_aux_stop_predicate(should_stop)
            logger.debug("Auxiliary containers of %s are stoppable for this span "
                         "(project %s)", tag, getattr(project, "config_path", ""))
            try:
                yield
            finally:
                # Restored rather than cleared, for the reason ``_reset_factory`` gives
                # about the factory beside it: a span entered inside another must hand the
                # outer one its predicate back, not disarm it.
                try:
                    token.var.reset(token)
                except (AttributeError, LookupError, ValueError):
                    set_aux_stop_predicate(None)

        return _span()
    def _postprocess_in_process(self) -> bool:
        """True when the worker runs analysis postprocessing after the loop.

        Local does (in-process). The cluster service instead chains it *inside* the
        builder (``RunOptions.postprocess``) so ``data.db`` rides the campaign's
        existing upload rather than needing one of its own.
        """
        return True
    @property
    def _images(self):
        """This lane's image store — where its built experiment images live.

        The one member a lane overrides about images, and it is a **factory, not
        behavior**: everything that consumes the store is written once, here, so a lane
        cannot answer an image question wrongly by forgetting to override the method that
        asks it. Without the seam, :meth:`_exec_image` asks the local docker daemon on a
        lane whose images live in a registry, inside a pod with no docker at all, and
        reports every built image as unbuilt.
        """
        store = getattr(self, "_image_store", None)
        if store is None:
            from robovast.service.image_store import LocalDockerImageStore
            root = os.environ.get("ROBOVAST_BUILDS_ROOT")
            log_root = Path(root) if root else Path.home() / ".robovast" / "builds"
            store = LocalDockerImageStore(log_root)
            self._image_store = store
        return store
    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None, should_stop=None) -> list:
        """Submit (or join) each container's image build; return their refs.

        Empty when nothing needs building. Returns as soon as each build has a
        *handle* — it does not wait; :meth:`_await_build_image` does that, on the
        campaign's own worker thread. Overridden by :class:`ClusterService` for the
        in-cluster BuildKit Job.

        *should_stop* reaches the plugin install inside :meth:`_build_specs_for`, which is
        the long step here and the one a stop has to be able to end.
        """
        specs, project_dir = self._build_specs_for(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag, should_stop=should_stop)
        return [self._images.start(spec, project_dir)
                for spec in specs.values()]
    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None, should_stop=None) -> dict:
        """Concrete image refs to pin once the builds are done, by container name."""
        specs, project_dir = self._build_specs_for(
            project, campaign_config, image_project=image_project,
            image_project_tag=image_project_tag, should_stop=should_stop)
        return {name: self._images.ref_for(spec, project_dir).ref
                for name, spec in specs.items()}
    def build_image(self, request) -> "ImageBuildRef":  # noqa: F821
        from robovast.common.common import load_config
        from robovast.common.config import validate_config
        from robovast.service.image_build import primary_build_ref, validate_build_spec
        self._admit_storage("build an image")
        project = self._resolve_project(request.workspace_id, request.config_path)
        campaign_config = validate_config(load_config(project.config_path))
        specs, project_dir = self._build_specs_for(project, campaign_config)
        if not specs:
            raise ValueError(
                "nothing to build: no container adds system_packages, "
                "python_packages or ros_packages, so every image is used as declared")
        wanted = request.container
        if wanted:
            if wanted not in specs:
                raise ValueError(
                    f"container '{wanted}' builds no image; the ones that do are: "
                    + ", ".join(sorted(specs)))
            specs = {wanted: specs[wanted]}
        for name, spec in specs.items():
            problems = validate_build_spec(spec, project_dir)
            if problems:
                raise ValueError(f"invalid execution.containers.{name}:\n  - "
                                 + "\n  - ".join(problems))
        refs = {name: self._images.start(spec, project_dir)
                for name, spec in specs.items()}
        return primary_build_ref(refs)
    def get_image_build_status(self, build_id: str):
        return self._images.status(build_id)
    def get_image_build_log(self, build_id: str, offset: int = 0):
        return self._images.log(build_id, offset)
    def _exec_lane(self):
        from robovast.service.docker_exec_lane import DockerExecLane
        return DockerExecLane()
    def _scheduling_for(self, campaign_id: str, *, live: bool) -> dict:
        """``{"priority", "paused"}`` for a listing row. The defaults here: no queue.

        :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`
        overrides it with what its queue actually holds. Reported as a pair so a row that
        admits nothing says which of the two reasons it is.
        """
        del campaign_id, live
        return {"priority": 0, "paused": False}
    def list_jobs(self, campaign_id: str) -> ListJobsResponse:
        """List the campaign's runs (local Docker fans a batch out into runs).

        Runs are discovered on disk as ``<config>/<run-number>`` directories (the
        same layout :func:`get_vast_configuration_info` reads); a run is
        ``completed``/``failed`` by its ``test.xml`` result, ``killed`` when an operator
        stopped its job, or ``running`` when the campaign is still live and the run has
        not produced one yet (local is sequential, so at most one). Pending
        (not-yet-started) runs have no directory, so they are counted from the
        controller's expected total but not listed.

        The kill has to be consulted because "running" here means *no ``test.xml`` yet* —
        and a killed run's ``test.xml`` is precisely the thing that never arrives. Without
        it the job stayed ``running`` for the rest of the campaign's life, keeping a row in
        the live Jobs list and a Stop button on a job that was already dead.
        """
        from robovast.common.campaign_data import (killed_failure_message, killed_runs,
                                                   read_test_result)
        campaign_dir = self._campaigns_root() / campaign_id
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        live = entry is not None and not self._is_done(entry)
        # One read for the whole listing, and `{}` for every campaign nobody intervened in.
        killed = killed_runs(campaign_dir)

        jobs: list[JobSummary] = []
        if campaign_dir.is_dir():
            config_dirs = sorted(
                d for d in campaign_dir.iterdir()
                if d.is_dir() and d.name not in self._RESERVED_DIRS
                and not d.name.startswith("."))
            for config_dir in config_dirs:
                run_dirs = sorted(
                    (d for d in config_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                    key=lambda d: int(d.name))
                for run_dir in run_dirs:
                    job_name = f"{config_dir.name}/{run_dir.name}"
                    detail = None
                    try:
                        status = "completed" if read_test_result(run_dir)["success"] \
                            else "failed"
                    except FileNotFoundError:
                        # Same precedence as ``read_run_outcome``: a kill only explains a
                        # run that delivered nothing. One that wrote a ``test.xml`` before
                        # the kill landed keeps the verdict it earned, above.
                        entry_killed = killed.get(job_name)
                        if entry_killed is not None:
                            status = "killed"
                            detail = killed_failure_message(entry_killed)
                        else:
                            status = "running" if live else "failed"
                    jobs.append(JobSummary(
                        job_name=job_name,
                        status=status,
                        detail=detail,
                        display_name=f"{config_dir.name} · run {run_dir.name}"))

        expected_total = 0
        if entry is not None:
            snap = entry.state.snapshot()
            expected_total = snap.runs.total if snap.runs else 0
        pending = max(0, expected_total - len(jobs)) if live else 0
        counts = JobCounts(
            running=sum(1 for j in jobs if j.status == "running"),
            pending=pending,
            completed=sum(1 for j in jobs if j.status == "completed"),
            failed=sum(1 for j in jobs if j.status == "failed"),
            killed=sum(1 for j in jobs if j.status == "killed"),
            total=len(jobs) + pending)
        return ListJobsResponse(jobs=jobs, counts=counts)
    def _new_job_log_tail(self, campaign_id: str, job_name: str):
        """Build this lane's tail for a job. Overridden by the cluster lane."""
        from robovast.service.local_job_log import LocalJobLogTail
        return LocalJobLogTail()
    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0) -> LogChunk:
        """Serve a run's live container logs, merged (``job_name`` = ``<config>/<run>``).

        The containers write these files in place as they execute, so the same read
        serves a running and a finished run.

        **All** of the job's containers, not just the main one: the ROS shape runs the
        simulator and the system under test in their own containers, which write
        ``logs/system_<name>.log`` beside the main container's ``logs/system.log``. Reading
        only the latter shows scenario-execution and neither the simulator nor nav2 -- the
        two whose output explains a failed run. See
        :class:`~robovast.service.local_job_log.LocalJobLogTail` for how concurrent files
        are merged without breaking the byte-offset contract.

        The containers write to the JOB's artifact dir (``_jobs[/<batch>]/job-<j>``),
        not to the config/run dir -- ``<config>/<run>/logs/`` exists but stays empty, so
        reading there returns a silently blank job log even though the same output is
        visible in the campaign log (the local backend also folds container stdout into
        ``controller.log``). :func:`job_artifact_dir` resolves the real dir.

        ``eof`` needs the run finished **and** a settled poll. A sidecar flushes during
        compose's stop grace, i.e. after the main container wrote ``test.xml``, so ending
        the stream on ``test.xml`` alone would close the panel on exactly the shutdown
        output that says whether the simulator saved its recording.
        """
        from robovast.client.safe_path import UnsafePathError
        from robovast.common.campaign_data import read_test_result
        from robovast.common.execution import job_artifact_dir
        campaign_dir = self._campaigns_root() / campaign_id
        # job_name comes from a client, so confine it to the campaign (shared check).
        try:
            run_dir = safe_join(campaign_dir, job_name)
        except UnsafePathError as e:
            raise KeyError(str(e)) from e
        if not run_dir.is_dir():
            raise KeyError(f"job {job_name!r} not found in campaign {campaign_id!r}")
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        live = entry is not None and not self._is_done(entry)
        try:
            read_test_result(run_dir)
            run_done = True
        except FileNotFoundError:
            run_done = False
        try:
            job_dir = Path(job_artifact_dir(campaign_dir, job_name))
        except FileNotFoundError:
            # Before the first job starts there is no manifest yet; that is the
            # documented startup race, not a broken layout.
            return LogChunk(text="", next_offset=offset, eof=run_done or not live)
        finished = run_done or not live
        tail = self._job_log_tail(campaign_id, job_name)
        with tail.lock:
            grew = tail.read(job_dir / "logs", flush_partial=finished)
            text, next_offset = tail.merged.slice_from(offset)
        return LogChunk(text=text, next_offset=next_offset,
                        eof=(not live) or (finished and not grew))
    def set_campaign_scheduling(self, campaign_id: str, priority=None, paused=None) -> ActionResult:
        """Refuse: this lane runs one campaign at a time, so there is nothing to order.

        Refused even for the default rank, unlike the launch path: a launch that never
        mentions scheduling is an ordinary launch, whereas *asking* for a rank here is asking
        for something this lane cannot do, whatever the value.
        :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService`
        overrides this with the real thing.
        """
        del campaign_id
        require_scheduling_change(priority, paused)
        raise UnsupportedOnLane("set_campaign_scheduling", self.LANE, hint=NO_CAMPAIGN_QUEUE)
    def stop(self, campaign_id: str) -> ActionResult:
        """Request a cooperative stop and kill the compute so the worker unblocks.

        A campaign still in ``building`` is stopped by the flag alone: the teardown below
        removes the *scenario* container and cannot reach a ``docker buildx`` build thread.
        That is deliberate and must stay true — an image build is content-addressed and
        therefore shared, so cancelling it could strand a sibling campaign waiting on the
        same image, and the image is a cache entry rather than this campaign's property.
        ``_await_build_image`` detaches instead (see its ``CampaignStopped`` path).

        **What a stop lands on depends on what is running**, and
        :func:`~robovast.execution.control_server.stop_scope_for_phase` is what decides --
        the campaign's runs, its postprocessing, or its upload to share. The reply says
        which, because the three leave different things behind, and a campaign that is
        already over is refused rather than told a stop was requested.
        """
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            return ActionResult(ok=False, message=f"campaign {campaign_id} not tracked here")
        phase = entry.state.snapshot().phase
        scope = stop_scope_for_phase(phase)
        if scope is None:
            return ActionResult(ok=False, message=STOP_ALREADY_OVER.format(phase=phase))
        entry.state.request_stop(scope)
        # Only where a run is what is being stopped: during postprocessing or an upload
        # there is no scenario container, and the pipeline/upload poll their own scope.
        if scope == STOP_RUNS:
            self._kill_scenario_container()
        return ActionResult(
            ok=True,
            message=STOP_SCOPE_MESSAGES.get(scope, "stop requested"))
    def stop_job(self, campaign_id: str, job_name: str,
                 reason: "str | None" = None, source: str = "api") -> ActionResult:
        """Kill the running job's scenario container; the run loop moves to the next run.

        Deliberately **not** ``request_stop()``: that flag is what ends the campaign, and
        leaving it clear is the whole difference between this and :meth:`stop`. The
        generated ``run.sh`` runs one ``docker compose up``/``down`` cycle per job, so
        removing the container makes only *that* cycle exit non-zero and the loop proceeds
        (see :class:`~robovast.execution.backends.DockerBackend`).

        The named job must be the one actually in flight. This lane is single-flight
        behind a fixed container name, so the kill lands on whichever job is current
        regardless of what was asked for — accepting a stale name would report success for
        killing a *different* run than the caller named, which is the one outcome worse
        than refusing.
        """
        from robovast.common.campaign_data import KIND_KILLED, record_intervention
        from robovast.common.execution import job_artifact_dir
        campaign_dir = self._campaigns_root() / campaign_id
        with self._lock:
            entry = self._campaigns.get(campaign_id)
        if entry is None:
            raise KeyError(f"campaign {campaign_id!r} not tracked here")
        job = self._require_running_job(campaign_id, job_name)
        # Recorded before the kill, not after: the container dies asynchronously and a
        # crash in between would leave a dead run with no explanation for why it stopped.
        try:
            job_dir = os.path.relpath(job_artifact_dir(campaign_dir, job_name),
                                      campaign_dir)
        except (FileNotFoundError, OSError, ValueError):
            # No manifest entry yet (the documented startup race). The run key below is
            # this lane's own job identity, so resolution does not depend on it.
            job_dir = ""
        record_intervention(campaign_dir, kind=KIND_KILLED, job_dir=job_dir, job_name=job_name,
                            source=source, detail=reason, runs=(job_name,))
        self._kill_scenario_container()
        return ActionResult(
            ok=True,
            message=(f"killed job {job.display_name or job_name}; the campaign continues "
                     f"with its remaining runs and this run is recorded as 'killed'"))
    def _job_output_dir(self, campaign_id: str, job_name: str, run_dir: str) -> str:
        """Where this job writes its **job-level** artifacts, which is not where its runs are.

        The two are different subtrees and conflating them is why two reads failed at once:
        ``behaviors.jsonl`` and the simulator's records are per RUN
        (``<config>/<run>/``), while the logs, the sysinfo and the resource monitor's CSVs are
        per JOB, under ``_jobs/<batch>/job-<idx>/`` -- as
        :func:`~robovast.common.execution.job_artifact_dir` says outright: *"never into the run
        dir, so ``<config>/<run>/logs/`` stays empty and reading there yields a silently blank
        log."* One path for both meant whichever read matched the path worked and the other
        reported the run had written nothing.

        Resolved through the manifest, which is written before the first job starts, so a RUNNING
        job resolves. Falls back to *run_dir* when it cannot be resolved -- the read that follows
        then reports finding nothing, which is the honest outcome and names the directory it
        looked in.
        """
        from robovast.common.execution import job_artifact_dir
        try:
            rel = job_artifact_dir(self._campaigns_root() / campaign_id, job_name)
        except Exception as err:  # noqa: BLE001 - the documented startup race, among others
            logger.debug("no job artifact dir for %s of %s: %s", job_name, campaign_id, err)
            return run_dir
        rel = str(rel).strip("/")
        return f"/out/{rel}" if rel else run_dir
    def _job_live_run(self, campaign_id: str, job_name: str, target, run_dir: str) -> tuple:
        """``(run_dir, run_key)`` for the run this job is working on **right now**.

        Locally a job *is* one run, so this is what the target already said. The hook exists for
        the cluster, where a Job may pack several runs -- and the packer runs them
        **sequentially** (see ``KubernetesBackend``), so at any instant exactly one of them is
        live and "where is this job" has a single right answer. Which is the point: pointing the
        readers at the Job's whole ``/out`` let each of them pick a run for itself, silently, and
        a caller could not tell which run it had been told about.
        """
        del campaign_id, target, run_dir
        return f"/out/{job_name}", job_name
    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        """``(target, run_dir)`` for one running job's *role* — the lane-specific part of a read.

        Locally *job_name* **is** the run key (``<config>/<run>``), which is also where the run
        writes inside the container: ``/out/<config>/<run>``, the same path both lanes mount.
        Derived from the run rather than read from ``RUN_OUTPUT_DIR``, which the backends set only
        for a job that is exactly one run and so is absent from a packed one.

        The run dir does not depend on the role: ``/out`` is mounted into every container of a run,
        which is what lets a sidecar be asked about the run being written under it.
        """
        return self._job_container(role, campaign_id), f"/out/{job_name}"
    def exec_in_job(self, campaign_id: str, job_name: str, command: str,
                    container: str = "scenario", source: str = "api") -> "ExecResult":
        """Run *command* in the live job's container, recording the probe first.

        Locally the scenario runs in a container of a fixed name and every sidecar takes its role's
        name, which is the same mapping ``logs/system_<name>.log`` follows.

        Run in the run's own environment, not a bare login shell: ``ros2`` and everything else
        colcon-built lives in an overlay no shell rc sources, so ``ros2 topic list`` -- the single
        most likely thing to type here -- answered ``command not found``.
        """
        from robovast.common.campaign_data import KIND_PROBED, record_intervention
        from robovast.common.execution import in_run_env, job_artifact_dir
        from robovast.service.interface import ExecResult

        if not (command or "").strip():
            raise ValueError("exec_in_job needs a command: there is no scenario to start here, "
                             "only a live job to look at.")
        campaign_dir = self._campaigns_root() / campaign_id
        self._require_running_job(campaign_id, job_name)
        try:
            job_dir = os.path.relpath(job_artifact_dir(campaign_dir, job_name), campaign_dir)
        except (FileNotFoundError, OSError, ValueError):
            # The documented startup race, as in stop_job: no manifest entry yet. The run key below
            # is this lane's own job identity, so resolution does not depend on it.
            job_dir = ""
        # Before the command, not after: it may change the run or wedge it, and a crash in between
        # must not leave perturbed data with nothing saying why.
        record_intervention(campaign_dir, kind=KIND_PROBED, job_dir=job_dir, job_name=job_name,
                            source=source, detail=command, runs=(job_name,))
        target = self._job_container(container, campaign_id)
        exit_code, stdout, stderr, timed_out = self._exec_lane().exec_in(
            target, in_run_env(command), _PROBE_LIMIT_S)
        return ExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                          timed_out=timed_out, limit_s=_PROBE_LIMIT_S, limit_source="command")
    def _job_container(self, role: str, campaign_id: str = "") -> str:
        """The container a role runs in on this lane.

        The scenario's container has a fixed name; a sidecar's is its role. Mapped rather than taken
        verbatim so a caller names the role it means and the lane resolves it -- the cluster answers
        the same question with a pod and a container, which is why the callers never build one.

        With a *campaign_id* the mapping comes from that campaign's own container plan, which is the
        only thing that knows how many containers back a role: a simulator stepped in-process **is**
        the scenario container, so ``simulation`` must resolve to it rather than to a name nothing
        started. Without one -- a caller that has no campaign in hand -- the role's own name is the
        best available answer, and a role this campaign does not have fails on the exec rather than
        here, which is the same outcome as before.
        """
        from robovast.common.config import CONTAINER_ROLES
        if role not in CONTAINER_ROLES:
            raise ValueError(f"unknown container role {role!r}; expected one of "
                             f"{', '.join(CONTAINER_ROLES)}")
        if campaign_id:
            role = self._plan_role(campaign_id, role)
        return self._CONTAINER_NAME if role == SCENARIO_CONTAINER else role
    def _kill_scenario_container(self) -> None:
        """Force-remove the single-flight scenario container so the worker unblocks.

        The backend's run script (in its own session) is blocked on ``docker
        compose``; removing the container makes it return promptly, then its
        ``stop_requested`` poll tears the rest down. Best-effort — a missing
        container is fine.
        """
        try:
            subprocess.run(["docker", "rm", "-f", self._CONTAINER_NAME],  # noqa: S603,S607
                           check=False, capture_output=True)
        except OSError as e:
            logger.warning("docker rm -f %s failed: %s", self._CONTAINER_NAME, e)
    def _shutdown_running_campaigns(self, running) -> None:
        """End *running* before this process exits: nothing comes back for them.

        A local campaign's compute is containers this process started, so exiting has to
        tear them down or they are orphaned. The cooperative flag alone is not enough -- a
        worker blocked in ``run_batch`` must have its compute killed to return -- and one
        scenario container backs whichever campaign is running (single-flight), so a single
        force-remove covers them all. The workers are then joined briefly so their
        container-teardown traps complete before the process exits.

        What exiting means for a lane's running campaigns is a property of the **lane**,
        which is why each answers this hook for itself: a cluster campaign's compute
        outlives any one service process and is left for the successor to adopt, and
        nothing about that decision may be inherited from here.
        """
        logger.info("Shutting down — stopping %d running campaign(s)", len(running))
        for entry in running:
            # The run scope: what this is for is ending the campaign so its container
            # teardown runs before the process exits.
            entry.state.request_stop(STOP_RUNS)
        self._kill_scenario_container()
        for entry in running:
            if entry.thread is not None:
                entry.thread.join(timeout=self._SHUTDOWN_JOIN_SECONDS)
                if entry.thread.is_alive():
                    logger.warning(
                        "Campaign %s did not stop within %ds; exiting anyway",
                        entry.campaign_id, self._SHUTDOWN_JOIN_SECONDS)
    def run_postprocessing(self, request) -> ActionResult:
        self._admit_storage(f"postprocess {request.campaign_id}")
        campaign_dir = self.campaign_dir(request.campaign_id)

        def work(state):
            from robovast.client.logging_config import (add_campaign_log_handler,
                                                        remove_campaign_log_handler)
            from robovast.execution.status_recovery import record_step_outcome
            handler = None
            try:
                handler = add_campaign_log_handler(
                    str(campaign_dir / "_execution" / "postprocessing.log"))
            except Exception:  # pylint: disable=broad-except
                logger.warning("Could not open postprocessing.log for %s",
                               request.campaign_id, exc_info=True)
            try:
                ok, message = self._postprocess_campaign(
                    request.campaign_id, campaign_dir,
                    force=request.force, skip=list(request.skip or []), state=state)
            finally:
                remove_campaign_log_handler(handler)
            status = record_step_outcome(campaign_dir, postprocessing=(ok, message))
            state.update(postprocessed=status.postprocessed,
                         postprocessing_error=status.postprocessing_error)
            # The recorded phase, not `finished`: `record_step_outcome` preserves how the
            # campaign ended, and a live entry that disagreed with the record would say
            # `finished` until the next service restart said `stopped` -- the same fact,
            # two answers depending on uptime.
            state.set_phase(status.phase)
            # Same one-shot notifier as a re-triggered share: this op runs from disk with
            # no live entry to inherit one from, and it reports on a campaign that ended
            # long ago -- so neither branch is the campaign's terminal message.
            notifier = self._notifier(request.campaign_id)
            if ok:
                notifier.postprocessed()
            elif state.postprocessing_stop_requested:
                # A re-run is a tracked campaign while it lasts, so ``stop_campaign``
                # reaches it and ends it. What comes back then is the operator's own
                # doing, and announcing it as a failure would file that under faults.
                notifier.postprocessing_cancelled(message)
            else:
                notifier.postprocessing_failed(message)

        return self._dispatch_background(
            request.campaign_id, phase=Phase.POSTPROCESSING, work=work)
    # None means 'nothing to arrange', per the docstring
    def _scene_runner_context(self, campaign_id: str, identity: dict, on_wait=None):  # pylint: disable=useless-return
        """Context manager yielding the generator's container-runner factory, or None.

        Locally there is nothing to arrange: an absent factory makes the generator fall back to an
        ephemeral ``docker run`` on the campaign's image, which is exactly right. The cluster lane
        overrides this with an aux pod whose lifetime is the build's.

        *on_wait* is ``(stage, detail) -> None``, called by a lane while it waits for something
        before the build can begin, so that wait can be named to whoever is polling for the
        geometry. Only the lane can see those states, and only the caller knows whether anyone is
        listening — a screenshot render is synchronous and passes nothing.
        """
        del campaign_id, identity, on_wait
        return None
    def _resolve_image_digest(self, ref: str):
        """An image reference resolved to the bytes it currently names, or None.

        The lane-specific half of naming the simulator's image: a campaign that recorded only a
        declared *tag* (one from before per-role digests were written on this lane) can still be
        keyed on bytes, because Docker is right here to ask. The cluster lane deliberately does not
        implement this -- there is no pull-less registry lookup in the tree, and an aux pod's
        imageID arrives only after the pull, far too late to name the output directory -- so there a
        tag-only campaign is refused with a message instead of being guessed at. It costs nothing:
        that lane has recorded per-role digests since per-role digests existed.
        """
        from robovast.common.execution import \
            _get_image_revision  # pylint: disable=import-outside-toplevel
        revision = _get_image_revision(ref)
        return None if revision == "unknown" else revision
