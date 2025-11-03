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

from pydantic import BaseModel, ValidationError, ConfigDict
from typing import Optional, Any

class GeneralConfig(BaseModel):
    model_config = ConfigDict(extra='allow')

class ExecutionConfig(BaseModel):
    kubernetes_manifest: str
    scenario: str
    runs: int

class PreprocessingConfig(BaseModel):
    pass

class VisualizationConfig(BaseModel):
    pass

class AnalysisConfig(BaseModel):
    preprocessing: Optional[list[str]] = None
    visualization: Optional[list[dict[str, Any]]] = None

class ConfigV1(BaseModel):
    version: int = 1
    general: Optional[GeneralConfig] = None
    variation: Optional[list[dict[str, Any]]] = None
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
    version = config.get("version", None)
    if version != 1:
        raise ValueError(f"Unsupported config version: {version}")
    
    try:
        config = ConfigV1(**config)
    except Exception as e:
        if isinstance(e, ValidationError):
            errors = []
            for error in e.errors(): # pylint: disable=no-member
                field = ".".join(str(loc) for loc in error['loc'])
                msg = error['msg']
                errors.append(f"  - {field}: {msg}")
            raise ValueError(f"Config validation failed:\n" + "\n".join(errors)) from None
        raise ValueError(f"Config validation failed: {str(e)}") from None
