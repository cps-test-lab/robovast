# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Where the roqsim backend tells the simulator its config is, inside the container.

A path in the ``.vast`` is relative to the ``.vast``; the campaign's files are mounted at
``/config``. The rewrite must keep the path the author wrote, and a path that leaves the
``.vast``'s directory was never staged, so it is refused rather than rewritten into one that
does not exist.
"""

import pytest

from robovast_sim_roqsim.backend import _config_in_container


@pytest.mark.parametrize("authored,expected", [
    ("worlds/depot.yaml", "/config/worlds/depot.yaml"),
    ("./worlds/depot.yaml", "/config/worlds/depot.yaml"),
    (".hidden/depot.yaml", "/config/.hidden/depot.yaml"),
    ("/abs/depot.yaml", "/abs/depot.yaml"),
])
def test_a_config_path_lands_under_the_mount_as_written(authored, expected):
    assert _config_in_container(authored) == expected


def test_a_config_outside_the_vast_directory_is_refused():
    with pytest.raises(ValueError, match=r"\.\./shared/depot\.yaml"):
        _config_in_container("../shared/depot.yaml")
