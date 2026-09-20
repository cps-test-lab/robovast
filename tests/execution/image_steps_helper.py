# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Image steps for postprocessing-Job tests that have no campaign tree to build them from.

A Job's image steps come from the campaign's ``.vast``; these tests exercise the Job around
them, so they get the steps a rosbag conversion would render, for the Job's own paths.
"""

from robovast.execution.cluster_execution import postprocess_job as pj
from robovast.results_processing.postprocessing_plugins import (ImageContext, ImageStep,
                                                                RosbagsProcess)

#: A campaign's image commands, as ``image_commands_for`` returns them.
CMDS = [{"rosbags_process": {"plugins": [{"type": "rosout_to_csv"}]}}]


def steps(campaign_id="c1", plugins=None, force=False, tolerate_under=()):
    """The rendered steps of one rosbag conversion over *campaign_id*'s tree in the pod."""
    root = pj.campaign_dir(campaign_id)
    ctx = ImageContext(campaign_dir=root,
                       provenance_file=f"{root}/{pj._IMAGE_PROVENANCE_REL}",  # pylint: disable=protected-access
                       force=force, tolerate_under=tuple(tolerate_under))
    argv = RosbagsProcess().image_command(ctx, plugins=plugins or [{"type": "rosout_to_csv"}])
    return [ImageStep(name="rosbags_process", argv=argv)]


def stub_image_steps(monkeypatch):
    """Build the Job's steps without reading a ``.vast``: what ``image_steps_for`` would."""
    monkeypatch.setattr(
        pj, "image_steps_for",
        lambda campaign_id, root, cmds, force=False, tolerate_under=():
        steps(campaign_id, force=force, tolerate_under=tolerate_under) if cmds else [])
