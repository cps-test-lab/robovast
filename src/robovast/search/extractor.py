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

"""The single, SUT-specific scoring step: results -> objectives + measures.

An :class:`Extractor` reads one parameter set's per-config result directory and
returns exactly what the search needs: the **objectives** (optimized) and the
**measures** (quality-diversity behavior axes; ignored by non-QD strategies).
It is the one place SUT-specific evaluation lives — and is parameterized from the
``.vast`` (``extract.params``) and loadable from a local file. ``objectives`` and
``measures`` are named dicts so single- and multi-objective use the same shape.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from robovast.common.campaign_data import invalid_runs

logger = logging.getLogger(__name__)


class NoSampleError(RuntimeError):
    """This parameter set produced nothing measurable, as opposed to a bug in the
    extractor. The only exception type a search is allowed to treat as "record this
    one and carry on" rather than a fatal error.

    The counterpart of
    :class:`~robovast.common.variation.base_variation.VariationInfeasibleError` on the
    scoring side, and the distinction is the same one: a draw that could not be
    *realized* skips composition, a cell that could not be *measured* skips evaluation,
    and anything else is a defect that must still abort loudly.

    Raise this instead of scoring a fallback value. A fabricated ``0.0`` is
    indistinguishable from a cell that genuinely scored zero, which is how an objective
    goes structurally dead while the campaign looks healthy. Raising a bare
    ``RuntimeError`` instead is the other failure: it aborted a 50-batch campaign over
    one cell's container bringup, discarding every completed batch with it.

    ``config_name`` is filled in by the caller that knows it, so a reporting layer can
    name the cell in a structured field rather than only inside the message.

    ``include_traceback = False`` (see
    :func:`robovast.client.status.failure_detail`): the message names the cell and the
    reason, which is the whole of what a reader can act on. A stack trace through the
    controller only makes an unmeasurable cell look like a RoboVAST crash.
    """

    include_traceback = False

    def __init__(self, message, config_name=None):
        super().__init__(message)
        self.config_name = config_name


def run_dirs(config_dir: Path) -> list[Path]:
    """Numeric run subdirectories of a per-config result directory, in order."""
    if not config_dir.is_dir():
        return []
    return sorted(
        (d for d in config_dir.iterdir() if d.is_dir() and d.name.isdigit()),
        key=lambda d: int(d.name),
    )


def completed_run_dirs(config_dir: Path) -> list[Path]:
    """Run subdirectories that produced a **trustworthy** result (``test.xml``).

    The canonical "completed sample" notion, shared by extractors (aggregation
    denominator) and the framework (``n_samples``).

    A run the runner invalidated is excluded even though it wrote a ``test.xml``: a
    container the trial depended on crashed and was restarted under it, so what that file
    records is a simulator that had lost its state, and it is at its most dangerous when it
    says the run passed. The filter lives HERE and not in each extractor because this is
    the shared definition and extractors *outside this repository* call it directly -- a
    rule they have to opt into is a rule that will be missed, silently, in the direction of
    believing a broken trial.

    Costs one ``is_file`` miss per config for every campaign nobody intervened in, which is
    nearly all of them. A config directory copied out of its campaign has no ledger beside
    it and gets the old behaviour; that is the honest degradation.
    """
    invalid = _invalidated_runs(config_dir)
    return [d for d in run_dirs(config_dir)
            if (d / "test.xml").exists() and f"{config_dir.name}/{d.name}" not in invalid]


def _invalidated_runs(config_dir: Path) -> "set[str]":
    """``{"<config>/<run>"}`` the runner threw away, from the campaign's ledger.

    The campaign root is ``config_dir``'s parent -- every caller builds the config dir as
    ``campaign_root / config_name`` -- so the ledger is reachable without threading a new
    argument through the extractor API that third-party extractors implement.
    """
    try:
        return set(invalid_runs(config_dir.parent))
    except Exception:  # noqa: BLE001 - a missing or odd ledger must not stop extraction
        logger.debug("Could not read the intervention ledger beside %s", config_dir)
        return set()


@dataclass
class ExtractResult:
    """What an :class:`Extractor` returns for one parameter set.

    Attributes:
        objectives: Named optimized values (>=1). Single-objective is one entry.
            Must contain every objective the ``.vast`` declares; reporting more than
            that is allowed and costs nothing, as
            :class:`~robovast.search.evaluator.Evaluator` narrows this to the declared
            names and files the rest under ``measures``. Prefer putting a value that
            is not optimized there in the first place -- it says what it is.
        measures: Named quality-diversity behavior values; ``{}`` when unused.
    """
    objectives: dict[str, float]
    measures: dict[str, float] = field(default_factory=dict)


class Extractor(ABC):
    """Maps a per-config result directory to objectives + measures.

    Constructed with the ``extract.params`` from the ``.vast`` (so thresholds /
    column names / aggregation can be swept without editing code). Aggregation
    over the config's runs is the extractor's responsibility.
    """

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def extract(self, config_dir: Path) -> ExtractResult:
        ...
