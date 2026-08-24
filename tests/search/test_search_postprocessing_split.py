# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Which of a search's postprocessing commands need a container, and which do not.

``search.postprocessing`` runs inside the search loop, before each batch is scored, and
until now it ran **entirely in the controller process**. That works for a pure-Python
plugin and cannot work for a rosbag converter: deserializing a bag needs the campaign's own
ROS 2 image, which is why campaign-level postprocessing dispatches an in-cluster conversion
Job instead of importing anything.

Measured consequence, on a real campaign: the converter tried to launch its aux container
from the controller, resolved the image against the *default* project rather than the
deployment's, and exited 1 -- while the campaign's own three containers in the same log
resolved correctly. Every downstream plugin then read files that did not exist, and the
extractor refused the batch for a reason that pointed at the world.

So a search's commands have to be split the same way the campaign-level path splits them.
This is that split, and it is the part worth testing: the dispatch itself needs a cluster,
but *which command goes where* is pure and is where the bug was.
"""

import pytest

from robovast.execution.controller import split_container_postprocessing


def _names(commands):
    return [c if isinstance(c, str) else next(iter(c)) for c in commands]


# -- the split --------------------------------------------------------------

def test_a_pure_python_plugin_stays_in_process():
    """A local-file metrics plugin needs no image and must not pay for a Job."""
    container, local = split_container_postprocessing(['search/nav_metrics.py:NavMetrics'])
    assert container == []
    assert local == ['search/nav_metrics.py:NavMetrics']


def test_a_rosbag_converter_goes_to_the_container():
    container, local = split_container_postprocessing([
        {'rosbags_to_csv': {'topics': ['/collision']}}])
    assert container and local == []


def test_a_mixed_list_is_split_and_order_within_each_half_is_kept():
    """The metrics plugin reads what the converter wrote, so the container half must run
    first -- and the caller runs them in that order."""
    container, local = split_container_postprocessing([
        {'rosbags_tf_to_csv': {'frames': 'all'}},
        {'rosbags_to_csv': {'topics': ['/clearance']}},
        {'nav2_bt_tree': {'bt_xml': 'files/nav2_bt.xml'}},
        'search/nav_metrics.py:NavMetrics',
    ])
    assert container, 'the two rosbag commands belong in the container half'
    assert _names(local) == ['nav2_bt_tree', 'search/nav_metrics.py:NavMetrics']


def test_the_rosbag_commands_are_batched_one_pass_per_bag():
    """Same batching the campaign-level path uses: one rosbags_process per bag_dir, so a
    bag is read once rather than once per handler.

    Two passes, not one: the run's own bag carries the three handlers asked for, and the
    infrastructure bag (logs/rosout_bag, recorded in wall time for the container's whole
    life) is auto-injected exactly as it is for a campaign. A search gets the same
    treatment as the block it is modelled on rather than a quietly different one.
    """
    container, _ = split_container_postprocessing([
        {'rosbags_tf_to_csv': {'frames': 'all'}},
        {'rosbags_to_csv': {'topics': ['/collision', '/clearance']}},
        {'rosbags_nav2bt_to_csv': {}},
    ])
    by_bag = {(c['rosbags_process'] or {}).get('bag_dir'):
              [p.get('type') for p in (c['rosbags_process'] or {}).get('plugins', [])]
              for c in container}
    assert set(by_bag) == {'rosbag2', 'logs/rosout_bag'}
    assert by_bag['rosbag2'] == ['tf_to_csv', 'to_csv', 'nav2_bt_to_csv']


def test_an_empty_list_splits_to_two_empty_halves():
    assert split_container_postprocessing([]) == ([], [])


def test_none_is_tolerated():
    """`search.postprocessing` is optional, and absent is not an error."""
    assert split_container_postprocessing(None) == ([], [])


def test_rosbags_process_named_directly_is_also_a_container_command():
    """A campaign may name the batched plugin itself; it needs the image just the same."""
    container, local = split_container_postprocessing([
        {'rosbags_process': {'plugins': [{'type': 'to_csv', 'topics': ['/clearance']}]}}])
    assert container and local == []


@pytest.mark.parametrize('command', ['command', 'compress', 'resource_usage', 'run_log'])
def test_plugins_that_read_no_bag_stay_local(command):
    """Only bag deserialization needs the campaign's image; the rest would pay a Job's
    latency for nothing, once per batch, for the length of a search."""
    container, local = split_container_postprocessing([command])
    assert container == [] and local == [command]


# -- the capability is the plugin's to declare -------------------------------

def test_a_plugin_declaring_the_capability_is_dispatched_without_touching_this_code(tmp_path):
    """The genericity that matters: a NEW plugin needing the campaign's image says so on
    itself, and the split honours it. A name list here would serve only what existed when
    it was written -- which is how the rosbag converters ended up running in the wrong
    process in the first place.
    """
    plugin = tmp_path / 'needs_image.py'
    plugin.write_text(
        'from robovast.results_processing.postprocessing_plugins import '
        'BasePostprocessingPlugin\n'
        'class NeedsImage(BasePostprocessingPlugin):\n'
        '    needs_execution_image = True\n'
        '    def __call__(self, results_dir, config_dir, **kw):\n'
        '        return True, "ok"\n')
    container, local = split_container_postprocessing(
        [f'{plugin.name}:NeedsImage', 'search/nav_metrics.py:NavMetrics'],
        config_dir=str(tmp_path))
    assert _names(container) == [f'{plugin.name}:NeedsImage']
    assert _names(local) == ['search/nav_metrics.py:NavMetrics']


def test_a_plugin_that_does_not_declare_it_stays_local(tmp_path):
    """The default is local, so an ordinary plugin pays no Job latency once per batch."""
    plugin = tmp_path / 'plain.py'
    plugin.write_text(
        'from robovast.results_processing.postprocessing_plugins import '
        'BasePostprocessingPlugin\n'
        'class Plain(BasePostprocessingPlugin):\n'
        '    def __call__(self, results_dir, config_dir, **kw):\n'
        '        return True, "ok"\n')
    container, local = split_container_postprocessing(
        [f'{plugin.name}:Plain'], config_dir=str(tmp_path))
    assert container == [] and _names(local) == [f'{plugin.name}:Plain']


def test_an_unresolvable_command_is_left_local_rather_than_guessed(tmp_path):
    """It will fail loudly where it runs, which says more than a guess made here."""
    container, local = split_container_postprocessing(
        ['no_such_plugin'], config_dir=str(tmp_path))
    assert container == [] and local == ['no_such_plugin']
