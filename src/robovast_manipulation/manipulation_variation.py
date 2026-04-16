"""ManipulationVariation — robovast variation plugin for manipulation experiments.

Contributes manipulation-specific PROV-O nodes to the campaign provenance
graph: per-config scenario properties (planner, velocity/acceleration scaling,
joint goal) and per-run ManipulationExecution activity nodes with result
metrics read from each run's result.json.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from rdflib import PROV, Namespace

from robovast.common.variation.base_variation import ProvContribution, Variation
from robovast_manipulation import MANIPULATION_NS_PREFIX, MANIPULATION_NS_URI

logger = logging.getLogger(__name__)

MANIPULATION = Namespace(MANIPULATION_NS_URI)


class ManipulationVariation(Variation):
    """Variation plugin that contributes manipulation-specific PROV-O nodes.

    Does not generate new configurations (``variation()`` is a pass-through).
    Its sole purpose is to hook into the PROV generation pipeline via
    ``prov_namespaces()`` and ``collect_prov_metadata()``.
    """

    def variation(self, in_configs):
        """Pass configs through unchanged — this variation adds no new configs."""
        return in_configs

    @classmethod
    def prov_namespaces(cls) -> dict:
        """Declare the manipulation namespace prefix for the JSON-LD context."""
        return {MANIPULATION_NS_PREFIX: MANIPULATION_NS_URI}

    @classmethod
    def collect_prov_metadata(
        cls,
        config_entry: dict,
        campaign_namespace,
        config_namespace,
        gen_activity_id: str,
        vast_id: str,
        campaign_dir: Optional[Path] = None,
    ) -> Optional[ProvContribution]:
        config = config_entry.get("config", {})

        # --- Config-level properties (merged onto ConcreteScenario node) ---
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

        # --- Run-level nodes (one ManipulationExecution Activity per run) ---
        graph_nodes = []

        for run_entry in config_entry.get("test_results", []):
            run_dir_rel = run_entry["dir"]  # e.g. "rrtconnect-1-1-1/1"
            run_number = run_dir_rel.rsplit("/", 1)[-1]  # "1"

            result: dict = {}
            if campaign_dir is not None:
                result_path = Path(campaign_dir) / run_dir_rel / "result.json"
                try:
                    with open(result_path, encoding="utf-8") as fh:
                        result = json.load(fh)
                except Exception as exc:
                    logger.warning(
                        "ManipulationVariation: could not read %s: %s",
                        result_path,
                        exc,
                    )

            test_exec_iri = str(campaign_namespace[run_dir_rel])
            manip_exec_iri = str(
                config_namespace[f"{run_number}/manipulation_execution"]
            )

            node: dict = {
                "@id": manip_exec_iri,
                "@type": [str(PROV["Activity"]), str(MANIPULATION["ManipulationExecution"])],
                str(PROV["wasInformedBy"]): test_exec_iri,
            }

            for src_key, tgt_key in (
                ("result_code",        str(MANIPULATION["resultCode"])),
                ("planning_time_sec",  str(MANIPULATION["planningTimeSec"])),
                ("execution_time_sec", str(MANIPULATION["executionTimeSec"])),
                ("joint_space_error",  str(MANIPULATION["jointSpaceError"])),
                ("final_joint_state",  str(MANIPULATION["finalJointState"])),
            ):
                val = result.get(src_key)
                if val is not None:
                    node[tgt_key] = val

            graph_nodes.append(node)

        if not scenario_properties and not graph_nodes:
            return None

        return ProvContribution(
            graph_nodes=graph_nodes,
            scenario_properties=scenario_properties,
            run_used_iris=[],
        )
