"""Version policy and the migration ladders — one package, so adding a step is one place.

Public API:

* :func:`upgrade_config` / :func:`upgrade_config_file` — bring a ``.vast`` forward.
* :data:`SUPPORTED_CONFIG_VERSION`, :data:`BASELINE_CONFIG_VERSION` — the config surface.
* the refusals: :class:`ConfigTooNew`, :class:`ConfigTooOld`, :class:`UnmigratableConfig`.

``README.md`` in this directory is the entry point for adding a migration step, and lists
all four version surfaces (config, campaign store, analysis DB, host<->container).
"""

from .config import (BASELINE_CONFIG_VERSION, MIGRATION_MARKER, SUPPORTED_CONFIG_VERSION,
                     ConfigTooNew, ConfigTooOld, ConfigVersionError, UnmigratableConfig,
                     config_version, find_migration_markers, migration_marker, needs_upgrade,
                     upgrade_config)

__all__ = [
    "BASELINE_CONFIG_VERSION",
    "MIGRATION_MARKER",
    "SUPPORTED_CONFIG_VERSION",
    "ConfigTooNew",
    "ConfigTooOld",
    "ConfigVersionError",
    "UnmigratableConfig",
    "config_version",
    "find_migration_markers",
    "migration_marker",
    "needs_upgrade",
    "upgrade_config",
    "upgrade_config_file",
]


def upgrade_config_file(path, *, write: bool = False):
    """Upgrade the ``.vast`` at *path*, **preserving comments**; return ``(config, applied)``.

    Separate from :func:`upgrade_config` because the two serve different callers and only
    one of them can afford the dependency:

    * :func:`upgrade_config` takes a plain dict and is what *reading* uses -- displaying an
      archived campaign, importing one, staging a retrigger. Comments are irrelevant there.
    * this function rewrites a file a **human will then edit**, so dropping their comments
      is not acceptable. The ``.vast`` files in this tree carry load-bearing ones (one
      explains a whole image strategy), and the manual-migration workflow hands the result
      to a person precisely when decisions are needed.

    ``ruamel.yaml`` round-trip mode is what keeps them. Its ``CommentedMap`` is a ``dict``
    subclass, so the pure ``dict -> dict`` steps work on it unchanged and comments attached
    to untouched keys survive.
    """
    from ruamel.yaml import YAML  # pylint: disable=import-outside-toplevel

    yaml = YAML()
    yaml.preserve_quotes = True
    with open(path, "r", encoding="utf-8") as handle:
        documents = list(yaml.load_all(handle))
    if not documents or documents[0] is None:
        raise ConfigVersionError(f"No documents found in {path}")

    upgraded, applied = upgrade_config(documents[0])
    documents[0] = upgraded
    if write and applied:
        with open(path, "w", encoding="utf-8") as handle:
            yaml.dump_all(documents, handle)
    return upgraded, applied
