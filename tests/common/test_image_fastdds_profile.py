# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The framework image widens how long a Fast DDS service waits for a new client's reply reader.

rmw_fastrtps drops a reply whose reader is not matched within the response writer's reliability
``max_blocking_time``. The image writes a profile raising it and points
``FASTRTPS_DEFAULT_PROFILES_FILE`` at it. Fast DDS reports a malformed profile only as a log line
and then runs on its defaults, so a broken profile would be a silent regression -- these tests
generate the file exactly as the Dockerfile does and read it back the way Fast DDS will.
"""

import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "container" / "robovast" / "Dockerfile"
PROFILE_PATH = "/etc/robovast/fastdds_profiles.xml"
NS = {"f": "http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"}
#: Fast DDS's default ``max_blocking_time`` for a reliable DataWriter.
FASTDDS_DEFAULT_BLOCKING_NS = 100_000_000


def _instructions():
    """The Dockerfile as instructions, continuation lines joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text())
    return [line.strip() for line in joined.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _generated_profile(tmp_path):
    runs = [i for i in _instructions() if i.startswith("RUN ") and PROFILE_PATH in i]
    assert len(runs) == 1, f"expected one RUN writing {PROFILE_PATH}, found {len(runs)}"
    target = tmp_path / "fastdds_profiles.xml"
    subprocess.run(["bash", "-euc", runs[0][len("RUN "):].replace(PROFILE_PATH, str(target))],
                   check=True)
    return ET.parse(target).getroot()


@pytest.mark.skipif(shutil.which("bash") is None, reason="the RUN line is a bash command")
def test_the_profile_raises_the_reply_wait_of_service_response_writers_only(tmp_path):
    root = _generated_profile(tmp_path)
    assert root.tag == f"{{{NS['f']}}}profiles"
    writers = root.findall("f:data_writer", NS)
    # "service" is the profile name rmw_fastrtps looks up for a service's response writer;
    # any other name, or a default profile, would leave services unchanged or touch topics too.
    assert [w.get("profile_name") for w in writers] == ["service"]
    assert all(w.get("is_default_profile") is None for w in writers)
    assert root.findall("f:data_reader", NS) == [] and root.findall("f:participant", NS) == []

    blocking = writers[0].find("f:qos/f:reliability/f:max_blocking_time", NS)
    wait_ns = (int(blocking.findtext("f:sec", "0", NS)) * 1_000_000_000
               + int(blocking.findtext("f:nanosec", "0", NS)))
    assert wait_ns > FASTDDS_DEFAULT_BLOCKING_NS
    assert writers[0].findtext("f:qos/f:reliability/f:kind", None, NS) == "RELIABLE"


def test_fast_dds_is_pointed_at_the_generated_profile():
    envs = [i for i in _instructions() if i.startswith("ENV FASTRTPS_DEFAULT_PROFILES_FILE")]
    assert envs == [f"ENV FASTRTPS_DEFAULT_PROFILES_FILE={PROFILE_PATH}"]
