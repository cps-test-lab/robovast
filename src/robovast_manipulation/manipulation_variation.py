"""ManipulationVariation — robovast variation plugin for manipulation experiments.

Expands input configurations by planner ID and contributes manipulation-specific
scenario properties (planner, velocity/acceleration scaling, joint goals) to the
PROV-O provenance graph.
Run-level result processing is handled by the manipulation_postprocessing plugin.
"""

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict
from rdflib import Namespace

from robovast.common.variation.base_variation import ProvContribution, Variation
from robovast_manipulation import MANIPULATION_NS_PREFIX, MANIPULATION_NS_URI

MANIPULATION = Namespace(MANIPULATION_NS_URI)

_DEFAULT_PLANNERS = ["RRTConnect", "PTP", "KPIECE1"]


class ManipulationVariationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planners: list[str] = _DEFAULT_PLANNERS


class ManipulationVariation(Variation):
    """Variation plugin for manipulation experiments.

    Expands each input configuration into one config per planner ID, then
    contributes manipulation-specific PROV-O scenario properties.
    """

    CONFIG_CLASS = ManipulationVariationConfig

    def variation(self, in_configs):
        results = []
        for config in in_configs:
            for planner in self.parameters.planners:
                results.append(self.update_config(config, {"planner_id": planner}))
        return results

    @classmethod
    def prov_namespaces(cls) -> dict:
        return {MANIPULATION_NS_PREFIX: MANIPULATION_NS_URI}

    @classmethod
    def collect_prov_metadata(
        cls,
        config_entry: dict,
        campaign_namespace,
        config_namespace,
        gen_activity_id: str,
        vast_id: str,
        campaign_dir=None,
    ) -> Optional[ProvContribution]:
        config = config_entry.get("config", {})

        scenario_properties: dict = {}
        if config.get("planner_id") is not None:
            scenario_properties[str(MANIPULATION["plannerId"])] = config["planner_id"]
        if config.get("velocity_scaling_requested") is not None:
            scenario_properties[str(MANIPULATION["velocityScaling"])] = config[
                "velocity_scaling_requested"
            ]
        if config.get("acceleration_scaling_requested") is not None:
            scenario_properties[str(MANIPULATION["accelerationScaling"])] = config[
                "acceleration_scaling_requested"
            ]
        if config.get("arm_joint_goal") is not None:
            scenario_properties[str(MANIPULATION["armJointGoal"])] = json.dumps(
                config["arm_joint_goal"]
            )

        if not scenario_properties:
            return None

        return ProvContribution(
            graph_nodes=[],
            scenario_properties=scenario_properties,
            run_used_iris=[],
        )
