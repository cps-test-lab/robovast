import os

def get_scenario_base_path():
    path = os.path.join(os.getcwd(), "Dataset")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Scenario base path does not exist: {path}")
    return path

