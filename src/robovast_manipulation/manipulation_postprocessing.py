"""Postprocessing plugin for manipulation campaigns.

Reads result.json from each run directory and writes a summary CSV.
"""

import csv
import json
import logging
from pathlib import Path
from typing import Optional, Tuple

from robovast.results_processing.postprocessing_plugins import BasePostprocessingPlugin

logger = logging.getLogger(__name__)

_RESULT_FIELDS = [
    "result_code",
    "planning_time_sec",
    "execution_time_sec",
    "joint_space_error",
    "final_joint_state",
]


class ManipulationProcessResults(BasePostprocessingPlugin):
    """Read result.json files from all runs and write manipulation_results.csv."""

    def __call__(
        self,
        results_dir: str,
        config_dir: str,
        provenance_file: Optional[str] = None,
        **kwargs,
    ) -> Tuple[bool, str]:
        results_path = Path(results_dir)
        rows = []

        for result_json in sorted(results_path.glob("*/*/result.json")):
            run_dir = result_json.parent
            config_name = run_dir.parent.name
            run_number = run_dir.name
            try:
                with open(result_json, encoding="utf-8") as fh:
                    data = json.load(fh)
                row = {"config": config_name, "run": run_number}
                for field in _RESULT_FIELDS:
                    row[field] = data.get(field)
                rows.append(row)
            except Exception as exc:
                logger.warning("Could not read %s: %s", result_json, exc)

        if not rows:
            return True, "No result.json files found — nothing to process"

        out_csv = results_path / "manipulation_results.csv"
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["config", "run"] + _RESULT_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        return True, f"Wrote {len(rows)} rows to {out_csv}"
