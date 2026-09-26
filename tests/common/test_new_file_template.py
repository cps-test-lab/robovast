# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The web UI's new ``.vast`` passes the validation it is written for.

The editor creates a file from ``frontend/ui/src/lib/vastTemplate.ts``, taking its
``version:`` from the default the config schema publishes. So the schema's default must be
the one version authoring accepts, and the template's body must validate under it.
"""

import pathlib
import re

import pytest
import yaml

from robovast.common.config import ConfigV1, validate_config
from robovast.common.migrations import SUPPORTED_CONFIG_VERSION

TEMPLATE = (pathlib.Path(__file__).resolve().parents[2]
            / "frontend" / "ui" / "src" / "lib" / "vastTemplate.ts")


def test_the_schema_publishes_the_supported_version_as_its_default():
    schema = ConfigV1.model_json_schema()
    assert schema["properties"]["version"]["default"] == SUPPORTED_CONFIG_VERSION


def test_the_web_uis_new_file_validates():
    if not TEMPLATE.is_file():
        pytest.skip("frontend/ui not present (no web UI checkout)")
    match = re.search(r"VAST_BODY = `([^`]*)`", TEMPLATE.read_text(encoding="utf-8"))
    assert match, f"no VAST_BODY template literal in {TEMPLATE}"
    version = ConfigV1.model_json_schema()["properties"]["version"]["default"]
    validate_config(yaml.safe_load(f"version: {version}\n{match.group(1)}"))
