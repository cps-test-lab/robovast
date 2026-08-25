"""Config version 1 -> 2: fold the execution-level image/resources/secondary_containers
and the top-level ``build:`` section into a single ``execution.containers`` mapping.

This is the transform the ``_V1_MIGRATION`` text in :mod:`robovast.common.config` has
always described. It became code when version 1 campaigns had to stay readable and
re-runnable; the text remains the human-facing explanation.

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config` --
see ``migrations/README.md`` for why, and ``test_migration_purity`` for what enforces it.

**Deep-copy the input and mutate the copy in place; never rebuild it with ``dict(raw)``.**
When the caller is ``upgrade_config_file`` the mapping is a ``ruamel`` ``CommentedMap``
carrying the author's comments, and ``dict(mapping)`` silently returns a plain dict --
dropping every comment in a file a human is about to edit. ``deepcopy`` keeps them.
"""

import copy

#: Keys a v1 ``execution`` block carried that belong to the scenario container in v2.
_SCENARIO_KEYS = ("image", "resources")

#: Keys that appear *beside* a container name in a v1 ``secondary_containers`` entry, and so
#: cannot themselves be the name. v1 knew only ``resources``; the others are listed because
#: the v2 vocabulary accepts them and an entry carrying one must not have it read as a name.
_V1_SIBLING_KEYS = frozenset({"resources", "command", "image", "system_packages",
                              "python_packages", "env"})

#: Keys of a v1 ``build:`` section, mapped to their v2 container key. ``tag`` and
#: ``base_image`` are absent on purpose: in v2 the tag is derived from the container name
#: and ``image`` *is* the base, so both are dropped rather than carried.
_BUILD_KEY_MAP = {
    "base_image": "image",
    "system_packages": "system_packages",
    "python_packages": "python_packages",
}


def _secondary_entries(raw_list):
    """Yield ``(name, config)`` from a v1 ``secondary_containers`` list.

    Mirrors the authoritative v1 reader, ``normalize_secondary_containers`` as it stood at
    commit ``cdbf7da^`` -- the commit that folded these into ``execution.containers``. Taken
    from that function rather than inferred from the campaigns lying around, because some
    ``.vast`` files reachable from here are interim snapshots from branches that were never a
    released format, and fitting a permanent, append-only migration to one of those would
    encode a shape that never existed.

    It documented **three** input shapes, all of which really occur:

    * ``- name: nav`` with the config beside it -- the pydantic ``SecondaryContainerConfig``
      spelling, identified by the literal ``name`` key;
    * ``- nav:`` with ``resources:`` as a *sibling*, which YAML parses as
      ``{"nav": None, "resources": {...}}`` -- what every campaign in this tree actually
      wrote. v1 found the name as "the first key that is not ``resources``", and that rule is
      reproduced rather than replaced: guessing by position or by null-ness diverges on an
      entry carrying more than one sibling; and
    * ``- nav: {resources: {...}}`` -- the nested form.

    ``resources`` was genuinely consumed by v1 (``sc.get('resources') or {}``), so carrying it
    across preserves what ran instead of inventing a limit the campaign never had.
    """
    for entry in raw_list or []:
        if not isinstance(entry, dict):
            continue
        if "name" in entry:
            name = entry["name"]
            config = {key: value for key, value in entry.items() if key != "name"}
        else:
            # v1's own rule: the first key that is not a config key names the container.
            name = next((key for key in entry if key not in _V1_SIBLING_KEYS), None)
            nested = entry.get(name) if name is not None else None
            if isinstance(nested, dict):
                config = dict(nested)
            else:
                config = {key: value for key, value in entry.items() if key != name}
        if name:
            yield name, config


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version 2 config. Does not mutate the input."""
    out = copy.deepcopy(raw)
    execution = out.get("execution")
    if not isinstance(execution, dict):
        execution = {}
    containers: dict = {}

    scenario = {key: execution.pop(key) for key in _SCENARIO_KEYS if key in execution}
    campaign_image = scenario.get("image")
    if scenario:
        containers["scenario"] = scenario

    for name, config in _secondary_entries(execution.pop("secondary_containers", None)):
        entry = containers.setdefault(name, {})
        entry.update(config)
        # v1 had ONE image per campaign: a secondary container declared only its
        # resources and ran ``execution.image``. v2 has no such inheritance -- an ad-hoc
        # container (any name outside the known roles) must state its own image, and a
        # known role would otherwise silently adopt v2's default instead of the image
        # that actually ran. Both are behaviour changes a migration must not make, so the
        # campaign image is written out explicitly. This is what ``_V1_MIGRATION``'s
        # "nav: {image: <img>, ...}" target meant.
        if campaign_image and not entry.get("image"):
            entry["image"] = campaign_image

    # ``build:`` was top-level in v1 and applied to the scenario container -- it is what
    # the campaign's own image was built from, and scenario is the container that ran it.
    build = out.pop("build", None)
    if isinstance(build, dict):
        target = containers.setdefault("scenario", {})
        for v1_key, v2_key in _BUILD_KEY_MAP.items():
            if v1_key in build:
                target[v2_key] = build[v1_key]

    if containers:
        execution["containers"] = containers
    if execution or "execution" in out:
        out["execution"] = execution
    out["version"] = 2
    return out
