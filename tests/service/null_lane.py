# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A lane that runs nothing: the concrete ``ServiceBase`` tests drive lane-neutral code through.

Most of the service is correct for any lane -- workspaces, files, records, listings, the
event log, imports, the routes and the MCP tools over them -- and a test of that code needs
a concrete class to construct, not a lane. This is that class. Every hook the base leaves
to a lane is answered here in the way that commits to nothing: a launch is admitted and
reaches the controller, which the tests that launch patch out; nothing is built, nothing
runs, no job exists, no resources are measured, and every operation that would need a
driver is refused by name with :class:`UnsupportedOnLane`, exactly as a lane that lacks it
would refuse it. A test that needs one answer replaces that one hook.

Not a fake cluster and not a mock: it is a real ``ServiceBase``, so what it exercises is
the base's own code paths, and what it refuses is refused the way a client would see it.
"""

import contextlib

from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.service.interface import (ActionResult, ResourceUsage, UnsupportedOnLane,
                                        UpgradeInfo, VersionInfo)
from robovast.service.service_base import ServiceBase


class NullBackend(ExecutionBackend):
    """The backend a null lane hands the controller: it refuses to run a batch.

    The launch path builds the backend before the controller is entered, so a test that
    launches with the controller patched out never reaches this refusal; one that forgets
    the patch gets the refusal rather than a hang.
    """

    def __init__(self, state=None):
        self.state = state

    def run_batch(self, *args, **kwargs):
        del args, kwargs
        raise UnsupportedOnLane("run_batch", NullLane.LANE, hint="the null lane runs nothing")


class NullLane(ServiceBase):
    """See the module docstring."""

    LANE = "null"

    def _refuse(self, operation: str):
        raise UnsupportedOnLane(operation, self.LANE, hint="the null lane runs nothing")

    # -- admission: everything the default asks for, nothing more ------------------------

    def _guard_new_campaign(self) -> None:
        pass

    def _queues_campaigns(self) -> bool:
        return False

    def _admit_scheduling(self, request) -> None:
        asked = [name for name in ("priority", "paused") if getattr(request, name, None)]
        if asked:
            self._refuse(" and ".join(asked))

    def _admit_show_gui(self, request) -> None:
        if getattr(request, "show_gui", False):
            self._refuse("show_gui")

    def _scheduling_for(self, campaign_id: str, *, live: bool) -> dict:
        del campaign_id, live
        return {"priority": 0, "paused": False}

    def _run_options(self, request) -> RunOptions:
        return RunOptions(
            upload_to_share=bool(getattr(request, "upload_to_share", False)),
            image_project=getattr(request, "image_project", "") or None,
            image_project_tag=getattr(request, "image_project_tag", "") or None)

    # -- running: a backend that refuses, contexts that hold nothing ---------------------

    def _build_backend(self, state):
        return NullBackend(state=state)

    def _postprocess_in_process(self) -> bool:
        return False

    def _postprocess_campaign(self, campaign_id, campaign_dir, *, force=False, skip=(),
                              state=None):
        del campaign_id, campaign_dir, force, skip, state
        self._refuse("postprocessing")

    def _aux_runner_context(self, tag: str, project, *, hold: bool = False,
                            should_stop=None):
        del tag, project, hold, should_stop
        return contextlib.nullcontext(None)

    def _scene_runner_context(self, campaign_id: str, identity: dict, on_wait=None):
        del campaign_id, identity, on_wait
        return None

    def _shutdown_running_campaigns(self, running) -> None:
        del running

    # -- images: nothing to build, nothing built -----------------------------------------

    @property
    def _images(self):
        store = getattr(self, "_image_store", None)
        if store is None:
            self._refuse("image store")
        return store

    def _start_build_images(self, project, campaign_config, image_project=None,
                            image_project_tag=None, should_stop=None) -> list:
        del project, campaign_config, image_project, image_project_tag, should_stop
        return []

    def _resolve_built_images(self, project, campaign_config, image_project=None,
                              image_project_tag=None, should_stop=None) -> dict:
        del project, campaign_config, image_project, image_project_tag, should_stop
        return {}

    def _resolve_image_digest(self, ref: str):
        del ref
        return ""

    def build_image(self, request):
        del request
        self._refuse("build_image")

    def get_image_build_status(self, build_id: str):
        del build_id
        self._refuse("get_image_build_status")

    def get_image_build_log(self, build_id: str, offset: int = 0):
        del build_id, offset
        self._refuse("get_image_build_log")

    # -- jobs and exec: there are none ---------------------------------------------------

    def _exec_lane(self):
        self._refuse("exec_in_container")

    def _job_state_target(self, campaign_id: str, job_name: str, role: str) -> tuple:
        del campaign_id, job_name, role
        self._refuse("get_job_state")

    def _job_live_run(self, campaign_id: str, job_name: str, target, run_dir: str) -> tuple:
        del campaign_id, job_name, target, run_dir
        self._refuse("get_job_state")

    def _job_output_dir(self, campaign_id: str, job_name: str, run_dir: str) -> str:
        del campaign_id, job_name, run_dir
        self._refuse("get_job_state")

    def _new_job_log_tail(self, campaign_id: str, job_name: str):
        del campaign_id, job_name
        self._refuse("get_job_log")

    def list_jobs(self, campaign_id: str):
        del campaign_id
        self._refuse("list_jobs")

    def get_job_log(self, campaign_id: str, job_name: str, offset: int = 0):
        del campaign_id, job_name, offset
        self._refuse("get_job_log")

    def stop(self, campaign_id: str) -> ActionResult:
        del campaign_id
        self._refuse("stop")

    def stop_job(self, campaign_id: str, job_name: str, reason=None,
                 source: str = "api") -> ActionResult:
        del campaign_id, job_name, reason, source
        self._refuse("stop_job")

    def run_postprocessing(self, request) -> ActionResult:
        del request
        self._refuse("run_postprocessing")

    # -- capacity: nothing measured, nothing reserved ------------------------------------

    def _compute_resource_usage(self) -> ResourceUsage:
        return ResourceUsage(backend=self.LANE, cpu_capacity=0.0, cpu_used=0.0,
                             memory_capacity_bytes=0, memory_used_bytes=0,
                             parallel_runs=False)

    def _scenario_job_tally(self) -> "tuple[int, int]":
        return 0, 0

    # -- the handshake and the roll ------------------------------------------------------

    def version(self) -> VersionInfo:
        return self._version_info(backend=self.LANE, can_build_images=False,
                                  can_schedule=self._queues_campaigns(),
                                  results_root=str(self._campaigns_root()),
                                  sources_root=str(self.store.registry.root))

    def upgrade_info(self) -> UpgradeInfo:
        return UpgradeInfo(supported=False,
                           unsupported_reason="the null lane is not deployed anywhere",
                           active_campaigns=self._active_campaigns())

    def upgrade_service(self, force: bool = False) -> ActionResult:
        del force
        self._refuse("upgrade_service")
