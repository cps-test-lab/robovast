# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""The service's identity and its backend's capacity, as the fields a caller reads.

The MCP tools ``get_service_info`` and ``get_resource_usage`` return these, and
``vast service info --json`` / ``vast service resources --json`` print them.
"""


def service_info_report(client) -> dict:
    """Which service answers, which code it runs, and which backend it drives.

    A field the service could not state is absent rather than empty or ``None``, so a
    caller never reads a placeholder as a value.

    Raises whatever the client raises; the caller decides how a failure is reported.
    """
    v = client.version()
    info = {
        "code_version": v.robovast_version,
        "api_version": v.api_version,
        "backend": v.backend,
        "results_address": v.results_address,
        "sources_address": v.sources_address,
    }
    for key in ("code_revision", "package_version", "built_at"):
        if getattr(v, key, ""):
            info[key] = getattr(v, key)
    # Absent: no origin to declare (unpublished, or bound to a wildcard).
    if v.web_base:
        info["web_base"] = v.web_base
    # Only a Kubernetes backend has these; elsewhere they would read as "unknown".
    if v.backend == "kubernetes":
        info.update({
            "kube_context": v.kube_context,
            "kube_context_source": v.kube_context_source,
            "namespace": v.namespace,
            "in_pod": v.in_pod,
            "api_server": v.api_server,
        })
    # ``None`` is "the service did not say", which is not ``False``.
    if v.can_build_images is not None:
        info["can_build_images"] = v.can_build_images
        if not v.can_build_images and v.build_unavailable:
            info["build_unavailable"] = v.build_unavailable
    if v.can_schedule is not None:
        info["can_schedule"] = v.can_schedule
    return info


def resource_usage_report(client) -> dict:
    """The backend's capacity and use now, every ``ResourceUsage`` field.

    Raises whatever the client raises; the caller decides how a failure is reported.
    """
    return client.resource_usage().model_dump()
