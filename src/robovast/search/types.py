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

"""Data types exchanged across the search interface.

These are intentionally minimal and open: a strategy receives full
:class:`Evaluation` objects (objective + descriptor + raw results), so the
interface never constrains what an algorithm can use to decide the next
generation.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Optional


def _param_set_id(values: dict[str, Any]) -> str:
    """Stable, content-derived id for a parameter assignment."""
    blob = json.dumps(values, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]  # nosec - not security


@dataclass(frozen=True)
class ParamSet:
    """One concrete parameter assignment proposed by a strategy.

    Attributes:
        values: Mapping of ``search_space`` param path -> sampled value.
        id: Stable identifier derived from ``values`` (used as the config name
            and result directory name so results stay addressable per param set).
            Deliberately independent of ``n_reps`` so the same assignment has a
            stable identity regardless of how many times it is evaluated.
        n_reps: Optional override for how many times to evaluate this assignment.
            ``None`` (the default) means use the campaign's ``execution.runs``.
            A noise-aware strategy can request more repetitions for borderline /
            uncertain assignments and fewer for clearly-decided ones.
    """
    values: dict[str, Any]
    id: str = ""
    n_reps: Optional[int] = None

    def __post_init__(self):
        if not self.id:
            # frozen dataclass: assign via object.__setattr__
            object.__setattr__(self, "id", _param_set_id(self.values))


@dataclass
class Evaluation:
    """The scored outcome of running one :class:`ParamSet`.

    Attributes:
        params: The evaluated parameter set.
        objectives: Named optimized values the extractor returned (>=1). Single-
            objective strategies read the sole entry; multi-objective uses all.
        measures: Named quality-diversity behavior values (``{}`` when unused).
        n_samples: How many repetitions (completed runs) the values were
            aggregated over — a point estimate from this many samples, so a
            noise-aware strategy can reason about confidence.
        raw: The full per-config result dict (paths, …) for any algorithm to mine.
    """
    params: ParamSet
    objectives: dict[str, float]
    measures: dict[str, float] = field(default_factory=dict)
    n_samples: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class SearchReport:
    """A strategy's deliverable at the end (or any point) of a search.

    Populated by whichever strategy produced it: single-objective strategies set
    ``best``; quality-diversity returns an archive (``extra``); multi-objective
    sets ``front`` (the Pareto set). ``evaluations`` is the flat history.
    """
    evaluations: list[Evaluation] = field(default_factory=list)
    best: Optional[Evaluation] = None
    front: list[Evaluation] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
