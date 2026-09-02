# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Which of a search's postprocessing commands need a container, and which do not.

``search.postprocessing`` runs inside the search loop, before each batch is scored, and
until now it ran **entirely in the controller process**. That works for a pure-Python
plugin and cannot work for a rosbag converter: deserializing a bag needs the campaign's own
ROS 2 image, which is why campaign-level postprocessing dispatches an in-cluster conversion
Job instead of importing anything.

Launching the aux container from the controller resolves its image against the *default*
project rather than the deployment's and exits 1 -- while the campaign's own containers in
the same log resolve correctly. Every downstream plugin then reads files that do not exist,
and the extractor refuses the batch for a reason that points at the world.

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


# -- the shape the conversion Job expects ------------------------------------

def test_container_commands_unwrap_to_what_the_conversion_job_takes():
    """`run_conversion_job` takes the INNER dicts -- ``{plugins, bag_dir}`` -- not the
    ``{'rosbags_process': {...}}`` wrapper the local runner takes.

    The campaign-level path unwraps them (`rosbag_commands_for` ends in
    ``out.append(cmd["rosbags_process"] or {})``); a search dispatching the wrapped form
    instead created the Job with the right image and watched it fail, which reads as a
    broken converter rather than a mismatched argument. The two halves of the split feed
    two different callers and only one of them wants the wrapper.
    """
    from robovast.execution.controller import unwrap_conversion_commands

    container, _ = split_container_postprocessing([
        {'rosbags_tf_to_csv': {'frames': 'all'}},
        {'rosbags_to_csv': {'topics': ['/clearance']}},
    ])
    unwrapped = unwrap_conversion_commands(container)
    assert unwrapped, 'nothing to convert'
    for cmd in unwrapped:
        assert 'rosbags_process' not in cmd, 'still wrapped'
        assert 'plugins' in cmd, f'expected {{plugins, bag_dir}}, got {sorted(cmd)}'


def test_unwrapping_leaves_a_non_rosbag_container_command_alone():
    """A plugin that declares needs_execution_image is not a rosbags_process batch and has
    no wrapper to strip."""
    from robovast.execution.controller import unwrap_conversion_commands
    assert unwrap_conversion_commands(['some_plugin.py:Cls']) == ['some_plugin.py:Cls']


# -- the conversion is two steps, not one -----------------------------------

def test_the_conversion_helper_syncs_after_running_the_job(monkeypatch):
    """A conversion Job writes its output to the object store; something has to pull it
    into the campaign root before anything can read it.

    The campaign-level path does both -- run, then `sync_outputs`, unconditionally, so a
    failure's own log lands too. A search that ran the Job and skipped the sync got
    "rosbag conversion complete" in the log and an extractor that then found no CSVs,
    which reads as a conversion that lied.
    """
    from robovast.execution import controller as ctrl

    calls = []

    class _Backend:
        cluster_config = object()
        kube_context = None

    def _fake_job(*a, **kw):
        calls.append('job')
        return True, 'rosbag conversion complete'

    def _fake_sync(*a, **kw):
        calls.append('sync')

    monkeypatch.setattr(ctrl, '_conversion_job_runner',
                        lambda: (_fake_job, _fake_sync, lambda root: 'img', lambda m, _p: m))

    obj = ctrl.CampaignController.__new__(ctrl.CampaignController)
    obj.backend = _Backend()
    obj.campaign_id = 'c'
    obj.campaign_root = '/tmp/does-not-matter'
    obj.vast_dir = '/tmp'
    obj._convert_bags_in_cluster([{'rosbags_process': {'plugins': []}}])

    assert calls == ['job', 'sync'], f'expected run then sync, got {calls}'


def test_the_sync_happens_even_when_the_job_failed(monkeypatch):
    """Unconditionally, for the reason the campaign-level path gives: the conversion tees
    its own error into postprocessing.log and mirrors it out, so skipping the sync on
    failure loses the only account of what went wrong."""
    from robovast.execution import controller as ctrl

    calls = []
    monkeypatch.setattr(
        ctrl, '_conversion_job_runner',
        lambda: (lambda *a, **kw: (calls.append('job') or (False, 'boom')),
                 lambda *a, **kw: calls.append('sync'),
                 lambda root: 'img', lambda m, _p: m))

    class _Backend:
        cluster_config = object()
        kube_context = None

    obj = ctrl.CampaignController.__new__(ctrl.CampaignController)
    obj.backend = _Backend()
    obj.campaign_id = 'c'
    obj.campaign_root = '/tmp/does-not-matter'
    obj.vast_dir = '/tmp'
    obj._convert_bags_in_cluster([{'rosbags_process': {'plugins': []}}])

    assert calls == ['job', 'sync']


# -- each batch's conversion is its own Job ----------------------------------

def test_each_conversion_is_dispatched_under_its_own_name(monkeypatch):
    """The Job name was the campaign's alone, so batch 1's create returned 409, the wait
    read batch 0's already-completed Job, and the conversion reported success having done
    nothing -- 0 outputs synced, and an extractor that then blamed the world.

    Per repetitions-group, not per batch: `_run_postprocessing` runs once per group and
    adaptive repetitions produce several in one batch, so a per-batch name would collide
    again the moment repetitions stopped being uniform. The batch tag already carries both.
    """
    from robovast.execution import controller as ctrl

    seen = []

    def _fake_job(*a, **kw):
        seen.append(kw.get('discriminator'))
        return True, 'rosbag conversion complete'

    monkeypatch.setattr(ctrl, '_conversion_job_runner',
                        lambda: (_fake_job, lambda *a, **kw: None, lambda root: 'img', lambda m, _p: m))

    class _Backend:
        cluster_config = object()
        kube_context = None

    obj = ctrl.CampaignController.__new__(ctrl.CampaignController)
    obj.backend = _Backend()
    obj.campaign_id = 'c'
    obj.campaign_root = '/tmp/does-not-matter'
    obj.vast_dir = '/tmp'
    cmds = [{'rosbags_process': {'plugins': []}}]
    obj._convert_bags_in_cluster(cmds, 'batch-0')
    obj._convert_bags_in_cluster(cmds, 'batch-1')
    obj._convert_bags_in_cluster(cmds, 'batch-1/reps-5')

    assert len(set(seen)) == 3, f'conversions shared a Job identity: {seen}'


def test_the_batch_tag_reaches_the_conversion(monkeypatch):
    """`_run_postprocessing` is the only caller and it must pass the tag through, or the
    discriminator is threaded everywhere except where it is produced."""
    from robovast.execution import controller as ctrl

    passed = []
    obj = ctrl.CampaignController.__new__(ctrl.CampaignController)
    obj.postprocessing = [{'rosbags_to_csv': {'topics': ['/clearance']}}]
    obj.vast_dir = '/tmp'
    obj.campaign_root = '/tmp'
    monkeypatch.setattr(ctrl.CampaignController, '_convert_bags_in_cluster',
                        lambda self, cmds, tag: passed.append(tag))
    monkeypatch.setattr('robovast.common.config_plugins.ensure_plugins_importable',
                        lambda *a, **kw: None)
    obj._run_postprocessing('batch-2/reps-3')
    assert passed == ['batch-2/reps-3']


# -- a conversion that could not run is not a conversion that found nothing ---

def test_a_conversion_that_cannot_even_start_fails_the_campaign(monkeypatch):
    """These are different failures and were reported as the same one.

    A conversion that RAN and produced nothing is the extractor's business -- it refuses the
    batch and says what was missing. A conversion that could not START is a broken campaign:
    every batch will hit it, nothing will ever score, and the reason is not in the world.

    Logging it as a warning and carrying on lets the campaign die reporting that no run
    recorded a value and pointing at the postprocessing plugins, while the actual cause --
    no execution image recorded in execution.yaml -- sits in a warning line above it. The
    cost is the diagnosis, not the compute.
    """
    from robovast.execution import controller as ctrl

    def _boom():
        raise ValueError("no execution image recorded in execution.yaml")

    monkeypatch.setattr(ctrl, '_conversion_job_runner', _boom)

    class _Backend:
        cluster_config = object()
        kube_context = None

    obj = ctrl.CampaignController.__new__(ctrl.CampaignController)
    obj.backend = _Backend()
    obj.campaign_id = 'c'
    obj.campaign_root = '/tmp/does-not-matter'
    obj.vast_dir = '/tmp'

    with pytest.raises(RuntimeError) as excinfo:
        obj._convert_bags_in_cluster([{'rosbags_process': {'plugins': []}}], 'batch-0')
    message = str(excinfo.value)
    assert "execution image" in message, "the original cause must survive in the message"
    assert "could not" in message.lower() or "cannot" in message.lower()


def test_a_conversion_that_ran_and_failed_is_still_left_to_the_extractor(monkeypatch):
    """Unchanged: the Job started, so this batch's inputs may be partly there and the
    extractor is the thing that decides whether a batch is scorable."""
    from robovast.execution import controller as ctrl

    monkeypatch.setattr(
        ctrl, '_conversion_job_runner',
        lambda: (lambda *a, **kw: (False, 'conversion exited 1'),
                 lambda *a, **kw: None, lambda root: 'img', lambda m, _p: m))

    class _Backend:
        cluster_config = object()
        kube_context = None

    obj = ctrl.CampaignController.__new__(ctrl.CampaignController)
    obj.backend = _Backend()
    obj.campaign_id = 'c'
    obj.campaign_root = '/tmp/does-not-matter'
    obj.vast_dir = '/tmp'
    obj._convert_bags_in_cluster([{'rosbags_process': {'plugins': []}}], 'batch-0')  # no raise
