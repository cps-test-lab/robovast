#!/usr/bin/env python3
"""Shared building blocks, re-exported **lazily**.

These names are the convenient spelling for the campaign-composition core, and callers
have used ``from robovast.common import load_config`` for a long time. Importing them
eagerly here, though, made the package ``__init__`` reach ``.common`` -> ``numpy`` and
``scenario_execution``, and Python runs a parent ``__init__`` before *any* submodule. So
``import robovast.client.status`` -- a pydantic model with no other dependency -- cost 528
modules and half a second, and every module under ``robovast.common`` inherited the
simulator stack whether it touched it or not.

PEP 562 module ``__getattr__`` keeps both properties: the re-exports resolve on first
attribute access, so every existing call site works unchanged, while a submodule import
pays only for that submodule. Nothing here is a compatibility shim -- there is still one
spelling for each name, it just resolves later.

Add to ``_LAZY`` when adding a re-export; the test in
``tests/common/test_lazy_common_reexports.py`` checks the two halves agree and that
importing this package stays cheap.
"""

from typing import TYPE_CHECKING

#: Re-exported name -> the submodule that defines it.
_LAZY = {
    "convert_dataclasses_to_dict": ".common",
    "filter_configs": ".common",
    "get_scenario_parameters": ".common",
    "is_scenario_parameter": ".common",
    "load_config": ".common",
    "CONTAINER_ROLES": ".config",
    "SCENARIO_CONTAINER": ".config",
    "SIMULATION_CONTAINER": ".config",
    "SUT_CONTAINER": ".config",
    "ContainerConfig": ".config",
    "VariationConfig": ".config",
    "get_validated_config": ".config",
    "ContainerPlan": ".containers",
    "PlannedContainer": ".containers",
    "plan_containers": ".containers",
    "execute_variation": ".config_generation",
    "generate_scenario_variations": ".config_generation",
    "CampaignConfigError": ".errors",
    "missing_input_error": ".errors",
    "COMPAT_VERSION": ".execution",
    "MIN_IMAGE_COMPAT": ".execution",
    "COMPAT_VERSION_LABEL": ".execution",
    "check_image_compat": ".execution",
    "image_compat_version": ".execution",
    "check_campaign_inputs": ".execution",
    "create_execution_yaml": ".execution",
    "generate_execution_yaml_script": ".execution",
    "get_campaign": ".execution",
    "get_campaign_timestamp": ".execution",
    "get_execution_env_variables": ".execution",
    "is_campaign_dir": ".execution",
    "prepare_campaign_configs": ".execution",
    "scenario_env": ".execution",
    "FileCache": ".file_cache",
    "ProgressBar": ".progress",
    "fmt_size": ".progress",
    "make_transfer_progress_callback": ".progress",
}

__all__ = sorted(_LAZY)


def __getattr__(name):
    """Resolve a re-exported name on first access (PEP 562)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module  # pylint: disable=import-outside-toplevel
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value  # cache, so the next access is a plain attribute lookup
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:  # the real imports, for type checkers and IDEs only
    from .common import (convert_dataclasses_to_dict, filter_configs, get_scenario_parameters,
                         is_scenario_parameter, load_config)
    from .config import (CONTAINER_ROLES, SCENARIO_CONTAINER, SIMULATION_CONTAINER, SUT_CONTAINER,
                         ContainerConfig, VariationConfig, get_validated_config)
    from .config_generation import execute_variation, generate_scenario_variations
    from .containers import ContainerPlan, PlannedContainer, plan_containers
    from .errors import CampaignConfigError, missing_input_error
    from .execution import (COMPAT_VERSION, COMPAT_VERSION_LABEL, MIN_IMAGE_COMPAT,
                            check_campaign_inputs, check_image_compat, create_execution_yaml,
                            generate_execution_yaml_script, get_campaign, get_campaign_timestamp,
                            get_execution_env_variables, image_compat_version, is_campaign_dir,
                            prepare_campaign_configs, scenario_env)
    from .file_cache import FileCache
    from .progress import ProgressBar, fmt_size, make_transfer_progress_callback
