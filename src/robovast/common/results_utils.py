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

"""Common utilities for results directory layout (<campaign-name>-<timestamp>/<config>/<run-number>)."""
from pathlib import Path
from typing import Iterator, Optional, Tuple

from robovast.common.execution import is_campaign_dir


def iter_run_folders(results_dir: str) -> Iterator[Tuple[str, str, str, Path]]:
    """Iterate over all run folders under a results directory.

    Discovers the standard layout: results_dir/<campaign-name>-<timestamp>/<config>/<run-number>/.
    Under results_dir, only directories matching the campaign naming pattern are
    considered; under each campaign, subdirs are config names; under each config,
    subdirs whose names are numeric are run numbers.

    Args:
        results_dir: Path to the project results directory (parent of campaign directories).

    Yields:
        Tuples (campaign, config_name, run_number, folder_path) where folder_path
        is the full path to <campaign-name>-<timestamp>/<config>/<run-number>.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return

    for campaign_item in sorted(root.iterdir()):
        if not campaign_item.is_dir() or not is_campaign_dir(campaign_item.name):
            continue
        if campaign_item.name == "_config":
            continue
        campaign = campaign_item.name

        for config_item in sorted(campaign_item.iterdir()):
            if not config_item.is_dir():
                continue
            config_name = config_item.name

            for run_item in sorted(config_item.iterdir()):
                if not run_item.is_dir() or not run_item.name.isdigit():
                    continue
                run_number = run_item.name
                folder_path = run_item
                yield campaign, config_name, run_number, folder_path


def campaign_vast(campaign_dir) -> Path:
    """The single ``.vast`` under a campaign's ``_config/`` — the one source of truth
    for that campaign's config (both the editable postprocessing/visualization blocks
    and the as-ran variations/execution). Raises ``ValueError`` if it is missing.

    Distinct from :func:`find_campaign_vast_file`, which takes a *results dir* and picks
    the most recent campaign; this takes a specific campaign directory.
    """
    config_dir = Path(campaign_dir) / "_config"
    vasts = sorted(config_dir.glob("*.vast"))
    if not vasts:
        # Say what the absence *means*, because the bare "no .vast in <dir>" was read as
        # a broken config and is nothing of the kind: the frozen config is projected with
        # a campaign's results, so a directory without one is a campaign whose results
        # were never projected here -- one that failed before that step, or one this
        # process does not drive.
        raise ValueError(
            f"no .vast in {config_dir}: this campaign has no frozen config here, so its "
            "results were never projected into this directory (it failed before that "
            "step, or it belongs to another driver). There is nothing to read its "
            "configuration from.")
    return vasts[0]


def campaign_execution(campaign_dir) -> dict:
    """The ``execution`` block of a campaign's own frozen ``.vast``, as a **mapping**.

    Read from ``_config/`` rather than from whatever a workspace holds now: the question is what
    *this* campaign is running, and a workspace edited since it launched would answer for a
    different one.

    A mapping and not :class:`~robovast.common.config.ConfigV1`, because that is what every
    consumer of an execution block takes -- :func:`~robovast.common.containers.plan_containers`
    says so in its own signature, and :func:`~robovast.common.simulators.health_command` indexes
    it. Handing back the pydantic model is exactly the bug this function exists to end.

    Read as a **subsection**, which is the lenient policy :func:`load_config` documents for this:
    one section does not need the whole archived document to validate, and requiring it made a
    perfectly good campaign unreadable by the tool that produced it. The strict read is for a
    config being authored or launched, and this is neither.

    Raises for a missing or unreadable ``.vast``; a campaign that simply declares no ``execution``
    yields ``{}``. Whether the raise is fatal is the caller's to decide -- a live-run diagnostic
    reports it as a stated reason, a viewer shrugs and shows the campaign anyway -- and collapsing
    the two here would make one of them wrong.

    **The backend's contributions are merged in**, through the one seam that does that
    (:func:`~robovast.common.simulators.apply_backend`), because the archived ``.vast`` is what the
    author *wrote* and not what the campaign *ran*. A simulator declared as bare
    ``simulation: {backend: roqsim}`` has no image in the file -- the backend supplies one -- so the
    container plan built from the raw block folds the simulator onto the scenario container, while
    the pod that ran had a separate one. Reading it raw therefore sent the simulator's own health
    command into a container with no simulator in it, and the campaign said so:
    ``exec: roqsim: not found``. ``apply_backend``'s docstring already names the container plan as a
    consumer that must see the merged picture; this was a second consumer that did not.

    *base_dir* is the archived project, so a backend referenced as ``./file.py:Class`` resolves
    against the tree the campaign actually ran with rather than against whatever is on disk now.
    """
    from robovast.common.common import load_config
    from robovast.common.simulators import apply_backend
    vast = campaign_vast(campaign_dir)
    execution = load_config(str(vast), subsection="execution", allow_missing=True) or {}
    return apply_backend(execution, base_dir=str(vast.parent))


def find_campaign_vast_file(results_dir: str) -> tuple[Optional[str], Optional[str]]:
    """The ``.vast`` for *results_dir* -- its own if it is a campaign, else a recent one.

    Two callers, two meanings, and getting them the same answer was a bug: postprocessing
    passes a **campaign directory** and must get that campaign's own snapshot, while a
    bare "show me a config" caller passes a **results root** and just wants a plausible
    one. Only the second wants the scan.

    Args:
        results_dir: A campaign directory, or a results root holding several.

    Returns:
        Tuple ``(vast_file_path, config_dir)`` where *config_dir* is the
        ``_config/`` directory containing the ``.vast`` file, or
        ``(None, None)`` if no campaign with a ``.vast`` file is found.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return None, None

    # *This* campaign first, when the caller handed us one. Postprocessing passes the
    # campaign root, and a campaign's own snapshot is the only correct answer for it --
    # falling through to the scan below made a campaign read a **different experiment's**
    # ``results_processing`` config, silently, whenever its own was not the one the scan
    # happened to land on.
    own = _vast_in_config_dir(root / "_config")
    if own is not None:
        return own

    # Otherwise scan the campaign directories under a results *root*, newest last-sorted
    # first. Note this orders by directory name, and a name is ``<experiment>-<timestamp>``
    # -- so it is only "most recent" among campaigns of the same experiment. Good enough
    # for a bare "give me some campaign's config" caller, and not reached by one that
    # knows which campaign it means.
    for campaign_item in sorted(root.iterdir(), reverse=True):
        if not campaign_item.is_dir() or not is_campaign_dir(campaign_item.name):
            continue
        config_dir = campaign_item / "_config"
        if config_dir.is_dir():
            vast_files = [f for f in sorted(config_dir.iterdir()) if f.is_file() and f.suffix == ".vast"]
            if len(vast_files) > 1:
                names = ", ".join(f.name for f in vast_files)
                raise ValueError(
                    f"Multiple .vast files found in {config_dir}: {names}. "
                    "Expected exactly one."
                )
            if vast_files:
                return str(vast_files[0]), str(config_dir)
    return None, None


def _vast_in_config_dir(config_dir: Path) -> "Optional[tuple[str, str]]":
    """The single ``.vast`` in one ``_config/``, or ``None`` if there is no such dir."""
    if not config_dir.is_dir():
        return None
    vast_files = [f for f in sorted(config_dir.iterdir())
                  if f.is_file() and f.suffix == ".vast"]
    if len(vast_files) > 1:
        names = ", ".join(f.name for f in vast_files)
        raise ValueError(
            f"Multiple .vast files found in {config_dir}: {names}. Expected exactly one.")
    if not vast_files:
        return None
    return str(vast_files[0]), str(config_dir)
