# Copyright (C) 2025 Frederik Pasch
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

import logging
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

logger = logging.getLogger(__name__)


class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra='allow')


class VariationConfig(BaseModel):
    pass
    # model_config = ConfigDict(extra='forbid')


class ScenarioParameterConfig(BaseModel):
    model_config = ConfigDict(extra='allow')


class ConfigurationConfig(BaseModel):
    name: str
    parameters: Optional[list[ScenarioParameterConfig]] = None
    variations: Optional[list[VariationConfig]] = None

    @field_validator('name')
    @classmethod
    def validate_name_no_invalid_characters(cls, v: str) -> str:
        if not v.islower():
            raise ValueError(f'name {v} must be all lowercase')
        if '_' in v or ' ' in v or '.' in v:
            raise ValueError(f'name {v}must not contain underscores, spaces, or periods')
        return v


class KubernetesResourcesConfig(BaseModel):
    cpu: int | str
    memory: Optional[str] = None


class KubernetesConfig(BaseModel):
    resources: KubernetesResourcesConfig


class ExecutionConfig(BaseModel):
    image: str
    kubernetes: KubernetesConfig
    env: Optional[list[dict[str, str]]] = None
    runs: int
    scenario_file: Optional[str] = None
    test_files_filter: Optional[list[str]] = None

    @field_validator('env')
    @classmethod
    def validate_no_reserved_env_vars(cls, v: Optional[list[dict[str, str]]]) -> Optional[list[dict[str, str]]]:
        """Validate that env does not contain reserved environment variable names."""
        if v is None:
            return v
        
        # Reserved keys that are set automatically during execution
        reserved_keys = {
            'RUN_ID', 'RUN_NUM', 'SCENARIO_ID', 'SCENARIO_CONFIG', 'ROS_LOG_DIR',
            'PRE_COMMAND', 'POST_COMMAND'
        }
        
        found_reserved = []
        for env_item in v:
            if isinstance(env_item, dict):
                for key in env_item.keys():
                    if key in reserved_keys:
                        found_reserved.append(key)
        
        if found_reserved:
            raise ValueError(
                f"execution.env contains reserved environment variable names: {', '.join(found_reserved)}. "
                f"Reserved names are: {', '.join(sorted(reserved_keys))}"
            )
        
        return v

class VisualizationConfig(BaseModel):
    pass


class AnalysisConfig(BaseModel):
    postprocessing: Optional[list[str | dict[str, Any]]] = None
    visualization: Optional[list[dict[str, Any]]] = None


class ConfigV1(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = 1
    metadata: Optional[dict[str, Any]] = None
    general: Optional[GeneralConfig] = None
    configuration: Optional[list[ConfigurationConfig]] = None
    execution: ExecutionConfig
    analysis: Optional[AnalysisConfig] = None


def validate_config(config: dict):
    """
    Validate the configuration settings.

    Args:
        settings: The settings dictionary to validate
    Raises:
        ValueError: If required sections are missing
    """
    logger.debug("Validating configuration")
    version = config.get("version", None)
    if version != 1:
        logger.error(f"Unsupported config version: {version}")
        raise ValueError(f"Unsupported config version: {version}")
    logger.debug(f"Config version {version} is supported")
    return get_validated_config(config, ConfigV1)


def get_validated_config(config: dict, config_class):
    try:
        logger.debug(f"Validating config against {config_class.__name__}")
        config = config_class(**config)
        logger.debug("Configuration validation successful")
    except Exception as e:
        if isinstance(e, ValidationError):
            errors = []
            for error in e.errors():  # pylint: disable=no-member
                field = ".".join(str(loc) for loc in error['loc'])
                msg = error['msg']
                errors.append(f"  - {field}: {msg}")
            error_msg = f"Config validation failed:\n" + "\n".join(errors)
            logger.error(error_msg)
            raise ValueError(error_msg) from None
        logger.error(f"Config validation failed: {str(e)}")
        raise ValueError(f"Config validation failed: {str(e)}") from None
    return config
