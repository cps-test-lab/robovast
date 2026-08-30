# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Config formats for the ``sut:`` channel.

The properties these pin are the ones that decide whether the channel is a peer of the
other two or a YAML reader with a seam: a format owns its own path syntax, it performs its
own pre-check (so XML is checked rather than waved through), it can create and delete as
well as assign, and the extension route a third-party format takes is the same one the
built-ins take.
"""

import pytest

from robovast.common.sut_formats import (CannotAnswer, SutConfigFormat, UnknownFormat,
                                         load_formats, resolve_format)
from robovast.common.sut_formats.mapping_formats import JsonFormat, YamlFormat, parse_path
from robovast.common.sut_formats.xml_format import XmlFormat

BT = """<!-- a header the stack's authors wrote -->
<root BTCPP_format="4">
  <BehaviorTree ID="MainTree">
    <RecoveryNode number_of_retries="6" name="NavigateRecovery">
      <RecoveryNode number_of_retries="1" name="ComputePathToPose"/>
      <RoundRobin name="RecoveryActions">
        <!-- why these, in this order -->
        <Spin spin_dist="1.57"/>
        <BackUp backup_dist="0.30"/>
      </RoundRobin>
    </RecoveryNode>
  </BehaviorTree>
</root>
"""

PARAMS = """local_costmap:
  local_costmap:
    ros__parameters:
      plugins: ["obstacle_layer", "inflation_layer"]
      inflation_layer:
        inflation_radius: 0.55
      qos_overrides./tf:
        publisher:
          depth: 10
"""


@pytest.fixture(name="bt_file")
def _bt_file(tmp_path):
    path = tmp_path / "nav2_bt.xml"
    path.write_text(BT, encoding="utf-8")
    return str(path)


@pytest.fixture(name="params_file")
def _params_file(tmp_path):
    path = tmp_path / "nav2_params.yaml"
    path.write_text(PARAMS, encoding="utf-8")
    return str(path)


# --- the path grammar belongs to the format --------------------------------------------

def test_dotted_path_reaches_a_key_a_dot_would_split():
    """A ROS params file addresses QoS with keys like ``qos_overrides./tf``.

    A grammar without a quoted form could not reach a real stack's configuration, so this
    is a requirement rather than a nicety.
    """
    assert parse_path("a.b[0].c") == ["a", "b", 0, "c"]
    assert parse_path("a['qos_overrides./tf'].publisher") == ["a", "qos_overrides./tf",
                                                              "publisher"]


def test_xpath_predicate_disambiguates_like_named_nodes(bt_file):
    """Two nodes are named ``RecoveryNode``; only a predicate picks one.

    This is the case that decides the design: no flat dotted grammar can express it, so a
    centrally defined path syntax would have made XML second class.
    """
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    fmt.set(doc, "//RecoveryNode[@name='NavigateRecovery']/@number_of_retries", 2)
    assert doc.find(".//RecoveryNode[@name='NavigateRecovery']").get("number_of_retries") == "2"
    assert doc.find(".//RecoveryNode[@name='ComputePathToPose']").get("number_of_retries") == "1"


# --- the format performs its own pre-check ---------------------------------------------

def test_can_address_accepts_a_key_the_file_leaves_unset(params_file):
    """The parent must exist, not the leaf: a factor may set a defaulted key."""
    fmt = YamlFormat()
    doc = fmt.load(params_file)
    base = "local_costmap.local_costmap.ros__parameters"
    assert fmt.can_address(doc, f"{base}.inflation_layer.inflation_radius")
    assert fmt.can_address(doc, f"{base}.inflation_layer.cost_scaling_factor")
    assert not fmt.can_address(doc, "local_costmp.local_costmap.ros__parameters.plugins")


def test_xml_is_checked_rather_than_waved_through(bt_file):
    """``can_address`` answers for XPath too — the whole reason it is not a set of addresses."""
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    assert fmt.can_address(doc, "//RoundRobin[@name='RecoveryActions']")
    assert fmt.can_address(doc, "//Spin/@spin_dist")
    assert not fmt.can_address(doc, "//RoundRobin[@name='NoSuchGroup']")
    assert not fmt.can_address(doc, "//Spinn/@spin_dist")


def test_a_format_that_cannot_decide_says_so_rather_than_refusing(bt_file):
    """``CannotAnswer`` is not "no": one leaves the destination unchecked, the other fails
    the campaign, and a format that never implemented the check must not look like one that
    examined the path and rejected it."""
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    with pytest.raises(CannotAnswer):
        fmt.can_address(doc, "//[[[")


# --- set creates and replaces; remove deletes -------------------------------------------

@pytest.mark.parametrize("value", [0.3, "text", True, ["a", "b"], {"k": 1}])
def test_set_puts_any_shape_through_one_call(params_file, value):
    """Scalar, list and mapping are the same call, which is what lets a factor swap a whole
    costmap plugin list rather than only a number."""
    fmt = YamlFormat()
    doc = fmt.load(params_file)
    path = "local_costmap.local_costmap.ros__parameters.plugins"
    fmt.set(doc, path, value)
    fmt.dump(doc, params_file)
    assert fmt.load(params_file)["local_costmap"]["local_costmap"]["ros__parameters"]["plugins"] == value


def test_set_creates_a_key_that_was_not_there(params_file):
    fmt = YamlFormat()
    doc = fmt.load(params_file)
    base = "local_costmap.local_costmap.ros__parameters.inflation_layer"
    fmt.set(doc, f"{base}.cost_scaling_factor", 3.0)
    fmt.dump(doc, params_file)
    layer = fmt.load(params_file)["local_costmap"]["local_costmap"]["ros__parameters"]["inflation_layer"]
    assert layer["cost_scaling_factor"] == 3.0
    assert layer["inflation_radius"] == 0.55


def test_remove_makes_a_block_absent_not_empty(params_file):
    """The distinction the method exists for: a stack tells a block that is present and
    empty apart from one that is not there, so no assignment expresses this."""
    fmt = YamlFormat()
    doc = fmt.load(params_file)
    fmt.set(doc, "local_costmap.local_costmap.ros__parameters.voxel_layer", {"enabled": True})
    fmt.remove(doc, "local_costmap.local_costmap.ros__parameters.voxel_layer")
    fmt.dump(doc, params_file)
    params = fmt.load(params_file)["local_costmap"]["local_costmap"]["ros__parameters"]
    assert "voxel_layer" not in params


def test_remove_on_a_path_with_nothing_there_is_an_error(params_file):
    fmt = YamlFormat()
    doc = fmt.load(params_file)
    with pytest.raises(KeyError):
        fmt.remove(doc, "local_costmap.local_costmap.ros__parameters.no_such_key")


def test_xml_set_replaces_a_whole_subtree(bt_file):
    """How a behaviour tree's recovery repertoire is varied: the subtree carries its own
    children, so adding and removing them is one assignment."""
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    fmt.set(doc, "//RoundRobin[@name='RecoveryActions']",
            '<RoundRobin name="RecoveryActions"><Wait wait_duration="5.0"/></RoundRobin>')
    group = doc.find(".//RoundRobin[@name='RecoveryActions']")
    assert [child.tag for child in group] == ["Wait"]


def test_xml_remove_deletes_an_element_and_an_attribute(bt_file):
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    fmt.remove(doc, "//BackUp")
    fmt.remove(doc, "//Spin/@spin_dist")
    assert doc.find(".//BackUp") is None
    assert "spin_dist" not in doc.find(".//Spin").attrib


def test_xml_refuses_to_replace_the_root(bt_file):
    """Replacing the whole document is not a variation of it, and the failure is worth a
    named refusal rather than an IndexError from the parent lookup."""
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    with pytest.raises(ValueError, match="root"):
        fmt.set(doc, "/root", "<root/>")


def test_comments_survive_the_round_trip(bt_file):
    """A stack file's header often says where it came from or that it is generated. The
    campaign runs a rewritten copy, so dropping it would quietly degrade the file that
    actually executes."""
    fmt = XmlFormat()
    doc = fmt.load(bt_file)
    fmt.set(doc, "//Spin/@spin_dist", "1.0")
    fmt.dump(doc, bt_file)
    text = open(bt_file, encoding="utf-8").read()
    assert "a header the stack's authors wrote" in text
    assert "why these, in this order" in text


def test_json_round_trips(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"a": {"b": [1, 2]}}', encoding="utf-8")
    fmt = JsonFormat()
    doc = fmt.load(str(path))
    assert fmt.can_address(doc, "a.b[1]")
    fmt.set(doc, "a.b[1]", 9)
    fmt.dump(doc, str(path))
    assert fmt.load(str(path))["a"]["b"] == [1, 9]


# --- format selection, and the extension route ------------------------------------------

def test_extension_selects_a_format_and_an_explicit_name_wins():
    formats = load_formats()
    assert isinstance(resolve_format("files/nav2_params.yaml", formats=formats), YamlFormat)
    assert isinstance(resolve_format("files/nav2_bt.xml", formats=formats), XmlFormat)
    assert isinstance(resolve_format("files/p.yaml", "xml", formats=formats), XmlFormat)


def test_an_unclaimed_extension_is_refused_naming_what_is_registered():
    """Not defaulted to YAML. The bad case is not the file that fails to parse -- it is the
    one that parses, into a document whose addresses are all wrong."""
    with pytest.raises(UnknownFormat, match="json, xml, yaml"):
        resolve_format("files/dds_profile.custom")


def test_the_builtins_are_reachable_only_through_the_entry_point():
    """If a built-in took an internal shortcut, the extension route would be the one that
    rots -- silently, because nothing in this repository exercises it."""
    assert set(load_formats()) >= {"yaml", "json", "xml"}


def test_a_format_registered_by_a_third_party_is_treated_identically(monkeypatch):
    """The test that the extension path is real: a format the built-ins know nothing about
    is selected and checked by exactly the code that selects and checks ``yaml``."""

    class IniFormat(SutConfigFormat):
        EXTENSIONS = (".ini",)

        def load(self, path):
            return {"section": {"key": "value"}}

        def can_address(self, doc, path):
            return path in ("section.key",)

    formats = dict(load_formats(), ini=IniFormat())
    chosen = resolve_format("files/stack.ini", formats=formats)
    assert isinstance(chosen, IniFormat)
    assert chosen.can_address(chosen.load("files/stack.ini"), "section.key")
