# Copyright (C) 2026 Frederik Pasch
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

"""A run's verdict and window, from the JUnit ``test.xml`` scenario-execution writes."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_test_result(run_dir) -> dict[str, Any]:
    """Parse JUnit test result from ``test.xml``.

    Args:
        run_dir: Path to the run directory (e.g. ``campaign-<id>/<config>/0``).

    Returns:
        Dictionary with keys: success (bool), duration_sec (float), start_time (ISO
        string), start_epoch (float), errors (int), failures (int), tests (int),
        failure_message (str or None).

    Raises:
        FileNotFoundError: If test.xml does not exist.
    """
    path = Path(run_dir) / "test.xml"
    if not path.exists():
        raise FileNotFoundError(f"test.xml not found in {run_dir}")
    return parse_test_result(path.read_bytes())


def parse_test_result(content: bytes | str) -> dict[str, Any]:
    """:func:`read_test_result` for a ``test.xml`` already read, from wherever it was read.

    For a reader that does not hold a run directory -- a calibration probe is read through a
    storage accessor -- so that every reader of the verdict parses it one way.

    Raises:
        ET.ParseError: If *content* is not XML.
    """
    root = ET.fromstring(content)

    errors = int(root.get("errors", "0"))
    failures = int(root.get("failures", "0"))
    tests = int(root.get("tests", "0"))

    testcase = root.find("testcase")
    duration = float(testcase.get("time", "0")) if testcase is not None else 0.0

    # Extract start_time from properties. Kept in both forms: the ISO string every reader
    # already uses, and the raw epoch seconds, because the wall window (start .. start +
    # duration) is how a job's container log is attributed to the run that produced it, and
    # re-parsing the ISO string to get back a number it was made from is a needless round
    # trip that also loses nothing gracefully when the format changes.
    start_time_iso = None
    start_epoch = None
    if testcase is not None:
        properties = testcase.find("properties")
        if properties is not None:
            for prop in properties.findall("property"):
                if prop.get("name") == "start_time":
                    ts = float(prop.get("value", "0"))
                    start_epoch = ts
                    start_time_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    break

    # Extract failure message if present
    failure_message = None
    if testcase is not None:
        failure_elem = testcase.find("failure")
        if failure_elem is not None:
            failure_message = failure_elem.get("message") or failure_elem.text

    return {
        "success": errors == 0 and failures == 0,
        "duration_sec": duration,
        "start_time": start_time_iso,
        "start_epoch": start_epoch,
        "errors": errors,
        "failures": failures,
        "tests": tests,
        "failure_message": failure_message,
    }


def trial_window(result: dict[str, Any]) -> tuple[float | None, float | None]:
    """A run's trial window ``(start, end)`` in wall epoch seconds, or ``(None, None)``.

    *result* is what :func:`read_test_result` returns. The window runs from the scenario's
    recorded start for the test case's duration: the stretch in which the run was executing
    its scenario, as opposed to its container's bring-up before it and teardown after it.
    ``(None, None)`` when ``test.xml`` recorded no start time.
    """
    start = result.get("start_epoch")
    if start is None:
        return None, None
    return start, start + (result.get("duration_sec") or 0.0)


__all__ = ["parse_test_result", "read_test_result", "trial_window"]
