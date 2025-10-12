import os

from variant_editor.data_models import VariantData
from robovast_common import get_scenario_base_path

def get_variant_map_path(maps_dir, variant_data: "VariantData") -> str:
    return os.path.join(
        maps_dir,
        variant_data.floorplan_variant_name,
        variant_data.variant.map_file,
    )
