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
from . import v2_to_v3  # noqa: F401
# <new-migration-import>

#: The oldest version the ladder starts from. Raising this is a deliberate, announced act
#: meaning "we no longer migrate from below here"; the fallback for such a campaign is to
#: check out the ``robovast_revision`` its results recorded.
BASELINE_CONFIG_VERSION = 1

#: The version a config is brought to, and the only one authoring accepts.
SUPPORTED_CONFIG_VERSION = 3

#: ``_MIGRATIONS[i]`` upgrades a config from ``BASELINE_CONFIG_VERSION + i`` to
#: ``+ i + 1``. **Append only; never edit an existing entry** -- an edit changes what an
#: already-migrated campaign would become, and each step's golden fixture exists to catch
#: exactly that.
_MIGRATIONS = [
    v1_to_v2.migrate,
    v2_to_v3.migrate,
    # <new-migration-entry>
]

# One step per version increment. Catches "bumped the constant, forgot the step" and its
# converse at import time, so any test run fails rather than a campaign a year from now.
assert len(_MIGRATIONS) == SUPPORTED_CONFIG_VERSION - BASELINE_CONFIG_VERSION


#: Sentinel a step leaves where it could not carry something forward. Chosen to be **invalid by
#: construction**: it is a mapping under a reserved key, so wherever it lands the schema rejects
#: it -- as an unexpected key where a key was expected, or as the wrong type where a value was.
#:
#: That is the whole point. A partly-migrated config that happened to load would run a *different
#: experiment* silently, which is worse than any refusal. The marker makes the file a work order
#: rather than a config, and `vast configuration validate` lists every one still outstanding.
MIGRATION_MARKER = "TODO_MIGRATE"


def migration_marker(reason: str, was=None) -> dict:
    """The value a step leaves in place of something it cannot migrate.

    Carries *why* and, when it helps, *what was there* -- because whoever resolves this is reading
    a file they did not write, about a version of robovast that no longer exists. A bare "fix me"
    would make them go and find the old schema.
    """
    marker = {"reason": reason}
    if was is not None:
        marker["was"] = was
    return {MIGRATION_MARKER: marker}


def find_migration_markers(raw, path: str = "") -> "list[tuple[str, str]]":
    """``[(dotted path, reason)]`` for every unresolved marker in *raw*.

    Walks rather than trusting a step to report its own locations: a step may leave several, and a
    caller needs each one's position to tell somebody where to look.
    """
    found = []
    if isinstance(raw, dict):
        if MIGRATION_MARKER in raw and isinstance(raw[MIGRATION_MARKER], dict):
            found.append((path or "<root>", raw[MIGRATION_MARKER].get("reason", "")))
            return found
        for key, value in raw.items():
            found.extend(find_migration_markers(value, f"{path}.{key}" if path else str(key)))
    elif isinstance(raw, list):
        for index, value in enumerate(raw):
            found.extend(find_migration_markers(value, f"{path}[{index}]"))
    return found


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

    def __init__(self, message: str, *, partial: dict = None, reached: int = 0,
                 capability: str = ""):
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
        try:
            config = step(config)
        except UnmigratableConfig as e:
            # Re-raised carrying how far the ladder got and what it produced, because the caller's
            # useful move is to hand that partial config to a human -- not to report a dead end.
            # A step only knows its own transform; the position in the ladder is knowable here.
            raise UnmigratableConfig(
                str(e),
                partial=e.partial if e.partial is not None else config,
                reached=step_version,
                capability=e.capability) from e
        applied.append(f"{step_version}_to_{step_version + 1}")
    return config, applied
