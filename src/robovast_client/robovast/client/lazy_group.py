# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A click group whose subcommands are listed without being imported.

Some subcommands cost real weight to import -- ``vast exec local`` pulls Docker and the
config schema, ``vast exec cluster setup`` pulls the Kubernetes client -- and they live in
distributions the client does not depend on. Registering them eagerly would put those
imports back on every ``vast`` invocation, since `load_plugins()` imports the group's
module each time, for a subcommand almost nobody in a given run is about to type.

Click asks for the names it can offer (``list_commands``) separately from the one it is
about to run (``get_command``), so the names come from entry-point *metadata* and only the
chosen subcommand is loaded. A subcommand that fails to import is reported and skipped,
the same way `load_plugins()` treats a missing plugin: an install without the cluster
package should be short a subcommand, not broken.

This is the *cross-distribution* mechanism only. A verb living in the same module as its
group stays an ordinary ``@group.command()`` -- entry points live in installed metadata,
so declaring a distribution's own verbs there means a pyproject edit without a reinstall
makes them vanish silently (see the note on ``run_startup_hooks`` in ``cli.py``).
"""

import click


class LazyPluginGroup(click.Group):
    """A ``click.Group`` that also offers the commands an entry-point group advertises.

    Pass the group name as ``plugin_group``; click forwards unknown decorator kwargs to
    ``cls(**attrs)``, so::

        @click.group(cls=LazyPluginGroup, plugin_group="robovast.exec_plugins")
        def execution():
            ...

    Eagerly-registered commands win over entry points of the same name, so a
    distribution cannot shadow a verb the group itself defines.
    """

    def __init__(self, *args, plugin_group="", **kwargs):
        super().__init__(*args, **kwargs)
        self.plugin_group = plugin_group

    def _plugins(self):
        from importlib.metadata import entry_points  # pylint: disable=import-outside-toplevel
        if not self.plugin_group:
            return {}
        return {ep.name: ep for ep in entry_points(group=self.plugin_group)}

    def list_commands(self, ctx):
        return sorted(set(super().list_commands(ctx)) | set(self._plugins()))

    def get_command(self, ctx, cmd_name):
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        entry = self._plugins().get(cmd_name)
        if entry is None:
            return None
        try:
            return entry.load()
        except Exception as exc:  # noqa: BLE001 - a missing package is not a crash
            click.echo(f"Warning: '{cmd_name}' could not be loaded: {exc}", err=True)
            return None
