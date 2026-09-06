"""Config version 3 -> 4: a configuration's fixed values move under ``parameters:``, grouped
by the channel they are written into.

In v3 a configuration entry stated its three destination channels in two different grammars:
``parameters:`` was a list of single-key mappings folded into one dict, while ``sim:`` and
``sut:`` sat beside it as mappings. Which channel a value went to was therefore decided partly
by *which key* it was under and partly by *what shape* that key had, and a preset -- a reusable
body composed by several configurations -- had no way to carry all three without inventing a
third spelling. In v4 all three are mappings under ``parameters:``::

    # v3                                  # v4
    - name: hexagon                       - name: hexagon
      parameters:                           parameters:
      - map_file: maps/hexagon.yaml           scenario: {map_file: maps/hexagon.yaml}
      sim: {overrides: {...}}                 sim: {overrides: {...}}
      sut: {nav2.a.b: 1}                      sut: {nav2.a.b: 1}

``variations:`` is untouched: it says how a value is *produced*, not where it lands.

``search.parameters`` is the same block by another name -- compose expands it into a
configuration entry per generation -- so it is migrated identically.

**Comments on individual ``parameters:`` list items do not survive**, because the list they
were attached to becomes a mapping. Comments anywhere else, including inside the ``sim:`` and
``sut:`` blocks, do: those mappings are *moved* rather than rebuilt.

**Pure ``dict`` -> ``dict``.** Nothing here may import :mod:`robovast.common.config`;
``test_migration_purity`` enforces it. Deep-copy the input and mutate the copy -- never
``dict(raw)``, which drops a ruamel ``CommentedMap``'s comments.
"""

import copy

#: The channels a fixed value can be written into, in the order they are emitted.
_CHANNELS = ("scenario", "sim", "sut")


def _scenario_mapping(parameters):
    """The v3 ``parameters:`` list folded into one mapping, later entries winning.

    The fold is not a reinterpretation: v3 already collapsed this list with ``dict.update``
    before anything read it, so a repeated key already meant "the last one". Writing it out as
    a mapping states what the file always meant.
    """
    if isinstance(parameters, dict):
        return parameters          # already a mapping; nothing to fold
    scenario = {}
    for item in parameters or []:
        if isinstance(item, dict):
            scenario.update(item)
    return scenario


def _migrate_block(entry):
    """Restructure one configuration entry (or the ``search.parameters`` block) in place."""
    if not isinstance(entry, dict):
        return

    parameters = entry.get("parameters")
    if isinstance(parameters, dict) and any(k in parameters for k in _CHANNELS):
        return                     # already v4-shaped

    scenario = _scenario_mapping(parameters)
    # Popped rather than read: the values are the author's own mappings, so moving them keeps
    # whatever comments ruamel has attached to them.
    moved = {name: entry.pop(name) for name in ("sim", "sut") if entry.get(name)}
    entry.pop("sim", None)
    entry.pop("sut", None)

    block = {}
    if scenario:
        block["scenario"] = scenario
    for name in ("sim", "sut"):
        if name in moved:
            block[name] = moved[name]

    if block:
        entry["parameters"] = block
    else:
        entry.pop("parameters", None)


def migrate(raw: dict) -> dict:
    """Return *raw* restructured as a version 4 config. Does not mutate the input."""
    out = copy.deepcopy(raw)

    for entry in out.get("configuration") or []:
        _migrate_block(entry)

    search = out.get("search")
    if isinstance(search, dict) and "parameters" in search:
        # `search.parameters` is the block without its wrapper, so wrap it, migrate, unwrap.
        holder = {"parameters": search["parameters"]}
        _migrate_block(holder)
        if "parameters" in holder:
            search["parameters"] = holder["parameters"]
        else:
            search.pop("parameters", None)

    out["version"] = 4
    return out
