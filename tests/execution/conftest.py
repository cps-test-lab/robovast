# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Shared stubs for the cluster-execution tests.

Everything here exists so a test can be about the one thing it is named for.
"""

import pytest

from robovast.common.execution import COMPAT_VERSION, COMPAT_VERSION_LABEL


@pytest.fixture(autouse=True)
def _image_speaks_this_protocol(monkeypatch):
    """Let the submission-time image-protocol check pass without a registry.

    Building a `BatchJobRunner` now asks the registry what protocol its image speaks, and
    refuses the campaign when nothing answers -- deliberately, because a check that could not
    read the image has established nothing. In a test there is no registry to answer, so
    without this every runner construction would fail on a question no test here is about.

    Autouse rather than opt-in: the alternative is remembering to add it to each of the several
    helpers that build a runner, and forgetting reads as a failure in whatever the test *was*
    about. The tests that ARE about this check stub it themselves, which overrides this.
    """
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.registry_client.manifest_labels",
        lambda ref, **kw: {COMPAT_VERSION_LABEL: str(COMPAT_VERSION)})
