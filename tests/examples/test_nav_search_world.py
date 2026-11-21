# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The nav_search example's world and its campaigns agree on what a campaign may vary.

Every factor the campaigns vary is a key of the world, reached through the ``sim:`` channel
as a nested mapping mirroring the world document with components addressed by name. An
address that names nothing is not refused by the simulator until the image is pulled and a
pod is scheduled, once per cell -- so the agreement is checked here, against the files a
reader copies.

Two facts about the world itself are checked beside it, because each is invisible from a
campaign that runs: where a walker goes is its nested ``navigator``'s (a route key on the
walker's own block is refused by the simulator), and the world states no ``sim.seed`` (a
stated seed gives every repetition of a cell the same sensor noise and the same walker
pauses, and ``aggregate: worst`` across them then reads nothing).
"""

import pathlib

import pytest

yaml = pytest.importorskip("yaml")

EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / "configs" / "examples" / "nav_search"
WALKER_ROUTE_KEYS = {"speed", "waypoints", "goals", "loop", "dwell", "arrival_radius",
                     "avoidance", "goal_endpoint"}


def _world():
    path = EXAMPLE / "world" / "empty_room.yaml"
    if not path.is_file():
        pytest.skip("nav_search example not present")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _plugin(entry):
    """A component entry's plugin reference and its config block."""
    ref = next(k for k in entry if k not in ("name", "components"))
    return ref, entry[ref] or {}


def _component(entries, token):
    """The entry *token* addresses: by its name, else by its plugin reference when unnamed."""
    for entry in entries:
        if entry.get("name") == token:
            return entry
    for entry in entries:
        if "name" not in entry and _plugin(entry)[0] == token:
            return entry
    return None


def resolves(world, address) -> bool:
    """Whether a ``sim:`` address names a key of this world."""
    tokens = address.split(".")
    if tokens[0] == "sim":
        return len(tokens) == 2 and tokens[1] in (world.get("sim") or {})
    if tokens[0] != "components" or len(tokens) < 3:
        return False
    entries = world.get("components") or []
    entry = None
    for depth, token in enumerate(tokens[1:], start=1):
        last = depth == len(tokens) - 1
        found = _component(entries, token)
        if found is not None and not last:
            entry = found
            entries = entry.get("components") or []
            continue
        if entry is None:
            return False
        return last and token in _plugin(entry)[1]
    return False


def _sim_addresses(doc):
    """Every ``sim:`` address a campaign's variations name, in search and batch form."""
    blocks = []
    if "search" in doc:
        blocks = (doc["search"] or {}).get("variations") or []
    for cfg in doc.get("configuration") or []:
        blocks += cfg.get("variations") or []
    for block in blocks:
        for params in block.values():
            sim = (params or {}).get("sim")
            if isinstance(sim, str):
                yield sim
            elif isinstance(sim, list):
                yield from sim
            elif isinstance(sim, dict):
                yield from sim.values()


@pytest.mark.parametrize("vast", sorted(p.name for p in EXAMPLE.glob("*.vast")))
def test_every_sim_address_a_campaign_names_is_a_key_of_the_world(vast):
    world = _world()
    doc = yaml.safe_load((EXAMPLE / vast).read_text(encoding="utf-8"))
    addresses = list(_sim_addresses(doc))
    assert addresses, f"{vast} varies nothing through sim:, which no nav_search campaign does"
    unresolved = [a for a in addresses if not resolves(world, a)]
    assert not unresolved, f"{vast} addresses keys the world does not have: {unresolved}"


def test_the_resolver_tells_a_present_key_from_an_absent_one():
    world = _world()
    assert resolves(world, "components.barrier.instances")
    assert resolves(world, "components.robot.rplidar.lidar.range_stddev")
    assert not resolves(world, "components.pedestrian.dwell")
    assert not resolves(world, "components.nobody.dwell")
    assert not resolves(world, "sim.nothing")


def test_the_walker_keeps_its_route_on_a_nested_navigator():
    world = _world()
    pedestrian = _component(world["components"], "pedestrian")
    ref, config = _plugin(pedestrian)
    assert ref == "walker"
    assert not WALKER_ROUTE_KEYS & set(config), "route keys on the walker block are refused"
    navigators = [e for e in pedestrian.get("components") or [] if _plugin(e)[0] == "navigator"]
    assert len(navigators) == 1
    assert _plugin(navigators[0])[1].get("output") == "walker"
    assert navigators[0].get("name"), "the navigator is named so a campaign can address it"


def test_the_world_leaves_the_noise_seed_to_each_run():
    assert "seed" not in (_world().get("sim") or {})
