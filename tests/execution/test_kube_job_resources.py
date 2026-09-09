# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What a job was GIVEN, read off its Job template, and the durations a cluster states in text.

Both feed the per-job cpu/memory the campaign view shows, and both are where that number goes
wrong quietly rather than loudly: a sum over the wrong container set is still a number, and a
duration this cannot read is still a string. The cases below are the ones where a plausible
implementation produces a wrong answer that looks right.

No cluster: a Job template is a shape, and these are pure functions over it.
"""

import types

import pytest

from robovast.execution.cluster_execution.kube_client import parse_duration, workload_resources


def _container(name, *, sidecar=False, requests=None, limits=None):
    return types.SimpleNamespace(
        name=name,
        restart_policy="Always" if sidecar else None,
        resources={"requests": requests or {}, "limits": limits or {}})


def _template(regular=(), init=()):
    """A Job's ``spec.template`` — the same shape as a pod, before any pod exists."""
    return types.SimpleNamespace(
        spec=types.SimpleNamespace(containers=list(regular), init_containers=list(init)))


def _campaign_job():
    """The real shape: one regular container, two native sidecars, one staging init container.

    Sized as a live campaign's job is, and deliberately so: the simulator and the system under
    test carry the bulk of the reservation, which is what makes reading ``spec.containers``
    alone a threefold error rather than a rounding one.
    """
    return _template(
        regular=[_container("robovast",
                            requests={"cpu": "1.429", "memory": "1073741824"},
                            limits={"cpu": "2", "memory": "1073741824"})],
        init=[
            # Staging only: it populates /config and exits, so it holds nothing for the run.
            _container("s3-init"),
            _container("sut", sidecar=True,
                       requests={"cpu": "2.495", "memory": "2147483648"},
                       limits={"cpu": "2.495", "memory": "2147483648"}),
            _container("simulation", sidecar=True,
                       requests={"cpu": "425m", "memory": "1476395008"},
                       limits={"cpu": "2", "memory": "1476395008"}),
        ])


def test_native_sidecars_are_in_the_sum():
    """The reservation is mostly NOT in ``spec.containers``.

    The simulator and the system under test are declared as init containers with
    ``restartPolicy: Always``, so a sum over ``spec.containers`` sees only the scenario
    container. It would report a ceiling of 2 cores against a pod that holds 6.495 -- and a job
    using 3 would render as 150% of its limit rather than under half of it.
    """
    given = workload_resources(_campaign_job())

    assert given["cpu_limit"] == pytest.approx(6.495)
    assert given["memory_limit"] == 1073741824 + 2147483648 + 1476395008


def test_request_and_limit_are_summed_apart():
    """They differ, which is the whole reason both are reported.

    One sidecar reserves 425m and may burst to 2 cores. Collapsing the two would answer only
    one of the two questions the meter exists for -- whether the reservation was right, and
    whether the job is near being throttled.
    """
    given = workload_resources(_campaign_job())

    assert given["cpu_request"] == pytest.approx(1.429 + 2.495 + 0.425)
    assert given["cpu_limit"] == pytest.approx(6.495)
    # Memory request == limit by construction (``stamp_resources``), so the meter has two
    # numbers to show there and three for cpu. Pinned because the display depends on it.
    assert given["memory_request"] == given["memory_limit"]


def test_a_missing_cpu_limit_makes_only_the_cpu_ceiling_unknown():
    """An open cpu limit means the whole node, so no finite sum is the truth.

    Reporting the containers that did state one would claim a ceiling BELOW the real one, and
    every job under it would read as closer to throttling than it is. The other three figures
    are unaffected: a reader still learns the memory ceiling and both reservations.
    """
    template = _template(
        regular=[_container("a", requests={"cpu": "1", "memory": "1000"},
                            limits={"memory": "1000"})],
        init=[_container("b", sidecar=True, requests={"cpu": "1", "memory": "1000"},
                         limits={"cpu": "2", "memory": "1000"})])

    given = workload_resources(template)

    assert given["cpu_limit"] is None
    assert given["cpu_request"] == pytest.approx(2)
    assert given["memory_limit"] == 2000


def test_container_names_scope_the_sum_to_what_was_measured():
    """The denominator must cover the same containers as the numerator.

    A measurement that reports one container, divided by a three-container ceiling, is a ratio
    between two different things -- and it reads as a comfortably idle job.
    """
    given = workload_resources(_campaign_job(), ["robovast"])

    assert given["cpu_limit"] == pytest.approx(2)
    assert given["memory_limit"] == 1073741824


def test_a_template_with_no_containers_answers_unknown_not_zero():
    """Zero is a claim; this is the absence of one.

    A Job fake without a pod spec reaches here from the job listing, and a zeroed ceiling would
    divide every measurement by nothing.
    """
    assert workload_resources(_template()) == {
        "cpu_request": None, "cpu_limit": None,
        "memory_request": None, "memory_limit": None}
    # And the shape the listing actually hands it when a Job carries no template spec at all.
    assert workload_resources(types.SimpleNamespace(metadata=None))["cpu_limit"] is None
    assert workload_resources(None)["cpu_limit"] is None


@pytest.mark.parametrize("text,seconds", [
    # What metrics-server actually states: fractional seconds, sometimes to the nanosecond.
    ("10.549007488s", 10.549007488),
    ("17.265s", 17.265),
    ("1m30s", 90.0),
    ("500ms", 0.5),
    ("2h", 7200.0),
])
def test_durations_kubernetes_states_as_text(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "1Gi", "10s5", "soon", None, 17.265])
def test_anything_that_is_not_a_whole_duration_yields_the_default(text):
    """A partial parse would put a made-up number where a caller asked for a measured one.

    ``"1Gi"`` is the case that matters: a resource quantity and a duration are different
    syntaxes that both turn up on the same objects, and reading one as the other would set a
    cache lifetime from a memory size.
    """
    assert parse_duration(text, default=15.0) == 15.0
