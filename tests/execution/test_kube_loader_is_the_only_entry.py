# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every cluster-touching path must load kube config through the shared loader.

:func:`robovast.execution.cluster_execution.kube_client.load_kube_config` is where the
process-wide **connect timeout** is installed, and its docstring called itself "the one entry point every
cluster-touching path already goes through, so the policy cannot be missed". It was
missed, in ten places: they called ``kubernetes.config.load_kube_config`` directly, so
every API call on those paths ran with ``timeout=None``.

The visible cost: an off-cluster ``vast serve --backend cluster`` against an unreachable
cluster sat on a TCP connect for over two minutes and then died in a urllib3 traceback,
rather than saying in seconds which cluster it could not reach. A documented invariant
that nothing checks is a comment; this makes it a test.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "robovast"
#: The one module allowed to call the generated client's loader.
CANONICAL = SRC / "execution" / "cluster_execution" / "kube_client.py"
_DIRECT_CALL = re.compile(r"^[^#]*\bconfig\.load_kube_config\s*\(")
_DIRECT_INCLUSTER = re.compile(r"^[^#]*\bconfig\.load_incluster_config\s*\(")


def _offenders(pattern):
    hits = []
    for path in SRC.rglob("*.py"):
        if path == CANONICAL:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    return hits


def test_nothing_bypasses_the_shared_kube_loader():
    offenders = _offenders(_DIRECT_CALL)
    assert not offenders, (
        "these call kubernetes.config.load_kube_config directly, so their API calls get "
        "no connect timeout and can hang for minutes against an unreachable cluster. "
        "Use cluster_execution.kube_client.load_kube_config instead:\n  "
        + "\n  ".join(offenders))


def test_nothing_bypasses_the_shared_loader_for_in_cluster_config():
    offenders = _offenders(_DIRECT_INCLUSTER)
    assert not offenders, (
        "these load in-cluster config directly and so skip the connect-timeout policy; "
        "cluster_execution.kube_client.load_kube_config already tries in-cluster "
        "first:\n  "
        + "\n  ".join(offenders))


def test_the_loader_installs_a_bounded_connect_timeout():
    from robovast.execution.cluster_execution import kube_client as kube
    assert kube.CONNECT_TIMEOUT_SECONDS > 0
    # Overridable, because "the cluster is simply slow" and "the cluster is down" want
    # different limits, and the error message points at this knob.
    assert "ROBOVAST_KUBE_CONNECT_TIMEOUT" in pathlib.Path(kube.__file__).read_text()


def test_an_unreachable_cluster_is_not_reported_as_no_service_deployed():
    """Absence and failure must stay distinguishable.

    ``read_service_config_from_cluster`` returns ``(None, {})`` for "no service is
    deployed", which makes ``vast serve`` suggest running ``cluster setup``. A connection
    failure must not collapse into that answer, or the advice is nonsense for a cluster
    that cannot be reached at all.
    """
    import inspect

    from robovast.execution.cluster_execution import service_deploy
    source = inspect.getsource(service_deploy.read_service_config_from_cluster)
    assert "HTTPError" in source, "a transport failure is not handled distinctly"
    # The 404 branch is the only one allowed to answer "nothing deployed".
    assert source.count("return None, {}") == 1
    assert "did not answer within" in source
