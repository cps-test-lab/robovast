import os

import yaml


def get_variant_data(file_path: str) -> dict:
    """
    Get the variant data for a test by parsing scenario.variant

    Args:
        file_path: The path to the variant file
    """
    scenario_variant_path = os.path.join(file_path, 'scenario.variant')

    if os.path.exists(scenario_variant_path):
        with open(scenario_variant_path, 'r') as file:
            scenario_data = yaml.safe_load(file)

            if "nav_scenario" in scenario_data:
                return dict(scenario_data["nav_scenario"])
    return None
