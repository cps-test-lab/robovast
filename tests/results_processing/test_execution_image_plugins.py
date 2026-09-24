# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A postprocessing step that needs the execution image runs, whoever wrote it.

Such a step names the command to run in the image (``ExecutionImagePlugin.image_command``)
and the files it ships beside the conversion scripts. A cluster Job runs it in its image
container; ``docker_exec.sh`` runs it on a development machine. Neither names the rosbag
conversion.
"""

import os
import textwrap

import pytest

from robovast.results_processing import postprocessing_plugins as pp
from robovast.results_processing.postprocessing import (campaign_postprocessing_commands,
                                                        image_steps, needs_execution_image)

_PLUGIN = textwrap.dedent('''
    import os
    from robovast.results_processing.postprocessing_plugins import (BasePostprocessingPlugin,
                                                                    ExecutionImagePlugin)

    HERE = os.path.dirname(os.path.abspath(__file__))


    class Decode(ExecutionImagePlugin):
        def image_command(self, ctx, topic="/x"):
            return ["decode.py", "--topic", topic, ctx.campaign_dir]

        def image_files(self):
            return [os.path.join(HERE, "decode.py")]


    class Claims(BasePostprocessingPlugin):
        needs_execution_image = True

        def __call__(self, results_dir, config_dir, **kwargs):
            return True, "ran"
''')


@pytest.fixture
def campaign(tmp_path):
    """A campaign whose ``.vast`` names a local image step and the rosbag shorthand."""
    config = tmp_path / "camp" / "_config"
    config.mkdir(parents=True)
    (config / "plugin.py").write_text(_PLUGIN)
    (config / "decode.py").write_text("print('decoding')\n")
    (config / "c.vast").write_text(textwrap.dedent("""
        version: 1
        results_processing:
          postprocessing:
            - rosbags_to_csv:
                topics: [/odom]
            - ./plugin.py:Decode:
                topic: /camera
            - run_log
    """))
    return tmp_path / "camp"


def _vast(campaign):
    return str(campaign / "_config" / "c.vast")


def test_a_local_image_step_is_one_of_the_campaigns_image_steps(campaign):
    config_dir = str(campaign / "_config")
    commands = campaign_postprocessing_commands(_vast(campaign))
    image = [c for c in commands if needs_execution_image(c, config_dir)]
    assert [next(iter(c)) if isinstance(c, dict) else c for c in image] == [
        "rosbags_process", "./plugin.py:Decode"]


def test_each_step_renders_its_own_command(campaign):
    ctx = pp.ImageContext(campaign_dir="/campaign/camp")
    steps = image_steps([{"./plugin.py:Decode": {"topic": "/camera"}}],
                        str(campaign / "_config"), ctx)
    assert steps[0].argv == ["decode.py", "--topic", "/camera", "/campaign/camp"]
    assert [os.path.basename(str(f)) for f in steps[0].files] == ["decode.py"]


def test_a_parameter_the_step_does_not_take_is_refused(campaign):
    ctx = pp.ImageContext(campaign_dir="/campaign/camp")
    with pytest.raises(ValueError, match="unexpected keyword"):
        image_steps([{"./plugin.py:Decode": {"colour": "red"}}], str(campaign / "_config"), ctx)


def test_a_step_that_claims_the_image_without_a_command_is_refused(campaign):
    """Nothing could run it, so it is refused before anything is spent."""
    ctx = pp.ImageContext(campaign_dir="/campaign/camp")
    with pytest.raises(ValueError, match="ExecutionImagePlugin"):
        image_steps(["./plugin.py:Claims"], str(campaign / "_config"), ctx)


def test_the_cluster_job_runs_and_ships_a_third_party_step(campaign, monkeypatch):
    from robovast.common.index_db import DSN_ENV
    from robovast.execution.cluster_execution import postprocess_job as pj

    monkeypatch.setenv(DSN_ENV, "host=index.example.com dbname=robovast")
    cmds = pj.image_commands_for(str(campaign))
    steps = pj.image_steps_for("camp", str(campaign), cmds)
    script = pj._conversion_script(steps, campaign_id="camp")  # pylint: disable=protected-access

    assert "/scripts/ros2_exec.sh /scripts/rosbags_process.py" in script
    assert "/scripts/ros2_exec.sh /scripts/decode.py --topic /camera /campaign/camp" in script
    configmap = pj.scripts_configmap_manifest("camp", "ns", steps=steps)
    assert configmap["data"]["decode.py"] == "print('decoding')\n"
    assert "rosbags_process.py" in configmap["data"]


def test_a_shipped_file_may_not_replace_a_conversion_script(tmp_path):
    from robovast.execution.cluster_execution import postprocess_job as pj

    impostor = tmp_path / "rosbags_process.py"
    impostor.write_text("print('not the converter')\n")
    step = pp.ImageStep(name="x", argv=["rosbags_process.py"], files=[str(impostor)])
    with pytest.raises(ValueError, match="rosbags_process.py"):
        pj.scripts_configmap_manifest("camp", "ns", steps=[step])


def test_the_host_pass_leaves_out_every_image_step(campaign):
    """What the Job's image container already ran -- whichever plugins those are."""
    config_dir = str(campaign / "_config")
    commands = campaign_postprocessing_commands(_vast(campaign))
    host = [c for c in commands if not needs_execution_image(c, config_dir)]
    assert host == ["run_log", "resource_usage"]


def test_docker_exec_runs_the_steps_command_with_its_files_beside_the_scripts(
        campaign, monkeypatch):
    captured = {}

    class _Stop(BaseException):
        pass

    def _popen(cmd, cwd=None, **kwargs):
        captured["cmd"] = cmd
        captured["scripts"] = sorted(os.listdir(cwd))
        raise _Stop

    monkeypatch.setattr(pp.subprocess, "Popen", _popen)
    from robovast.results_processing.postprocessing import resolve_postprocessing_plugin
    plugin = resolve_postprocessing_plugin("./plugin.py:Decode", str(campaign / "_config"))
    with pytest.raises(_Stop):
        plugin(str(campaign), str(campaign / "_config"), topic="/camera")

    cmd = captured["cmd"]
    assert cmd[cmd.index("--input") + 1] == str(campaign)
    assert cmd[-4:] == ["decode.py", "--topic", "/camera", "/input"]
    assert "decode.py" in captured["scripts"] and "rosbags_process.py" in captured["scripts"]
