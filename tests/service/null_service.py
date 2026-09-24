# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A service that runs nothing: the concrete ``ServiceBase`` the base's own code is tested through.

Most of the service does not depend on the cluster -- workspaces, files, records, listings,
the event log, imports, the routes and the MCP tools over them -- and a test of that code
needs a concrete class to construct. This is that class. Every hook the base leaves to its
implementation is answered here in the way that commits to nothing: a launch is admitted and
reaches the controller, which the tests that launch patch out; nothing is built, nothing
runs, no job exists, no resources are measured, and every operation that would need a
driver is refused by name with :class:`UnsupportedOperation`.

Not a fake cluster and not a mock: it is a real ``ServiceBase``, so what it exercises is
the base's own code paths, and what it refuses is refused the way a client would see it.
"""

import contextlib
import pathlib

from robovast.execution.backends import ExecutionBackend, RunOptions
from robovast.service.interface import (ActionResult, ResourceUsage, UnsupportedOperation,
                                        UpgradeInfo, VersionInfo)
from robovast.service.service_base import ServiceBase


class NullBackend(ExecutionBackend):
    """The backend NullService hands the controller: it refuses to run a batch.

    The launch path builds the backend before the controller is entered, so a test that
    launches with the controller patched out never reaches this refusal; one that forgets
    the patch gets the refusal rather than a hang.
    """

    def __init__(self, state=None):
        self.state = state

    def run_batch(self, *args, **kwargs):
        del args, kwargs
        raise UnsupportedOperation("run_batch", NullService.IMPLEMENTATION,
                                   hint="NullService runs nothing")



class NullService(ServiceBase):
    """See the module docstring."""

    IMPLEMENTATION = "null"

    def _refuse(self, operation: str):
        raise UnsupportedOperation(operation, self.IMPLEMENTATION, hint="NullService runs nothing")

    # -- admission: everything the default asks for, nothing more ------------------------

    def _guard_new_campaign(self) -> None:
        pass

    def _queues_campaigns(self) -> bool:
        return False

    def _admit_scheduling(self, request) -> None:
        asked = [name for name in ("priority", "paused") if getattr(request, name, None)]
        if asked and not self._queues_campaigns():
            self._refuse(" and ".join(asked))

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

    def _postprocess_campaign(self, campaign_id, campaign_dir, *, force=False, skip=(),
                              state=None):
        del campaign_id, campaign_dir, force, skip, state
        self._refuse("postprocessing")

    def _aux_runner_context(self, tag: str, project, *, hold: bool = False,
                            should_stop=None):
        del tag, project, hold, should_stop
        return contextlib.nullcontext(None)

    def _scene_runner_context(self, identity: dict, on_wait=None):
        del identity, on_wait

        @contextlib.contextmanager
        def context():
            yield None

        return context

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
        self._admit_storage("build an image")
        self._refuse("build_image")

    def get_image_build_status(self, build_id: str):
        del build_id
        self._refuse("get_image_build_status")

    def get_image_build_log(self, build_id: str, offset: int = 0):
        del build_id, offset
        self._refuse("get_image_build_log")

    # -- jobs and exec: there are none ---------------------------------------------------

    def _exec_runner(self):
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
        self._admit_storage("postprocess a campaign")
        self._refuse("run_postprocessing")

    # -- capacity: nothing measured, nothing reserved ------------------------------------

    def _disk_space(self) -> tuple:
        """``(DiskSpace, unavailable_reason)`` of nothing: no disk is measured here. A test
        of the storage reserve replaces this with the reading it wants judged."""
        return None, None

    def _compute_resource_usage(self) -> ResourceUsage:
        disk, unavailable = self._disk_space()
        return ResourceUsage(backend=self.IMPLEMENTATION, cpu_capacity=0.0, cpu_used=0.0,
                             memory_capacity_bytes=0, memory_used_bytes=0,
                             parallel_runs=False, disk=disk, disk_unavailable=unavailable)

    def _scenario_job_tally(self) -> "tuple[int, int]":
        return 0, 0

    def _image_labels(self, ref: str) -> "dict | None":
        return None

    def _image_build_lock(self, ref: str) -> dict:
        return {}

    # -- the handshake and the roll ------------------------------------------------------

    def version(self) -> VersionInfo:
        return self._version_info(backend=self.IMPLEMENTATION, can_build_images=False,
                                  can_schedule=self._queues_campaigns(),
                                  results_root=str(self._campaigns_root()),
                                  sources_root=str(self.store.registry.root))

    def upgrade_info(self) -> UpgradeInfo:
        return UpgradeInfo(supported=False,
                           unsupported_reason="NullService is not deployed anywhere",
                           active_campaigns=self._active_campaigns())

    def upgrade_service(self, force: bool = False) -> ActionResult:
        del force
        self._refuse("upgrade_service")


def serving(results_root, workspaces_root) -> NullService:
    """A NullService over *results_root*, for a test that needs a service to answer from it.

    What the MCP tools read -- listings, plots, logs, a configuration's contribution -- is
    read by the service from its results root, so a tool test hands them this NullService as the
    service (``service_access.service_client``) rather than a disk of its own.
    """
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore
    service = NullService(store=WorkspaceStore(registry=WorkspaceRegistry(root=workspaces_root)))
    service._campaigns_root = lambda: pathlib.Path(results_root)  # noqa: SLF001
    return service
