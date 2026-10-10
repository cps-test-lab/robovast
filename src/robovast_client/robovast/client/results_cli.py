# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``vast results`` -- work on a results directory on THIS machine.

Every verb here names a path and none of them needs a login: this is the local half of the
tool, for someone holding a campaign or a results tree. The group is defined here so that
it exists wherever a verb for it is installed, and its verbs arrive with what they need:

* ``build`` with ``robovast-data`` (``pip install robovast-client[data]``): a campaign's
  tables, built and compacted from a directory or a downloaded archive;
* ``publish``, ``generate-metadata``, ``merge-campaigns`` and the rest with the full
  ``robovast`` distribution.

A client-only install lists none of them, which is the honest signal that none of it is a
service operation. What acts on a campaign on a service is ``vast campaign``.
"""

import click

from robovast.client.lazy_group import LazyPluginGroup

#: Entry-point group for subcommands that attach to ``vast results``.
RESULTS_PLUGIN_GROUP = "robovast.results_plugins"


@click.group(cls=LazyPluginGroup, plugin_group=RESULTS_PLUGIN_GROUP)
def results():
    """Work on a results directory on THIS machine.

    Every verb names a path and none needs a login. ``build`` arrives with
    ``robovast-data`` (``pip install robovast-client[data]``); ``publish``,
    ``generate-metadata`` and ``merge-campaigns`` with the full ``robovast`` distribution.
    What acts on a campaign on a service is ``vast campaign``.
    """
