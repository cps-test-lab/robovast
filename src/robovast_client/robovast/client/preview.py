# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A preview the clients wait for by polling, so a long one can say what it is waiting on.

A preview composes the sweep, and when the campaign's simulator runs in the image the campaign
builds and that image is not built yet, it builds it first. A waited call would hold one
request for the whole build and say nothing. Polled (``wait=False``), the service answers at
once with ``composing`` and the build in ``stage``, so the CLI and the MCP tool both show it,
as the web launch bar does.
"""

import time
from typing import Callable, Optional

#: How often a waiting client asks again. The service answers from what it holds, so a poll
#: costs one small request.
POLL_SECONDS = 1.0

#: The first poll after the one that starts the preview: one that needs no build or helper
#: container lands in a fraction of a second, and waiting a whole interval for it is a delay
#: the waited call never had.
FIRST_POLL_SECONDS = 0.1


class PreviewFailed(RuntimeError):
    """The preview's composition, or the image build it waited on, failed."""

    #: The message is the service's whole diagnosis; the client's frames name nothing more.
    include_traceback = False


def await_preview(client, workspace_id: str, max_configs: int = 0, path: str = "", *,
                  on_stage: Optional[Callable[[str], None]] = None,
                  deadline_s: Optional[float] = None, poll_s: float = POLL_SECONDS):
    """Poll ``preview_configurations(wait=False)`` until it is ``ready``, and return it.

    A landed answer is given once, so the one this returns is the composition its own calls
    started or resumed -- the MCP tool's repeated call picks up the preview its first call
    left composing.

    *on_stage* hears each new ``stage`` -- what the preview waits on before it can compose,
    such as the build of the campaign's image -- once. With *deadline_s*, a preview still
    ``composing`` after that many seconds is returned as it stands, for a caller that answers
    with a handle rather than holding its own caller.

    Raises:
        PreviewFailed: the preview failed, with the service's reason.
    """
    started = time.monotonic()
    said = ""
    pause = min(poll_s, FIRST_POLL_SECONDS)
    while True:
        answer = client.preview_configurations(workspace_id, max_configs=max_configs,
                                               path=path, wait=False)
        if answer.state == "failed":
            raise PreviewFailed(answer.error or "the preview failed")
        if answer.state != "composing":
            return answer
        if answer.stage and answer.stage != said:
            said = answer.stage
            if on_stage is not None:
                on_stage(answer.stage)
        if deadline_s is not None and time.monotonic() - started >= deadline_s:
            return answer
        time.sleep(pause)
        pause = poll_s
