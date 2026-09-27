# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""An image build's status, as the fields a caller reads.

The MCP tool ``get_image_build_status`` returns it and ``vast image status --json`` prints it.
"""

from robovast.execution.wait_exit import ImageWaitExit


def build_status_report(client, build_id: str) -> dict:
    """``{build_id, tag, phase, done, cached, image_ref, next_step[, error_detail]}``.

    Raises whatever the client raises; the caller decides how a failure is reported.
    """
    s = client.get_image_build_status(build_id)
    out = {"build_id": s.build_id, "tag": s.tag, "phase": s.phase,
           "done": s.done, "cached": s.cached, "image_ref": s.image_ref,
           "next_step": build_next_step(s)}
    if s.error is not None:
        out["error_detail"] = s.error.model_dump()
    return out


def build_next_step(status) -> str:
    """What to do about the build state just reported.

    Each phase wants a different action: a running build a wait rather than a second build;
    a *blocked* one neither, since its pod is not running and its inputs are not the
    problem; a failed one the diagnosis rather than a retry of identical inputs; a finished
    one the run.
    """
    if status.phase == "blocked":
        # The builder pod cannot start, so neither waiting nor rebuilding produces an image,
        # and the build log is empty.
        return ("the build pod cannot start -- read error_detail above; it names the image "
                "or the capacity at fault. Nothing in the project's build: section is "
                "involved, and the build fails on its own shortly if this does not clear")
    if not status.done:
        return (f"run in the background: vast image wait {status.build_id} --interval 5 "
                f"({ImageWaitExit.summary()})")
    if status.phase == "failed":
        return (f"read error_detail above, then "
                f"get_image_build_log(build_id='{status.build_id}', summarize=True) "
                f"for the builder's own output")
    return ("the image is ready — start_campaign(...) to run it, or "
            "exec_in_container(...) to look inside it")
