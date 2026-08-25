# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``node`` table, and the rule that a machine is recorded without being named.

Campaign data is published. A cluster's node names are internal infrastructure, so what
travels with the results is a hash of the name -- enough to group a campaign's runs by the
machine that ran them, and enough for someone holding the real name to find its runs, while
carrying no name and no naming scheme for anyone who does not.

These pin both halves: that the label identifies a machine consistently, and that the name
it was made from reaches nothing that is stored.
"""

import json

from robovast.common.execution import node_label
from robovast.common.store import CampaignStore

_NAME = "some-internal-node-07"
_FACTS = {
    "capacity": {"cpu": "96", "memory": "131448588Ki"},
    "allocatable": {"cpu": "96"},
    "node_info": {"kernel_version": "5.14.0-1058-oem", "os_image": "Ubuntu 20.04.4 LTS"},
    "labels": {"node-role.kubernetes.io/control-plane": "true"},
}


def _sysinfo(name=_NAME, cpu_name="Intel Xeon"):
    """What the pod writes: the label, never the name it was made from."""
    return {"node_label": node_label(name), "cpu_name": cpu_name}


def _store(tmp_path, resolver=None):
    store = CampaignStore(tmp_path / "campaign.db")
    store.set_node_facts_resolver(resolver)
    return store


# -- the label ----------------------------------------------------------------------------

def test_the_label_is_the_hash_of_the_name_so_it_needs_no_stored_mapping():
    """The property the whole design rests on: someone who knows the machine's name can
    recompute its label and find its runs, without anything having recorded the pair."""
    import hashlib
    expected = "node-" + hashlib.sha256(_NAME.encode()).hexdigest()[:12]
    assert node_label(_NAME) == expected
    assert node_label(_NAME) == node_label(_NAME), "must be stable, or the join breaks"
    assert _NAME not in node_label(_NAME)


def test_no_node_is_no_label_rather_than_a_label_for_nothing():
    """A local run has no node. Empty must not hash to a label that would then read as a
    machine every local run shared."""
    assert node_label("") is None
    assert node_label(None) is None


def test_different_machines_get_different_labels():
    assert node_label("node-a") != node_label("node-b")


# -- the table ----------------------------------------------------------------------------

def test_one_row_per_machine_however_many_jobs_ran_on_it(tmp_path):
    """The grain is the machine, not the job: a campaign's thousands of runs land on a
    handful of machines, and the record should say that rather than repeat itself."""
    with _store(tmp_path, lambda label: _FACTS) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        for i in range(3):
            store.upsert_job(cid, f"_jobs/job-{i}", _sysinfo(), store._node_facts)
        rows = list(store._conn.execute("SELECT node_label, cpu_name FROM node"))
    assert len(rows) == 1
    assert rows[0]["node_label"] == node_label(_NAME)
    assert rows[0]["cpu_name"] == "Intel Xeon"


def test_only_machines_the_campaign_used_are_recorded(tmp_path):
    """Not an inventory of the cluster. A campaign that touched one of many machines
    records one -- which is also why the record cannot go stale describing the rest."""
    with _store(tmp_path, lambda label: _FACTS) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", _sysinfo("machine-a"), store._node_facts)
        store.upsert_job(cid, "_jobs/job-1", _sysinfo("machine-b"), store._node_facts)
        labels = {r[0] for r in store._conn.execute("SELECT node_label FROM node")}
    assert labels == {node_label("machine-a"), node_label("machine-b")}


def test_a_job_points_at_the_machine_that_ran_it(tmp_path):
    with _store(tmp_path) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", _sysinfo(), store._node_facts)
        row = store._conn.execute("SELECT node_label FROM job").fetchone()
    assert row["node_label"] == node_label(_NAME)


def test_the_api_facts_are_stored_as_given(tmp_path):
    with _store(tmp_path, lambda label: _FACTS) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", _sysinfo(), store._node_facts)
        row = store._conn.execute(
            "SELECT capacity_json, node_info_json, labels_json FROM node").fetchone()
    assert json.loads(row["capacity_json"])["cpu"] == "96"
    assert json.loads(row["node_info_json"])["os_image"] == "Ubuntu 20.04.4 LTS"
    assert "control-plane" in row["labels_json"]


def test_a_machine_nobody_could_ask_about_is_still_recorded(tmp_path):
    """The local lane, a re-index, an unreadable node. WHICH machine a run used is worth
    keeping even when its hardware is not available -- NULL facts, not a missing row, and
    never invented ones."""
    with _store(tmp_path, resolver=None) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", _sysinfo(), store._node_facts)
        row = store._conn.execute(
            "SELECT node_label, cpu_name, capacity_json FROM node").fetchone()
    assert row["node_label"] == node_label(_NAME)
    assert row["cpu_name"] == "Intel Xeon"     # the run itself reported this
    assert row["capacity_json"] is None        # nobody could ask the cluster


def test_a_later_job_cannot_blank_out_facts_an_earlier_one_recorded(tmp_path):
    """A resolver that answers once and fails later must not erase what it already said."""
    answers = [_FACTS, None]
    with _store(tmp_path, lambda label: answers.pop(0)) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", _sysinfo(), store._node_facts)
        store.upsert_job(cid, "_jobs/job-1", _sysinfo(), store._node_facts)
        row = store._conn.execute("SELECT capacity_json FROM node").fetchone()
    assert row["capacity_json"] is not None


def test_a_run_with_no_node_records_no_machine(tmp_path):
    with _store(tmp_path) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", {"cpu_name": "Intel Xeon"}, store._node_facts)
        assert store._conn.execute("SELECT COUNT(*) FROM node").fetchone()[0] == 0
        assert store._conn.execute("SELECT node_label FROM job").fetchone()[0] is None


# -- the property the change exists for ---------------------------------------------------

def test_the_stored_campaign_contains_the_name_nowhere(tmp_path):
    """The one that matters, asserted over the database's own bytes rather than over the
    columns we remembered to check: nothing this change writes may contain the machine's
    name."""
    with _store(tmp_path, lambda label: _FACTS) as store:
        cid = store.create_campaign("c", "batch", ".", "{}")
        store.upsert_job(cid, "_jobs/job-0", _sysinfo(), store._node_facts)
    raw = (tmp_path / "campaign.db").read_bytes()
    assert _NAME.encode() not in raw
    assert node_label(_NAME).encode() in raw, "the label itself must be there"


def test_the_file_the_pod_writes_carries_the_label_and_not_the_name(tmp_path):
    """``sysinfo.yaml`` ships inside the campaign archive, so this is the sink that cannot
    be cleaned up afterwards -- a raw name written here travels with every published
    dataset, and the raw share upload happens before postprocessing could scrub it. The
    hostname therefore has to stop in the process that learns it."""
    import subprocess
    import sys
    from importlib.resources import files

    script = str(files("robovast.execution.data").joinpath("collect_sysinfo.py"))
    out = tmp_path / "sysinfo.yaml"
    subprocess.run([sys.executable, script, "--output", str(out), "--node-name", _NAME],
                   check=True)
    text = out.read_text(encoding="utf-8")
    assert node_label(_NAME) in text
    assert _NAME not in text
    assert "node_name" not in text


def test_a_pod_with_no_node_writes_no_label(tmp_path):
    """The local lane passes an empty NODE_NAME; it must not record an empty machine."""
    import subprocess
    import sys
    from importlib.resources import files

    script = str(files("robovast.execution.data").joinpath("collect_sysinfo.py"))
    out = tmp_path / "sysinfo.yaml"
    subprocess.run([sys.executable, script, "--output", str(out), "--node-name", ""],
                   check=True)
    assert "node_label" not in out.read_text(encoding="utf-8")
