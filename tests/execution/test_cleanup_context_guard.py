"""``cluster cleanup`` and ``cluster jobs-cleanup`` read ``--vast`` for one check.

A ``.vast`` with per-cluster resource lists for several contexts, given without
``--context``, would leave the command on whichever context is active; both commands
refuse that before they reach a cluster.
"""

from click.testing import CliRunner
import pytest

from robovast.execution.cluster_execution import cli

MULTI = """\
execution:
  resources:
    cpu:
      - cluster-a: 2
      - cluster-b: 4
"""


@pytest.fixture
def multi_vast(tmp_path):
    path = tmp_path / "multi.vast"
    path.write_text(MULTI)
    return str(path)


def _no_cluster(*_args, **_kwargs):
    raise AssertionError("the command reached the cluster")


@pytest.mark.parametrize("command", [cli.run_cleanup, cli.cleanup])
def test_several_contexts_without_context_are_refused(command, multi_vast, monkeypatch):
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.kubernetes.get_kubernetes_client", _no_cluster)
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_setup.delete_server", _no_cluster)
    result = CliRunner().invoke(command, ["--vast", multi_vast])
    assert result.exit_code == 2, result.output
    assert "--context" in result.output
    assert "cluster-a" in result.output and "cluster-b" in result.output
