"""Version policy and the migration ladders — one package, so adding a step is one place.

Public API:

* :func:`upgrade_config` / :func:`upgrade_config_file` — bring a ``.vast`` forward.
* :func:`read_vast` / :func:`classify_config` — read an archived ``.vast`` and say what
  version it is and whether it can be carried forward.
* :data:`SUPPORTED_CONFIG_VERSION`, :data:`BASELINE_CONFIG_VERSION` — the config surface.
* the refusals: :class:`ConfigTooNew`, :class:`ConfigTooOld`, :class:`UnmigratableConfig`.

``README.md`` in this directory is the entry point for adding a migration step, and lists
all three version surfaces (config, campaign store, host<->container).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import (BASELINE_CONFIG_VERSION, MIGRATION_MARKER, SUPPORTED_CONFIG_VERSION,
                     ConfigTooNew, ConfigTooOld, ConfigVersionError, UnmigratableConfig,
                     config_version, find_migration_markers, migration_marker, needs_upgrade,
                     upgrade_config)

logger = logging.getLogger(__name__)

#: What :func:`classify_config` found. Every state but :data:`CONFIG_CURRENT` and
#: :data:`CONFIG_UPGRADABLE` is a refusal ``upgrade_config`` raises; what a caller *does*
#: about each is policy and stays with the caller.
CONFIG_CURRENT = "current"              # already at the supported version
CONFIG_UPGRADABLE = "upgradable"        # older, and the ladder carries it forward
CONFIG_NEWER = "newer"                  # written by a newer robovast
CONFIG_UNVERSIONED = "unversioned"      # no ``version:``, or one that is not an integer
CONFIG_TOO_OLD = "too_old"              # below the oldest version the ladder starts from
CONFIG_UNMIGRATABLE = "unmigratable"    # a step refused; the config uses something removed

__all__ = [
    "BASELINE_CONFIG_VERSION",
    "CONFIG_CURRENT",
    "CONFIG_NEWER",
    "CONFIG_TOO_OLD",
    "CONFIG_UNMIGRATABLE",
    "CONFIG_UNVERSIONED",
    "CONFIG_UPGRADABLE",
    "ConfigClassification",
    "MIGRATION_MARKER",
    "SUPPORTED_CONFIG_VERSION",
    "ConfigTooNew",
    "ConfigTooOld",
    "ConfigVersionError",
    "UnmigratableConfig",
    "classify_config",
    "config_version",
    "find_migration_markers",
    "migration_marker",
    "needs_upgrade",
    "read_vast",
    "upgrade_config",
    "upgrade_config_file",
]


@dataclass(frozen=True)
class ConfigClassification:
    """What version an archived ``.vast`` is, and whether it can be carried forward."""

    #: One of the ``CONFIG_*`` states.
    state: str
    #: The ``version:`` as the file states it -- ``None`` when absent, and not necessarily an
    #: integer, which is itself one of the things a caller has to report.
    version: Any
    #: The ladder steps that would run, in order. Empty unless the state is
    #: :data:`CONFIG_UPGRADABLE`.
    steps: list = field(default_factory=list)
    #: The refusal :func:`upgrade_config` raises for this config, empty when it does not
    #: refuse. It names the version found and what to do, so a caller reports it rather than
    #: composing a second wording of the same refusal.
    message: str = ""


def classify_config(raw: dict) -> ConfigClassification:
    """Which of the six states *raw* is in, from what the ladder itself says.

    Asked by *doing* the migration rather than by inspecting the version first: the version is
    untrusted input from a file this robovast did not write, and every way it can be wrong --
    absent, a string, a float, ahead, behind the baseline -- is already a distinct refusal
    :func:`upgrade_config` raises with a message naming it. Pre-classifying on the version
    duplicates that ladder, and a pre-classification that disagrees with it reports the
    opposite of what is wrong.

    Does not mutate *raw*, and raises nothing: this is the question asked *about* a config
    that may well be unusable.
    """
    version = config_version(raw)
    try:
        _, applied = upgrade_config(raw)
    except ConfigTooNew as e:
        return ConfigClassification(CONFIG_NEWER, version, message=str(e))
    except ConfigTooOld as e:
        return ConfigClassification(CONFIG_TOO_OLD, version, message=str(e))
    except UnmigratableConfig as e:
        return ConfigClassification(CONFIG_UNMIGRATABLE, version, message=str(e))
    except ConfigVersionError as e:
        return ConfigClassification(CONFIG_UNVERSIONED, version, message=str(e))
    if not applied:
        return ConfigClassification(CONFIG_CURRENT, version)
    return ConfigClassification(CONFIG_UPGRADABLE, version, steps=applied)


def read_vast(vast_path) -> dict:
    """The first YAML document of an archived ``.vast``, unvalidated.

    Read directly rather than through ``load_config``: this is a *diagnosis* of a file that may
    well be too old to validate, and the strict reader would raise before the report could say
    so -- turning the answer into the failure it was asked about.
    """
    import yaml  # pylint: disable=import-outside-toplevel

    with open(vast_path, "r", encoding="utf-8") as handle:
        documents = list(yaml.safe_load_all(handle))
    return (documents[0] if documents else None) or {}


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
    # Wide enough that nothing is re-wrapped. Left at the default, ruamel reflows any flow
    # mapping past ~80 columns -- including ones the step never touched -- so a migration
    # that changed two keys rewrites lines all over the file, and the diff stops showing
    # what the migration did.
    yaml.width = 4096
    with open(path, "r", encoding="utf-8") as handle:
        documents = list(yaml.load_all(handle))
    if not documents or documents[0] is None:
        raise ConfigVersionError(f"No documents found in {path}")

    upgraded, applied = upgrade_config(documents[0])
    documents[0] = upgraded
    if write and applied:
        if _only_the_version_moved(path, upgraded):
            _rewrite_version_line(path, upgraded.get("version"))
        else:
            with open(path, "r", encoding="utf-8") as handle:
                before = handle.read()
            with open(path, "w", encoding="utf-8") as handle:
                yaml.dump_all(documents, handle)
            with open(path, "r", encoding="utf-8") as handle:
                _warn_about_lost_comments(path, before, handle.read())
    return upgraded, applied


def _comment_counts(text: str):
    """Every comment in *text*, counted. Text, because that is what the author will read."""
    import collections  # pylint: disable=import-outside-toplevel
    import re  # pylint: disable=import-outside-toplevel

    return collections.Counter(m.group(0).strip() for m in re.finditer(r"#.*", text))


def _warn_about_lost_comments(path, before: str, after: str) -> None:
    """Say so when a rewrite dropped or doubled a comment, quoting the ones it did.

    A step that restructures has to move the notes written against what it moved, and getting
    that wrong is silent: the file still loads, still means the same thing, and is missing the
    only record of why a value is what it is. Checked here rather than in each step, because
    it is the rewrite that loses them and every step reaches this one.
    """
    before_counts, after_counts = _comment_counts(before), _comment_counts(after)
    lost, gained = before_counts - after_counts, after_counts - before_counts
    for label, counts in (("no longer in", lost), ("duplicated in", gained)):
        if not counts:
            continue
        quoted = "; ".join(sorted(counts)[:3])
        logger.warning(
            "%s: %d comment(s) %s the upgraded file, e.g. %s. The migration moved what they "
            "were written against without moving them; recover them from version control.",
            path, sum(counts.values()), label, quoted)


def _only_the_version_moved(path, upgraded) -> bool:
    """True when the ladder changed nothing in *path* but the version number.

    Most steps leave most files alone -- they restructure one block, and a campaign without
    that block is carried forward unchanged. Round-tripping such a file still rewrites it,
    because ruamel normalises what it re-emits: a flow mapping padded out to align a column
    loses the padding, and a nested flow map loses its braces. Both are equivalent YAML and
    neither is what the migration did, so the diff stops showing the change and starts hiding
    it. Rewriting one line instead keeps a no-op migration looking like one.
    """
    import yaml as _plain  # pylint: disable=import-outside-toplevel

    try:
        with open(path, "r", encoding="utf-8") as handle:
            before = next(iter(_plain.safe_load_all(handle)), None)
    except Exception:  # pylint: disable=broad-except
        return False
    if not isinstance(before, dict):
        return False
    return _without_version(before) == _without_version(dict(upgraded))


def _without_version(document: dict) -> dict:
    """*document* minus the one key the ladder is always allowed to change."""
    return {k: v for k, v in document.items() if k != "version"}


def _rewrite_version_line(path, version) -> None:
    """Replace the top-level ``version:`` line in *path*, touching nothing else."""
    import re  # pylint: disable=import-outside-toplevel

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    new_text, count = re.subn(r"(?m)^version:[ \t]*\d+[ \t]*$", f"version: {version}", text, count=1)
    if count == 0:                       # no line to replace: the file left it implicit
        new_text = f"version: {version}\n" + text
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(new_text)
