"""The ``.vast`` config version ladder.

One of four version surfaces in robovast; ``migrations/README.md`` lists them all and is
the entry point for adding a step to any of them.

Mirrors :mod:`robovast.common.store`'s proven shape: a supported version, an ordered
append-only list of steps, and an assert tying the two together so a forgotten step cannot
reach ``main``.
"""

# Steps are imported explicitly rather than discovered, so the ladder is auditable in one
# place. tools/new_config_migration.py appends to both this block and _MIGRATIONS below,
# keyed on the markers -- so the insertion point is stated rather than guessed at.
from . import v1_to_v2  # noqa: F401
# <new-migration-import>

#: The oldest version the ladder starts from. Raising this is a deliberate, announced act
#: meaning "we no longer migrate from below here"; the fallback for such a campaign is to
#: check out the ``robovast_revision`` its results recorded.
BASELINE_CONFIG_VERSION = 1

#: The version a config is brought to, and the only one authoring accepts.
SUPPORTED_CONFIG_VERSION = 2

#: ``_MIGRATIONS[i]`` upgrades a config from ``BASELINE_CONFIG_VERSION + i`` to
#: ``+ i + 1``. **Append only; never edit an existing entry** -- an edit changes what an
#: already-migrated campaign would become, and each step's golden fixture exists to catch
#: exactly that.
_MIGRATIONS = [
    v1_to_v2.migrate,
    # <new-migration-entry>
]

# One step per version increment. Catches "bumped the constant, forgot the step" and its
# converse at import time, so any test run fails rather than a campaign a year from now.
assert len(_MIGRATIONS) == SUPPORTED_CONFIG_VERSION - BASELINE_CONFIG_VERSION


class ConfigVersionError(ValueError):
    """Base for every refusal this module raises."""


class ConfigTooNew(ConfigVersionError):
    """The config was written by a newer robovast. No migration can fix this."""


class ConfigTooOld(ConfigVersionError):
    """The config predates :data:`BASELINE_CONFIG_VERSION`, so the ladder cannot start."""


class UnmigratableConfig(ConfigVersionError):
    """A step refused: the config uses something this version cannot express.

    Carries what a caller needs to offer the manual path (``--to-workspace``): the
    partially migrated config, how far it got, and what could not be carried.
    """

    def __init__(self, message: str, *, partial: dict, reached: int, capability: str = ""):
        super().__init__(message)
        self.partial = partial
        self.reached = reached
        self.capability = capability


def config_version(raw: dict):
    """The declared ``version:``, or ``None`` when absent."""
    return (raw or {}).get("version")


def needs_upgrade(raw: dict) -> bool:
    """True when *raw* declares a version below the supported one."""
    version = config_version(raw)
    return isinstance(version, int) and version < SUPPORTED_CONFIG_VERSION


def upgrade_config(raw: dict) -> "tuple[dict, list[str]]":
    """Bring *raw* to :data:`SUPPORTED_CONFIG_VERSION`; return ``(config, applied)``.

    ``applied`` names the steps that ran, so a caller can record *how* a campaign was
    brought forward (``config_version_from``) instead of losing that it was migrated at all.
    Does not mutate *raw*.

    Raises:
        ConfigTooNew: the version is above the supported one.
        ConfigTooOld: the version is below :data:`BASELINE_CONFIG_VERSION`.
        UnmigratableConfig: a step could not carry something forward.
    """
    version = config_version(raw)
    if version is None:
        raise ConfigVersionError(
            "config declares no 'version:'. Every .vast must state one; "
            f"the current version is {SUPPORTED_CONFIG_VERSION}.")
    if not isinstance(version, int):
        raise ConfigVersionError(
            f"config version must be an integer, got {version!r}.")
    if version > SUPPORTED_CONFIG_VERSION:
        raise ConfigTooNew(
            f"config version {version} was written by a newer robovast; this one supports "
            f"up to {SUPPORTED_CONFIG_VERSION}. A newer format cannot be migrated "
            f"backwards -- upgrade robovast instead.")
    if version < BASELINE_CONFIG_VERSION:
        raise ConfigTooOld(
            f"config version {version} is below the oldest this robovast migrates from "
            f"({BASELINE_CONFIG_VERSION}).")

    config = raw
    applied: list[str] = []
    for step_version in range(version, SUPPORTED_CONFIG_VERSION):
        step = _MIGRATIONS[step_version - BASELINE_CONFIG_VERSION]
        config = step(config)
        applied.append(f"{step_version}_to_{step_version + 1}")
    return config, applied
