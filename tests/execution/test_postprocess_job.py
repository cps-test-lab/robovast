# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for analysis postprocessing surfacing."""

import logging
import types

import pytest

import robovast.execution.cluster_execution.postprocess_job as pj
from robovast.common.index_db import DSN_ENV
from robovast.common.quantity import to_bytes, to_cores
from robovast.execution.cluster_execution import pod_access
from robovast.results_processing.postprocessing import POSTPROCESS_CONVERT_DEFAULTS

from .image_steps_helper import CMDS, steps, stub_image_steps


@pytest.fixture(autouse=True)
def _the_index_is_configured(monkeypatch):
    """Every manifest in this file needs an index DSN in the submitting process.

    The Job's host container IS the index ingest, so ``build_manifest`` refuses to build
    one without a DSN rather than staging a campaign's worth of data onto a node before
    failing on config the submitter could already see. A test that is about job names or
    pull secrets would otherwise fail on that refusal instead of on what it is named for.

    An out-of-cluster DSN deliberately: it is passed through as it stands, so no Secret
    reference has to exist for these tests.
    """
    monkeypatch.setenv(DSN_ENV, "host=index.example.com dbname=robovast user=robovast")


_TOKEN = "campaign:camp.0123abcd"


@pytest.fixture(autouse=True)
def _steps_without_a_campaign_tree(monkeypatch):
    stub_image_steps(monkeypatch)


def _inputs(monkeypatch, rosbag_cmds=None):
    """The five facts a postprocess needs, without a campaign tree to read them from: an
    unsplit campaign."""
    monkeypatch.setattr(pj, "_read_submit_inputs",
                        lambda root, skip=None, skip_rosout=False:
                        (CMDS if rosbag_cmds is None else rosbag_cmds,
                         "img", (), None, None))


def _postprocess(monkeypatch, root, verdict, **kwargs):
    _inputs(monkeypatch)
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: verdict)
    return pj.postprocess_campaign(object(), "camp", str(root), "ns", token=_TOKEN,
                                   **kwargs)


def test_postprocess_echoes_conversion_log_to_console_on_failure(
        monkeypatch, tmp_path, caplog):
    """The pod's postprocessing.log is on the campaign's directory by the time the verdict
    is recorded, and it is also echoed to the service console."""
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir()
    (exec_dir / "postprocessing.log").write_text(
        "rosbags_process.py: error: unrecognized arguments: --output-root\n")

    with caplog.at_level(logging.WARNING, logger=pj.logger.name):
        ok, msg = _postprocess(monkeypatch, tmp_path, (False, "boom"))

    assert ok is False and "boom" in msg
    assert any("unrecognized arguments: --output-root" in r.message
               for r in caplog.records)


def test_the_submit_reads_its_inputs_from_the_campaigns_directory(tmp_path):
    """The four facts the manifest needs come from the campaign's own files on the
    service: the .vast, the execution record and the intervention ledger. No other
    source exists, so none is consulted."""
    (tmp_path / "_config").mkdir()
    (tmp_path / "_config" / "x.vast").write_text(
        "version: 3\nmetadata: {name: x}\nresults_processing:\n"
        "  postprocessing:\n  - rosbags_tf_to_csv\n")
    (tmp_path / "_execution").mkdir()
    (tmp_path / "_execution" / "execution.yaml").write_text("image: img:1\n")
    (tmp_path / "_execution" / "interventions.json").write_text(
        '[{"kind": "invalid", "job_dir": "_jobs/batch-1/job-27"}]')

    rosbag_cmds, image, tolerate, _sized, _split = pj._read_submit_inputs(str(tmp_path))

    assert rosbag_cmds and image == "img:1"
    assert "_jobs/batch-1/job-27" in tolerate


# ---------------------------------------------------------------------------
# a pod that cannot start
# ---------------------------------------------------------------------------

_CMDS = steps("c1", plugins=[{"type": "tf_to_csv", "frames": "all"}])


def test_the_conversion_pod_can_pull_its_own_images():
    """The Job runs the sidecar and controller images and the campaign's execution image,
    any of which may sit in a registry the kubelet has no credential for. Without this the
    pod stays ImagePullBackOff while the Job stays `active`, so the waiter reported a timeout
    and named neither image nor registry."""
    m = pj.build_manifest("c1", "img:1", _CMDS, "ns", pull_secret_name="robovast-registry")
    assert m["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "robovast-registry"}]


def test_no_credential_configured_leaves_the_pod_spec_alone():
    """A public-registry deployment needs none, and naming an absent Secret would itself keep
    the pod from starting."""
    m = pj.build_manifest("c1", "img:1", _CMDS, "ns")
    assert "imagePullSecrets" not in m["spec"]["template"]["spec"]


def test_a_failed_job_is_reported_without_a_cluster_command():
    """This message lands on ``postprocessing_error``, which the web UI renders to
    someone who has a log panel and no kubeconfig. An appended
    ``kubectl logs job/<name> -n <ns>`` is unrunnable for that reader, aimed at whichever
    cluster their context happens to name, and points at a Job that
    ``ttlSecondsAfterFinished`` reaps 300 s after it fails. The campaign log is where the
    conversion output actually is, and every surface already shows it.
    """
    msg = pj.job_failed_message("robovast-postproc-c1")

    assert "robovast-postproc-c1" in msg
    # No cluster command, in any form: no tool name, no shell.
    assert "kubectl" not in msg and "`" not in msg and "$" not in msg


def test_the_log_pointer_is_only_promised_when_the_log_is_there(tmp_path):
    """The message may not send a reader to a POSTPROCESSING section that does not exist.

    A Job whose pod could not be read leaves no log, so the section is never written;
    promising one anyway costs the reader a hunt through an empty panel and hides the real
    fault.
    """
    raw = pj.job_failed_message("robovast-postproc-c1")
    log = tmp_path / "postprocessing.log"

    missing = pj.with_log_pointer(raw, log)
    assert "POSTPROCESSING section" in missing and "no POSTPROCESSING section" in missing
    assert pj.POINTER_SLOT not in missing

    log.write_text("conversion error")
    present = pj.with_log_pointer(raw, log)
    assert "see the POSTPROCESSING section" in present
    assert pj.POINTER_SLOT not in present


def test_a_message_that_promises_no_log_is_left_alone(tmp_path):
    """A blocked pod and a timeout carry their own complete explanation and never ran a
    conversion, so neither may acquire a pointer to a log that was never in question."""
    for message in ("postprocessing job j cannot start: ImagePullBackOff",
                    "postprocessing job j timed out after 30s"):
        assert pj.with_log_pointer(message, tmp_path / "nope.log") == message


def test_a_blocked_pod_is_reported_by_its_reason(monkeypatch):
    """What the Job's own status cannot say: `active` is indistinguishable between "converting"
    and "will never start"."""
    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.blocked_job_reasons",
        lambda *_a, **_kw: {"job-x": "ImagePullBackOff: pull access denied"})
    assert pj._blocked_reason(object(), "ns", "job-x") == (
        "ImagePullBackOff: pull access denied")


def test_an_unreadable_pod_list_does_not_condemn_a_running_conversion(monkeypatch):
    """Advisory only: this check exists to sharpen a failure, so failing to make it must not
    turn a conversion that is working into a reported error."""
    def _boom(*_a, **_kw):
        raise RuntimeError("pods forbidden")

    monkeypatch.setattr(
        "robovast.execution.cluster_execution.cluster_execution.blocked_job_reasons", _boom)
    assert pj._blocked_reason(object(), "ns", "job-x") == ""


# -- one campaign, many conversions ------------------------------------------

def test_two_conversions_of_one_campaign_get_different_job_names():
    """A search converts once per batch, and the Job name was the campaign's alone.

    With one name per campaign the second `create_namespaced_job` returns 409, the code
    falls through to wait on the existing Job -- batch 0's, already complete -- reads
    `succeeded` and reports "rosbag conversion complete" having converted nothing. The
    extractor then refuses the batch for a missing clearance, naming the world.

    The 409 fallthrough is right for what it was written for: a one-shot campaign
    postprocess, retried, should wait on the in-flight Job rather than duplicate it. It is
    wrong as soon as one campaign converts more than once, so the Job's identity has to say
    WHICH conversion it is.
    """
    names = {pj.build_manifest("camp-2026-08-25-1234", "img", steps(), "ns",
                               role=pj.JobRole.search_batch(d, []))["metadata"]["name"]
             for d in ("batch-0", "batch-1", "batch-2")}
    assert len(names) == 3, f"batches collided on one Job name: {names}"


def test_the_same_conversion_keeps_a_stable_name():
    """So a genuine retry of one conversion still waits on the in-flight Job instead of
    launching a second copy of it -- the behaviour the 409 fallthrough exists for."""
    def name(disc):
        return pj.build_manifest("camp-x", "img", steps(), "ns",
                                 role=pj.JobRole.search_batch(disc, []))["metadata"]["name"]
    assert name("batch-3") == name("batch-3")


def test_no_discriminator_leaves_the_campaign_level_name_unchanged():
    """The campaign-level path converts once and its Job name is part of what an operator
    looks for; nothing about it should move because a search needed more names."""
    plain = pj.build_manifest("camp-x", "img", steps(), "ns")["metadata"]["name"]
    assert plain == "robovast-postproc-camp-x"


def test_long_campaign_ids_stay_within_the_label_limit_and_stay_distinct():
    """Kubernetes copies the name into the pod template's `job-name` label, capped at 63.
    Truncation must not be what makes two batches collide again -- the hash is taken over
    the discriminated identity for exactly that reason."""
    long_id = "nav-search-adaptive-reps-2026-08-25-13573569-with-a-long-suffix"
    names = set()
    for disc in ("batch-0", "batch-1", "batch-10", "batch-1-reps-3", "batch-1-reps-5"):
        n = pj.build_manifest(long_id, "img", steps(), "ns",
                              role=pj.JobRole.search_batch(disc, []))["metadata"]["name"]
        assert len(n) <= 63, f"{n} is {len(n)} chars"
        names.add(n)
    assert len(names) == 5, f"truncation collapsed distinct conversions: {names}"


def test_the_scripts_configmap_is_discriminated_too(monkeypatch):
    """Each conversion's scripts ConfigMap belongs to its Job. Sharing one name across
    conversions means a finishing batch's Job takes the ConfigMap another is mounting with
    it, which surfaces as a pod stuck in ContainerCreating rather than as a name clash."""
    a = pj._scripts_cm_name("camp-x", discriminator="batch-0")
    b = pj._scripts_cm_name("camp-x", discriminator="batch-1")
    assert a != b
    assert pj._scripts_cm_name("camp-x") == "robovast-postproc-scripts-camp-x"


# -- the stage container: the sidecar image, curl | tar from the data plane ---


def _stage(manifest):
    return manifest["spec"]["template"]["spec"]["initContainers"][0]


def _env_names(container):
    return {e["name"] for e in container.get("env") or []}


def test_the_stage_is_the_sidecar_image_fetching_the_campaign_archive(monkeypatch):
    """No Python stage: the campaign arrives as one tar stream from the service's data
    plane, and `curl | tar` is the whole of what lands it. The command is the one
    `pod_access` renders, so the stage and every other pod agree on how a stream is
    fetched -- the address from the env, the token as a bearer, the archive extracted
    under the mount where its top segment is the campaign id.
    """
    monkeypatch.setattr(pj, "resolve_sidecar_image", lambda: "example.com/sidecar:1")

    stage = _stage(pj.build_manifest("c1", "img:1", _CMDS, "ns"))

    assert stage["name"] == pj.STAGE_CONTAINER
    assert stage["image"] == "example.com/sidecar:1"
    script = stage["command"][-1]
    assert pod_access.fetch_command("/campaigns/c1/archive", pj.CAMPAIGN_MOUNT,
                                    "stage=true&uncompressed=true&skip_bags=false") in script
    assert "python" not in script


def test_the_stage_asks_for_what_the_pod_reads():
    """The selection is made where the bytes are: `stage=true` always (no probe, no log
    this pod writes), the bags only where a conversion will open one, and one batch's jobs
    for a per-batch Job -- with the tag quoted, since `/` is part of a repetitions group's."""
    with_bags = _stage(pj.build_manifest("c1", "img:1", _CMDS, "ns"))["command"][-1]
    without = _stage(pj.build_manifest("c1", None, [], "ns"))["command"][-1]
    batch = _stage(pj.build_manifest(
        "c1", "img:1", _CMDS, "ns",
        role=pj.JobRole.search_batch("batch-3/reps-5", [])))["command"][-1]
    part = _stage(pj.build_manifest("c1", "img:1", _CMDS, "ns",
                                    role=pj.JobRole.for_part("part-1", [])))["command"][-1]
    reduce_ = _stage(pj.build_manifest("c1", "img:1", _CMDS, "ns",
                                       role=pj.JobRole.reduce(stage_bags=False)))["command"][-1]

    assert "skip_bags=false" in with_bags and "batch_jobs" not in with_bags
    assert "skip_bags=true" in without
    assert "batch_jobs=batch-3%2Freps-5" in batch
    # A part has a discriminator and host commands too, but it is narrowed by its runs.
    assert "part=part-1" in part and "batch_jobs" not in part
    assert "batch_jobs" not in reduce_ and "skip_bags=true" in reduce_


def test_the_stage_and_its_storage_estimate_select_the_same_tree():
    """The manifest's stage query and the ephemeral-storage estimate both read these two
    rules from the role, so the pod is sized for exactly what it fetches."""
    assert pj.JobRole.search_batch("batch-3", []).batch_jobs == "batch-3"
    for role in (pj.JobRole.campaign(), pj.JobRole.for_part("part-1", []),
                 pj.JobRole.reduce(stage_bags=True)):
        assert role.batch_jobs == ""
    assert pj.JobRole.campaign().skips_bags([]) and not pj.JobRole.campaign().skips_bags(["s"])
    assert not pj.JobRole.reduce(stage_bags=True).skips_bags([])
    assert pj.JobRole.reduce(stage_bags=False).skips_bags(["s"])


def test_the_stage_hands_the_tree_to_the_pods_group():
    """`tar` run as root restores each member's owner and mode from the archive, so the
    tree it leaves is the service user's with the service's modes, and the conversion
    container -- another user -- fails on its first output with EACCES. The stage gives the
    tree to the shared group and makes it group-writable, links excluded: a dangling `job`
    link is not an error, and never the thing whose mode matters.
    """
    script = _stage(pj.build_manifest("c1", "img:1", _CMDS, "ns"))["command"][-1]

    assert f"chgrp {pj.CAMPAIGN_TREE_GID}" in script
    assert "chmod g+rwX" in script
    assert "! -type l" in script
    # After the fetch, and only if it succeeded: a half-extracted tree is not handed on.
    assert script.index("tar -x ") < script.index("chgrp")
    assert " && find " in script


def test_the_pod_reaches_the_data_plane_with_a_scoped_token_and_nothing_else():
    """The two robovast containers carry the address, the campaign id and the campaign's
    token -- the token from the campaign's Secret, never inline in a spec `kubectl get`
    prints back. The stage carries exactly that; the conversion, a stranger's image,
    carries none of it.
    """
    spec = pj.build_manifest("c1", "img:1", _CMDS, "ns")["spec"]["template"]["spec"]
    containers = {c["name"]: c for c in spec["initContainers"] + spec["containers"]}

    for name in (pj.STAGE_CONTAINER, pj.HOST_CONTAINER):
        env = {e["name"]: e for e in containers[name]["env"]}
        assert env[pod_access.DATA_URL_ENV]["value"].startswith("http://")
        assert env[pod_access.CAMPAIGN_ID_ENV]["value"] == "c1"
        token = env[pod_access.TOKEN_ENV]
        assert "value" not in token
        assert token["valueFrom"]["secretKeyRef"] == {
            "name": pod_access.campaign_secret_name("c1"), "key": pod_access.TOKEN_KEY}

    assert _env_names(containers[pj.STAGE_CONTAINER]) == {
        pod_access.DATA_URL_ENV, pod_access.CAMPAIGN_ID_ENV, pod_access.TOKEN_ENV}

    convert_env = _env_names(containers[pj.CONVERT_CONTAINER])
    assert pod_access.TOKEN_ENV not in convert_env
    assert pod_access.DATA_URL_ENV not in convert_env
    assert convert_env == {"PYTHONUNBUFFERED"}


def test_the_conversion_writes_the_campaign_tree_and_uploads_nothing():
    """Outputs go to the paths they are read from -- so no mapping step stands between an
    output and its canonical path, and the host container delivers them by the same
    campaign-relative name.

    The conversion reaches the data plane not at all: it reads and writes the one shared
    mount, and the host container that follows it is what delivers. That is what lets this
    container be an arbitrary user image: the conversion runs over the campaign tree in the
    mount and writes beside each bag, with no separate output volume.
    """
    script = pj._conversion_script(steps(), campaign_id="c1")
    step = next(line for line in script.splitlines() if "rosbags_process.py" in line)

    assert step.rstrip().endswith(f"{pj.CAMPAIGN_MOUNT}/c1")
    assert "--output-root" not in step
    assert script.rstrip().endswith("exit $rc")
    for absent in ("curl", pod_access.TOKEN_ENV, pod_access.DATA_URL_ENV):
        assert absent not in script, absent


def test_a_failed_conversion_still_writes_a_postprocessing_section(tmp_path):
    """The phase file IS the section: every surface assembles the campaign log from the
    files that exist, so a Job whose pod left no log would leave the phases stopping after
    RUN with the failure reported only in a status field elsewhere.
    """
    log = tmp_path / '_execution' / 'postprocessing.log'
    pj._write_failure_log('camp', str(log), pj.job_failed_message('job-x'))
    text = log.read_text()

    assert 'job-x' in text
    # The slot is decided by whether this file exists, so inside it there is nothing for it
    # to say -- and it must never reach a reader.
    assert pj.POINTER_SLOT not in text
    # No cause is read into the missing log: the per-container exit status is what
    # survives every failure, and the text says to read that instead.
    assert "exit status" in text and "pod's status" in text
    # And it names the candidates, because any container can fail before the log is read.
    assert 'stages the campaign' in text and 'converts its rosbags' in text


def test_the_message_points_at_the_section_once_it_has_been_written(tmp_path):
    """The two changes have to agree: writing the section is what makes the pointer true."""
    log = tmp_path / 'postprocessing.log'
    raw = pj.job_failed_message('job-x')

    assert 'no POSTPROCESSING section' in pj.with_log_pointer(raw, log)
    log.write_text('an account')
    assert 'see the POSTPROCESSING section' in pj.with_log_pointer(raw, log)


class _Term:
    def __init__(self, exit_code=None, reason=None):
        self.exit_code, self.reason = exit_code, reason


class _CS:
    def __init__(self, name, exit_code=None, reason=None):
        self.name = name
        self.state = type('S', (), {'terminated': _Term(exit_code, reason)})()


def _pod(reason=None, message=None, init=(), main=()):
    status = type('St', (), {'reason': reason, 'message': message,
                             'init_container_statuses': list(init),
                             'container_statuses': list(main)})()
    return type('P', (), {'status': status})()


def _core(pods):
    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return type('L', (), {'items': pods})()
    return _Core()


def test_an_evicted_pod_explains_itself_without_the_container_helping():
    """The failure that most needs an account is the one that can file none: a pod
    SIGKILLed by the kubelet under node disk pressure runs no cleanup. The kubelet recorded
    the reason all along -- reading it needs nothing from the dead container.
    """
    core = _core([_pod(reason='Evicted',
                       message='Pod ephemeral local storage usage exceeds the total limit')])

    reason = pj.pod_failure_reason(core, 'ns', 'job-x')

    assert reason.startswith('Evicted:') and 'ephemeral' in reason
    assert 'Evicted' in pj.job_failed_message('job-x', pod_reason=reason)


def test_the_staging_container_is_read_before_the_conversion():
    """Init containers run first, so when staging is what failed the conversion container's
    status says nothing at all -- and reading only the regular containers saw exactly that.
    """
    core = _core([_pod(init=[_CS(pj.STAGE_CONTAINER, exit_code=2)],
                       main=[_CS(pj.HOST_CONTAINER)])])

    reason = pj.pod_failure_reason(core, 'ns', 'job-x')

    assert reason.startswith(f'container {pj.STAGE_CONTAINER} ')
    assert 'exit 2' in reason


def test_an_unreadable_pod_list_never_becomes_the_failure():
    """This runs while reporting a failure, so it must not raise one of its own."""
    class _Broken:
        def list_namespaced_pod(self, namespace, label_selector):
            raise RuntimeError('no api')

    assert pj.pod_failure_reason(_Broken(), 'ns', 'job-x') == ''
    # And the message is still complete without it.
    assert 'job-x' in pj.job_failed_message('job-x', pod_reason='')


def test_a_stage_exit_code_is_read_in_the_pipelines_own_vocabulary():
    """The stage is `curl | tar`, so its code is curl's or tar's and a bare `exited 1
    (Error)` names neither. The fetch exits with curl's code when the transfer failed and
    tar's only when a whole stream would not extract, so a cut stream is curl's to name and
    tar's codes mean the node could not take the archive. Any other curl code is named by
    number -- and in every case the report is in the log the pod's stdout is published to.
    """
    def reason(code):
        return pj.pod_failure_reason(
            _core([_pod(init=[_CS(pj.STAGE_CONTAINER, exit_code=code, reason='Error')])]),
            'ns', 'job-x')

    assert set(pj.STAGE_EXIT_REASONS) == {7, 18, 22, 56}
    for code in pj.STAGE_EXIT_REASONS:
        assert pj.STAGE_EXIT_REASONS[code] in reason(code)
        assert f'exit {code}' in reason(code)
    for code in pj.STAGE_TAR_CODES:
        assert pj.STAGE_TAR_FAILED in reason(code) and 'disk' in reason(code)
    timeout = reason(28)
    assert pj.STAGE_FETCH_FAILED in timeout and 'exit 28' in timeout and 'disk' not in timeout


def test_another_containers_exit_code_is_reported_as_the_number():
    """The vocabulary is the stage's; every other container reports its code and the
    kubelet's reason as they are."""
    core = _core([_pod(main=[_CS(pj.HOST_CONTAINER, exit_code=7, reason='Error')])])

    assert pj.pod_failure_reason(core, 'ns', 'job-x') == (
        f'container {pj.HOST_CONTAINER} exited 7 (Error)')


class _FakeBatch:
    """Enough of BatchV1Api to drive adopt_or_replace."""

    def __init__(self, active=None, missing=False):
        self.active, self.missing = active, missing
        self.calls = []

    def read_namespaced_job(self, name, namespace):
        self.calls.append('read')
        if self.missing:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=404)
        return type('J', (), {'status': type('S', (), {'active': self.active})()})()

    def delete_namespaced_job(self, name, namespace, propagation_policy=None):
        self.calls.append('delete')
        self.missing = True

    def create_namespaced_job(self, namespace, body):
        self.calls.append('create')


class _FakeCore:
    """Enough of CoreV1Api for the blocked-pod probe behind _stuck_job.

    *pods* are handed to the real pod-signal code, so a test says what Kubernetes says and
    the classification stays the shared one rather than a second opinion written here.
    """

    def __init__(self, pods=(), events=()):
        self.pods, self.events = list(pods), list(events)

    def list_namespaced_pod(self, namespace, label_selector=None):
        return type('L', (), {'items': self.pods})()

    def list_namespaced_event(self, namespace, field_selector=None):
        reason = (field_selector or '').split('=')[-1]
        return type('L', (), {'items': [e for e in self.events
                                        if e.reason == reason]})()

    def list_node(self):
        return type('L', (), {'items': []})()


def _unpullable_pod(job='job-x'):
    """A pod whose image will never arrive -- the blocked shape the pod's own status carries."""
    waiting = type('W', (), {'reason': 'ImagePullBackOff', 'message': 'no such image'})()
    cs = type('CS', (), {'state': type('S', (), {'waiting': waiting})()})()
    return type('P', (), {
        'metadata': type('M', (), {'name': f'{job}-pod', 'labels': {'job-name': job}})(),
        'spec': type('Sp', (), {'node_name': 'node-a'})(),
        'status': type('St', (), {'phase': 'Pending', 'conditions': [],
                                  'init_container_statuses': [],
                                  'container_statuses': [cs]})()})()


def test_a_running_job_is_adopted_not_replaced():
    """Two postprocesses of one campaign must not race each other's pods."""
    batch = _FakeBatch(active=1)

    # Reported as adoption rather than as bare success: the caller has to know it is a
    # waiter on someone else's Job, because that decides what it may write and delete.
    assert pj.adopt_or_replace(batch, _FakeCore(), 'ns', 'job-x', {}) == pj._JOB_ADOPTED
    assert 'delete' not in batch.calls


def test_a_finished_job_is_replaced_rather_than_waited_on():
    """The Job name comes from the campaign and is reused every retrigger, so waiting on
    whatever answers to it would report the PREVIOUS attempt's outcome as this one's --
    against a pod whose containers ran an earlier version of the script.
    """
    batch = _FakeBatch(active=None)

    assert pj.adopt_or_replace(batch, _FakeCore(), 'ns', 'job-x', {}) == pj._JOB_RECREATED
    assert batch.calls.index('delete') < batch.calls.index('create')


def test_an_active_job_whose_pod_cannot_start_is_replaced_not_adopted():
    """``status.active`` counts a pod that will never run, so adopting on that field alone
    makes a campaign unrecoverable: the attempt waits on an outcome that is not coming, and
    every retrigger after it adopts the same Job and waits again. Re-running postprocessing
    is the documented recovery, so it has to be able to reach a new pod.
    """
    batch = _FakeBatch(active=1)
    core = _FakeCore(pods=[_unpullable_pod()])

    assert pj.adopt_or_replace(batch, core, 'ns', 'job-x', {}) == pj._JOB_RECREATED
    assert batch.calls.index('delete') < batch.calls.index('create')
    assert not pj.live_job(batch, core, 'ns', 'job-x')


def test_a_job_queued_behind_a_busy_cluster_is_still_adopted():
    """The counterpart, and the reason the check asks for the reasons that will NOT clear:
    a pod waiting for a node or a throttled pull is the work in flight adoption exists for,
    and replacing its Job would throw away a conversion that was about to run.
    """
    waiting = type('W', (), {'reason': 'ImagePullBackOff',
                             'message': 'toomanyrequests: rate limit exceeded'})()
    cs = type('CS', (), {'state': type('S', (), {'waiting': waiting})()})()
    pod = type('P', (), {
        'metadata': type('M', (), {'name': 'job-x-pod', 'labels': {'job-name': 'job-x'}})(),
        'spec': type('Sp', (), {'node_name': 'node-a'})(),
        'status': type('St', (), {'phase': 'Pending', 'conditions': [],
                                  'init_container_statuses': [],
                                  'container_statuses': [cs]})()})()
    batch = _FakeBatch(active=1)

    assert pj.adopt_or_replace(batch, _FakeCore(pods=[pod]), 'ns', 'job-x',
                                {}) == pj._JOB_ADOPTED
    assert 'delete' not in batch.calls


def test_staging_memory_is_a_bound_and_not_headroom_for_the_campaign():
    """Staging must not need a limit that grows with the campaign it stages.

    `curl | tar` streams: each member is written as it arrives, so the footprint is set by
    that construction and a *small* limit is the correct one. Sizing it for the largest
    campaign anyone might postprocess would only hide a regression in that streaming.

    Pinned against the conversion's default, because that is the number it must stay under.
    """
    def _mem_mib(spec):
        value = spec["limits"]["memory"]
        assert value.endswith(("Gi", "Mi")), value
        return int(value[:-2]) * (1024 if value.endswith("Gi") else 1)

    convert = pj.step_resources(**{k: POSTPROCESS_CONVERT_DEFAULTS[k]
                                   for k in ("cpu", "memory")})
    assert _mem_mib(pj.stage_resources()) < _mem_mib(convert)
    # The disk request, by contrast, is the campaign's and stays: it is what reserves the
    # shared emptyDir the campaign lands in.
    assert pj.stage_resources()["requests"]["ephemeral-storage"]


# -- the running Job's log, published to the campaign's directory ------------


def _pod_with(*containers, name="p1", when=None):
    inits, mains = containers[:-1], containers[-1:]
    return types.SimpleNamespace(
        metadata=types.SimpleNamespace(name=name, creation_timestamp=when),
        spec=types.SimpleNamespace(
            init_containers=[types.SimpleNamespace(name=c) for c in inits],
            containers=[types.SimpleNamespace(name=c) for c in mains]))


def _log_of(root):
    return (root / "_execution" / "postprocessing.log").read_text()


def test_the_running_job_publishes_its_log_as_the_phase(tmp_path):
    """Nothing the pod writes leaves it until it exits: the log is on a shared volume and
    the last container delivers it at the end. So a conversion measured in minutes would
    show an empty POSTPROCESSING section for all of them, and the only way to watch one
    a pod name nobody off-cluster has.

    Read from the pod's stdout rather than its volume, because the volume is the pod's own
    and nothing outside can see it -- every container in declaration order, so staging and
    conversion read as the one section they are -- and written straight to the campaign's
    phase file on the service, which is what every surface reads the section from.
    """
    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_pod_with("stage", "convert", "host")])

        def read_namespaced_pod_log(self, name, namespace, container):
            from kubernetes import client
            if container == "host":
                raise client.exceptions.ApiException(status=400)  # not started yet
            return f"{container} said something"

    assert pj.publish_live_log(_Core(), str(tmp_path), "ns", "job-x") is True

    text = _log_of(tmp_path)
    # Declaration order, and the container that has not started is simply absent -- which
    # is also how "this stage has not run" should look.
    assert text.index("stage said") < text.index("convert said")
    assert "host" not in text


def test_a_pod_that_cannot_be_read_does_not_fail_the_postprocess(tmp_path):
    """This is a read for someone watching. It must not fail the work it is watching."""
    class _Broken:
        def list_namespaced_pod(self, namespace, label_selector):
            raise RuntimeError("no api")

    assert pj.publish_live_log(_Broken(), str(tmp_path), "ns", "job-x") is False
    assert not (tmp_path / "_execution").exists()


def test_the_live_log_comes_from_the_newest_pod(tmp_path):
    """A Job can have more than one pod -- a backoffLimit retry makes another, and replacing
    a finished Job of the same name makes another still -- and the listing does not promise
    an order. Publishing from an arbitrary one would make the section alternate between two
    attempts each time this is called, which reads worse than either of them.
    """
    import datetime

    old = datetime.datetime(2026, 9, 2, 10, tzinfo=datetime.timezone.utc)
    new = datetime.datetime(2026, 9, 2, 11, tzinfo=datetime.timezone.utc)

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            # Oldest first, which is the order that makes this wrong.
            return types.SimpleNamespace(items=[_pod_with("host", name="older", when=old),
                                                _pod_with("host", name="newer", when=new)])

        def read_namespaced_pod_log(self, name, namespace, container):
            return f"log of {name}"

    assert pj.publish_live_log(_Core(), str(tmp_path), "ns", "job-x") is True
    assert _log_of(tmp_path).strip() == "log of newer"


def test_each_publish_replaces_rather_than_accumulates(tmp_path):
    """Called every thirty seconds for the length of a conversion, so anything but a
    replacement would grow the section by a copy of itself each time."""
    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            return types.SimpleNamespace(items=[_pod_with("host")])

        def read_namespaced_pod_log(self, name, namespace, container):
            return "the whole log so far"

    for _ in range(3):
        pj.publish_live_log(_Core(), str(tmp_path), "ns", "job-x")

    assert _log_of(tmp_path).count("the whole log so far") == 1


def _execution_dir(tmp_path, **files):
    """A campaign root holding just the ``_execution/`` records named in *files*."""
    import yaml
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in files.items():
        (exec_dir / f"{name}.yaml").write_text(yaml.safe_dump(payload))
    return tmp_path


def test_execution_image_falls_back_to_launch_yaml(tmp_path):
    """A campaign whose execution.yaml was never written is still convertible.

    launch.yaml is written before the first job exists, so it survives a campaign that
    ran to completion without its execution record. Refusing on execution.yaml's absence
    would strand exactly those campaigns -- every run on disk and every bag intact, with
    no way to convert them -- which is worse than converting in the image the launch
    itself resolved.
    """
    root = _execution_dir(tmp_path, launch={
        "campaign_name": "c", "images": {"sut": "example.com/sut:abc123"}})

    assert pj.campaign_execution_image(root) == "example.com/sut:abc123"


def test_execution_image_prefers_the_execution_record_over_the_launch_one(tmp_path):
    """`declared` wins over `launched` where both exist.

    They agree on a healthy campaign; where they differ, what the campaign asked for is
    the better answer than what the launcher resolved before any job ran.
    """
    root = _execution_dir(
        tmp_path,
        launch={"campaign_name": "c", "images": {"sut": "example.com/sut:from-launch"}},
        execution={"images": {"sut": "example.com/sut:from-execution"}})

    assert pj.campaign_execution_image(root) == "example.com/sut:from-execution"


def test_execution_image_still_refuses_when_nothing_is_recorded(tmp_path):
    """Absence of a FILE is recoverable; absence of any image is not -- converting in the
    wrong image deserializes the bags' custom ROS2 types wrongly or not at all."""
    root = _execution_dir(tmp_path)

    with pytest.raises(ValueError, match="no execution image recorded"):
        pj.campaign_execution_image(root)


def _pod_charge(manifest, resource):
    """What Kubernetes charges the pod for *resource*.

    ``max(max(initContainer requests), sum(container requests))`` -- the rule the scheduler
    applies, spelled out here because the whole reason only one step is settable is that it
    is a maximum rather than a sum.
    """
    spec = manifest["spec"]["template"]["spec"]

    def _value(container):
        raw = container["resources"]["requests"][resource]
        return to_cores(raw) if resource == "cpu" else to_bytes(raw)

    inits = [_value(c) for c in spec.get("initContainers", [])]
    mains = [_value(c) for c in spec.get("containers", [])]
    return max([*inits, sum(mains)])


def _manifest(**kwargs):
    return pj.build_manifest("camp", "img:1", steps("camp", plugins=[{"type": "to_csv"}]), "ns",
                             **kwargs)


def _containers(manifest):
    spec = manifest["spec"]["template"]["spec"]
    return {c["name"]: c for c in
            list(spec.get("initContainers", [])) + list(spec.get("containers", []))}


def test_every_step_is_held_to_what_it_reserved():
    """No step of this pod may use more cpu or memory than it reserved.

    The pod runs on the nodes that run trials. A step allowed past its reservation takes
    cores from a run whose own request was honest, and that run's timing then depends on
    which campaign happened to be postprocessing beside it -- a hidden variable in the
    measurement that no artifact of the affected run records. Nothing in this pod is under
    test, so the throughput given up costs nothing that matters.
    """
    for name, container in _containers(_manifest()).items():
        resources = container["resources"]
        for resource in ("cpu", "memory"):
            assert resources["requests"][resource] == resources["limits"][resource], name


def test_disk_is_the_one_resource_that_still_bursts():
    """Ephemeral storage keeps a limit above its request, unlike cpu and memory.

    Disk is reclaimed as the conversion writes its outputs and the staged bags are dropped,
    so the peak is transient and far above the mean. Equalising the two would either price
    every postprocessing pod at a disk figure almost none of them reach, or fail a large
    campaign at a ceiling near the reservation.
    """
    for name, container in _containers(_manifest()).items():
        resources = container["resources"]
        assert (to_bytes(resources["limits"]["ephemeral-storage"])
                > to_bytes(resources["requests"]["ephemeral-storage"])), name


def test_the_fixed_steps_are_a_floor_under_the_pod_and_not_only_a_default():
    """A conversion sized BELOW the built-in steps does not shrink the pod any further.

    Pinned because the obvious "fix" -- scaling the fixed steps down with the declared
    figure -- trades a slow step for an OOM kill of the one that publishes the results. The
    index ingest's footprint is not the campaign's to declare, and a ``.vast`` asking for
    512Mi has said nothing about whether the ingest still fits in it.

    The declaration is not ignored: the conversion container is still held to it, and the
    fan-out still follows it. It is the pod's *reservation* that stops falling.
    """
    small = _manifest(convert_resources={"cpu": 1, "memory": "512Mi"})
    convert = _containers(small)["convert"]["resources"]["requests"]
    assert (convert["cpu"], convert["memory"]) == ("1", "512Mi")

    floor_cpu = to_cores(pj.POSTPROCESS_HOST_FLOOR["cpu"])
    assert _pod_charge(small, "cpu") == floor_cpu > 1


def test_raising_the_block_does_raise_what_the_pod_reserves():
    """The direction that matters, and the reason the knob exists: a conversion that needs
    more gets a pod that reserved more, rather than one that is merely allowed more."""
    big = _manifest(convert_resources={"cpu": 6, "memory": "12Gi"})
    assert _pod_charge(big, "cpu") == 6
    assert _pod_charge(big, "memory") == to_bytes("12Gi")


def test_a_search_batch_is_sized_by_the_same_block(monkeypatch):
    """The per-batch Job a search submits takes the same figure.

    This is the Job a campaign gets most of: one per batch, for the length of the search,
    so a conversion left at the default here would be the one place a campaign's declared
    size did not apply -- on the path that runs it most.
    """
    batch = _manifest(role=pj.JobRole.search_batch("", []),
                      convert_resources={"cpu": 6, "memory": "12Gi"})
    containers = _containers(batch)
    assert containers["convert"]["resources"]["requests"]["cpu"] == "6"
    assert _pod_charge(batch, "cpu") == 6
    # And the same floor applies in this shape, for the same reason.
    small = _manifest(role=pj.JobRole.search_batch("", []),
                      convert_resources={"cpu": 1, "memory": "512Mi"})
    assert _pod_charge(small, "cpu") == to_cores(pj.POSTPROCESS_HOST_FLOOR["cpu"])


def test_a_campaign_that_says_nothing_gets_the_shared_default():
    convert = _containers(_manifest())["convert"]["resources"]
    assert convert["requests"]["cpu"] == str(POSTPROCESS_CONVERT_DEFAULTS["cpu"])
    assert convert["requests"]["memory"] == str(POSTPROCESS_CONVERT_DEFAULTS["memory"])


def test_staging_is_never_raised_by_what_a_campaign_asks_for():
    """Staging keeps its figure whatever the ``.vast`` says.

    Its footprint is set by how it is written -- a stream, member by member -- so the
    small memory bound is a guard, not a reservation: a regression in that streaming shows
    up as this step failing, and a limit that grew with the campaign's request is exactly
    the one that would absorb it in silence. It also runs nothing of the campaign's, so
    there is nothing here a ``.vast`` would know better.
    """
    containers = _containers(_manifest(convert_resources={"cpu": 6, "memory": "12Gi"}))
    assert containers["convert"]["resources"]["requests"]["cpu"] == "6"
    assert containers["stage"]["resources"] == pj.stage_resources()


def test_one_step_cannot_edit_another_steps_resources():
    """The stamped blocks are copies, not the module's own dicts.

    Shared objects would let anything mutating a container's resources in place -- a
    GPU request, a cluster-specific override, a test -- rewrite the default for every
    later pod in the process. A service builds many of these.
    """
    first = _manifest()
    _containers(first)["stage"]["resources"]["limits"]["cpu"] = "99"
    assert _containers(_manifest())["stage"]["resources"]["limits"]["cpu"] != "99"


def test_the_host_step_is_raised_because_the_campaigns_own_code_runs_there():
    """The figure has to reach the host step, not only the conversion.

    ``run_host_postprocessing`` runs the ordinary pipeline with *only* the image steps
    skipped, so everything else a campaign declared happens there: its own metric plugins,
    metadata, publication, the health checks. Those are precisely the steps whose appetite
    RoboVAST cannot know. A knob that sized only the conversion would leave them pinned at a
    figure the campaign could not change, and the symptom would be an OOM kill of a step
    whose declared allocation said it had room.
    """
    host = _containers(_manifest(convert_resources={"cpu": 8, "memory": "16Gi"}))["host"]
    requests = host["resources"]["requests"]
    assert (requests["cpu"], requests["memory"]) == ("8", "16Gi")
    assert host["resources"]["limits"]["memory"] == "16Gi"


def test_the_host_step_keeps_its_floor_when_a_campaign_asks_for_less():
    """Raise-only. A campaign knows when its analysis needs more; it cannot know that the
    index ingest still fits in less, and being wrong that way kills the step that publishes
    the results rather than slowing it."""
    host = _containers(_manifest(convert_resources={"cpu": 1, "memory": "512Mi"}))["host"]
    requests = host["resources"]["requests"]
    assert (requests["cpu"], requests["memory"]) == (str(pj.POSTPROCESS_HOST_FLOOR["cpu"]),
                                                     pj.POSTPROCESS_HOST_FLOOR["memory"])


def test_raised_to_compares_quantities_rather_than_strings():
    """``"512Mi"`` is not less than ``"4Gi"`` by string order, and ``"8"`` is not more than
    ``"10"``. Both comparisons decide whether a step is raised or floored."""
    assert pj.raised_to({"cpu": 2, "memory": "4Gi"}, {"cpu": 10, "memory": "512Mi"}) == {
        "cpu": 10, "memory": "4Gi"}
    assert pj.raised_to({"cpu": 2, "memory": "4Gi"}, {"cpu": "500m", "memory": "8Gi"}) == {
        "cpu": 2, "memory": "8Gi"}
    # An unparseable quantity keeps the floor rather than raising to nonsense.
    assert pj.raised_to({"cpu": 2, "memory": "4Gi"}, {"cpu": "lots"})["cpu"] == 2


# -- what a submit writes around the Job --------------------------------------


class _SubmitBatch:
    """Enough of BatchV1Api to drive a whole `run_conversion_job` submit and wait."""

    def __init__(self, existing_active=None, calls=None):
        self.existing_active = existing_active
        self.calls = [] if calls is None else calls

    def read_namespaced_job(self, name, namespace):
        self.calls.append('read-job')
        if self.existing_active is None:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=404)
        return type('J', (), {
            'metadata': type('M', (), {'uid': 'job-uid'})(),
            'status': type('S', (), {'active': self.existing_active})()})()

    def create_namespaced_job(self, namespace, body):
        self.calls.append('create-job')
        self.existing_active = 1

    def read_namespaced_job_status(self, name, namespace):
        # Succeeded on the first poll: these tests are about what the submit writes around
        # the Job, so the wait must end without a second of sleeping.
        return type('J', (), {'status': type('S', (), {
            'active': None, 'succeeded': 1, 'failed': None})()})()


class _SubmitCore:
    """Enough of CoreV1Api to record every write to the scripts ConfigMap and the Secret."""

    def __init__(self, conflict=False, calls=None, secret_exists=False):
        self.conflict = conflict
        self.secret_exists = secret_exists
        self.calls = [] if calls is None else calls
        self.secrets = []

    def create_namespaced_config_map(self, namespace, body):
        self.calls.append('create-cm')
        if self.conflict:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=409)

    def replace_namespaced_config_map(self, name, namespace, body):
        self.calls.append('replace-cm')

    def patch_namespaced_config_map(self, name, namespace, body):
        self.calls.append('own-cm')
        self.owner = ((body.get("metadata") or {}).get("ownerReferences") or [None])[0]

    def delete_namespaced_config_map(self, name, namespace):
        self.calls.append('delete-cm')

    def create_namespaced_secret(self, namespace, body):
        self.calls.append('create-secret')
        self.secrets.append(body)
        if self.secret_exists:
            from kubernetes.client.rest import ApiException
            raise ApiException(status=409)


def _submit(monkeypatch, core, batch, tmp_path):
    monkeypatch.setattr("robovast.execution.cluster_execution.cluster_execution."
                        "resolve_pull_secret", lambda cfg, core_, ns: "")
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: "test")
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)
    monkeypatch.setattr("kubernetes.client.BatchV1Api", lambda: batch)
    monkeypatch.setattr(pj, "publish_live_log", lambda *a, **k: None)
    return pj.run_conversion_job(object(), "camp", str(tmp_path), "ns", "img",
                                 CMDS, token=_TOKEN)


def test_adopting_a_live_job_does_not_touch_the_scripts_it_mounts(monkeypatch, tmp_path):
    """A second attempt meeting a live Job must write NOTHING the Job mounts.

    The scripts ConfigMap is mounted at /scripts in the conversion container, which is
    executing out of that mount. The kubelet syncs new content into every mount of a
    ConfigMap, so replacing it swaps the script out from under the running interpreter and
    the container exits 1 -- and deleting it on the way out removes the mount entirely.
    Either write destroys a healthy in-flight conversion, and the attempt that did it is
    the one that reports the failure as the campaign's. So the live-Job check has to come
    before the ConfigMap, and an adopted Job's resources belong to the attempt that created
    them. The Secret the live Job reads its token from is left alone for the same reason.
    """
    calls = []
    core = _SubmitCore(calls=calls)
    batch = _SubmitBatch(existing_active=1, calls=calls)

    ok, message = _submit(monkeypatch, core, batch, tmp_path)

    assert ok is True and "complete" in message
    # Neither created, replaced, nor deleted -- and no second Job launched at the live one.
    assert calls == ['read-job']


def test_a_fresh_submit_writes_the_token_secret_then_the_scripts_then_the_job(
        monkeypatch, tmp_path):
    """Nothing is running under this name, so this attempt writes what the pod mounts --
    and then gives the scripts away.

    The Secret first: the pod's `secretKeyRef` names it, and a pod whose Secret does not
    exist waits in CreateContainerConfigError with the Job `active`. The ConfigMap outlives
    the wait on purpose. A waiter stops waiting for reasons that say nothing about the Job
    (its own deadline, a stop, a service restart), and deleting the scripts on the way out
    takes the mount away from a Job that is still running: the pod that follows cannot
    start at all, so the Job stays active forever, the campaign stays in postprocessing,
    and its log ends mid-step. The ownerReference is what makes that unexpressible -- the
    Job's own ttlSecondsAfterFinished is what collects it.
    """
    calls = []
    core = _SubmitCore(calls=calls)
    batch = _SubmitBatch(existing_active=None, calls=calls)

    ok, _message = _submit(monkeypatch, core, batch, tmp_path)

    assert ok is True
    assert calls == ['read-job', 'create-secret', 'create-cm', 'create-job', 'read-job',
                     'own-cm']
    assert core.owner["kind"] == "Job" and core.owner["uid"] == "job-uid"
    secret = core.secrets[0]
    assert secret["metadata"]["name"] == pod_access.campaign_secret_name("camp")
    assert secret["stringData"] == {pod_access.TOKEN_KEY: _TOKEN}


def test_a_secret_the_campaigns_runs_already_wrote_is_kept(monkeypatch, tmp_path):
    """The token is deterministic, so the Secret that exists holds the value this would
    write; a 409 is the normal case for a campaign that ran on this cluster."""
    calls = []
    core = _SubmitCore(calls=calls, secret_exists=True)
    batch = _SubmitBatch(existing_active=None, calls=calls)

    ok, _message = _submit(monkeypatch, core, batch, tmp_path)

    assert ok is True
    assert calls[:3] == ['read-job', 'create-secret', 'create-cm']


def test_a_stale_configmap_of_a_dead_job_is_still_replaced(monkeypatch, tmp_path):
    """No live Job means the ConfigMap left behind is nobody's, and this attempt's Job
    needs its own scripts there -- so the conflict is resolved by replacing it."""
    calls = []
    core = _SubmitCore(conflict=True, calls=calls)
    batch = _SubmitBatch(existing_active=None, calls=calls)

    ok, _message = _submit(monkeypatch, core, batch, tmp_path)

    assert ok is True
    assert calls == ['read-job', 'create-secret', 'create-cm', 'replace-cm', 'create-job',
                     'read-job', 'own-cm']


def test_a_configmap_no_job_ever_mounted_is_deleted(monkeypatch, tmp_path):
    """The one case the cleanup still exists for: the ConfigMap was written and the Job
    create then failed, so nothing mounts it and leaving it behind is a pure leak."""
    calls = []
    core = _SubmitCore(calls=calls)
    batch = _SubmitBatch(existing_active=None, calls=calls)

    def _refuse(namespace, body):
        calls.append('create-job')
        from kubernetes.client.rest import ApiException
        raise ApiException(status=500)

    batch.create_namespaced_job = _refuse
    ok, message = _submit(monkeypatch, core, batch, tmp_path)

    assert ok is False and "could not create postprocessing job" in message
    assert calls == ['read-job', 'create-secret', 'create-cm', 'create-job', 'delete-cm']


def test_a_submit_without_a_token_is_refused_before_anything_is_written(tmp_path):
    """The pod can reach the data plane with nothing else. A Job submitted without it
    would stage nothing and sit on a Secret nobody wrote, so the refusal is here, where
    the caller can read it."""
    with pytest.raises(ValueError, match="token"):
        pj.run_conversion_job(object(), "camp", str(tmp_path), "ns", "img",
                              CMDS, token="")


# -- Unknown is not failure --------------------------------------------------
#
# The conversion Job is a cluster object that outlives the process waiting on it. So the
# wait can end in three ways, not two, and the third one -- "this process can no longer
# see what the Job is doing" -- must never be recorded as the second. A conversion that
# succeeds while the driver is not watching, written down as a failure, sends someone to
# redo hours of work and marks a campaign whose derived data is complete as carrying none.


def _waiting_on_a_job(monkeypatch, batch, core=None):
    """Everything :func:`run_conversion_job` touches before its wait loop, faked.

    Returns nothing: the point is the wait, and every seam in front of it -- kubeconfig,
    the pull secret, the live-log publish -- is stubbed so the only thing a test in this
    group varies is what the API server answers about the Job.
    """
    from unittest import mock

    core = core or mock.Mock()
    monkeypatch.setattr(pj, "publish_live_log", lambda *a, **k: False)
    monkeypatch.setattr(pj, "POLL_SECONDS", 0)
    return [
        mock.patch("robovast.execution.cluster_execution.kube_client.load_kube_config"),
        mock.patch("kubernetes.client.CoreV1Api", return_value=core),
        mock.patch("kubernetes.client.BatchV1Api", return_value=batch),
        mock.patch("robovast.execution.cluster_execution.cluster_execution."
                   "resolve_pull_secret", return_value=""),
    ]


def _job_status(**fields):
    from unittest import mock
    absent = {"active": None, "succeeded": None, "failed": None}
    return mock.Mock(status=types.SimpleNamespace(**{**absent, **fields}))


def _run_the_wait(monkeypatch, batch, core=None, **kwargs):
    import contextlib
    from unittest import mock

    with contextlib.ExitStack() as stack:
        for patch in _waiting_on_a_job(monkeypatch, batch, core):
            stack.enter_context(patch)
        return pj.run_conversion_job(mock.Mock(), "camp", "/nonexistent", "ns", "img",
                                     CMDS, token=_TOKEN, **kwargs)


def test_a_job_status_that_cannot_be_read_is_unknown_not_failed(monkeypatch):
    """An API server that will not answer costs the observation, not the conversion.

    The Job keeps converting while this process cannot see it, so there is no negative
    result to record -- only an open question, which is what ``ok is None`` says.
    """
    from unittest import mock

    from kubernetes.client.rest import ApiException

    batch = mock.Mock()
    batch.read_namespaced_job_status.side_effect = ApiException(status=500,
                                                               reason="Internal")

    ok, message = _run_the_wait(monkeypatch, batch)

    assert ok is None, "the Job was never read as failed"
    assert "unknown" in message and "may still be running" in message
    assert "failed" not in message


def test_giving_up_on_the_wait_is_unknown_not_failed(monkeypatch):
    """The deadline is this process's patience. Nothing here stops the Job, so a wait that
    ends is a wait that ended -- the conversion is still running and its outcome is open.
    """
    from unittest import mock

    batch = mock.Mock()
    batch.read_namespaced_job_status.return_value = _job_status(active=1)

    ok, message = _run_the_wait(monkeypatch, batch, timeout=0)

    assert ok is None
    assert "unknown" in message
    assert "failed" not in message


def _failed_pod_core(container="convert"):
    from unittest import mock

    core = mock.Mock()
    core.list_namespaced_pod.return_value = types.SimpleNamespace(items=[
        types.SimpleNamespace(status=types.SimpleNamespace(
            phase="Failed", reason=None, conditions=[], init_container_statuses=[],
            container_statuses=[types.SimpleNamespace(
                name=container,
                state=types.SimpleNamespace(terminated=types.SimpleNamespace(
                    exit_code=1, reason="Error")))]))])
    return core


def test_a_job_read_as_failed_is_still_reported_in_full(monkeypatch):
    """The genuine failure keeps every part of its report: the Job controller's own
    verdict (``backoffLimit: 0`` makes one container exit terminal), the pod's cause of
    death, and the slot the log pointer is filled into once the log has been settled."""
    from unittest import mock

    batch = mock.Mock()
    batch.read_namespaced_job_status.return_value = _job_status(active=0, failed=1)

    ok, message = _run_the_wait(monkeypatch, batch, _failed_pod_core())

    assert ok is False
    assert "container convert exited 1 (Error)" in message
    assert pj.POINTER_SLOT in message
    assert "see the POSTPROCESSING section" in pj.with_log_pointer(message, __file__)


def test_a_failed_jobs_log_is_published_before_its_verdict_is_returned(monkeypatch):
    """A failure in an initContainer delivers nothing: the pod's stdout -- curl's report,
    the conversion's tee -- is the only account there is, and the Job is reaped 300 s
    later. So the log is read once more, after the failure was read and before the verdict
    leaves, rather than left to a periodic publish that may not come round again.
    """
    from unittest import mock

    batch = mock.Mock()
    batch.read_namespaced_job_status.return_value = _job_status(active=0, failed=1)
    published = []
    monkeypatch.setattr(pj, "POLL_SECONDS", 0)
    with mock.patch("robovast.execution.cluster_execution.kube_client.load_kube_config"), \
            mock.patch("kubernetes.client.CoreV1Api", return_value=_failed_pod_core()), \
            mock.patch("kubernetes.client.BatchV1Api", return_value=batch), \
            mock.patch("robovast.execution.cluster_execution.cluster_execution."
                       "resolve_pull_secret", return_value=""):
        monkeypatch.setattr(pj, "publish_live_log",
                            lambda core, root, ns, name, prefix="": published.append(root) or True)
        ok, _message = pj.run_conversion_job(mock.Mock(), "camp", "/results/camp", "ns",
                                             "img", CMDS, token=_TOKEN)

    assert ok is False
    # Once as the wait began and once more on the failure, both to the campaign's root.
    assert published == ["/results/camp", "/results/camp"]


def test_an_unknown_outcome_reaches_the_caller_without_a_failure_log(monkeypatch,
                                                                    tmp_path):
    """``postprocess_campaign`` carries the third value through rather than collapsing it.

    The failure log is the account of a fault; authoring one for a conversion that may be
    finishing writes a fault into the campaign log that nobody found.
    """
    authored = []
    monkeypatch.setattr(pj, "_write_failure_log",
                        lambda *a, **k: authored.append(a))

    ok, message = _postprocess(monkeypatch, tmp_path, (None, "outcome unknown"))

    assert ok is None and message == "outcome unknown"
    assert not authored


def test_a_failure_whose_pod_left_no_log_gets_an_account_written(monkeypatch, tmp_path):
    """The only path that authors the phase file: a verdict of failed and no log on the
    tree, because the pod could not be read. The message then says so instead of pointing
    at a section that does not exist."""
    ok, message = _postprocess(monkeypatch, tmp_path,
                               (False, pj.job_failed_message("job-x")))

    assert ok is False
    log = tmp_path / "_execution" / "postprocessing.log"
    assert log.is_file() and "job-x" in log.read_text()
    # Written before the pointer was decided, so the pointer says the section is there.
    assert "see the POSTPROCESSING section" in message


# -- re-attaching to a Job this process did not submit -----------------------
#
# The Job outlives the service process, and only the waiter writes the campaign's verdict.
# So a waiter that has no submit half is needed, and it has to be a waiter and nothing
# more: everything the Job mounts belongs to the attempt that created it.


def _reattach(monkeypatch, core, batch, published):
    monkeypatch.setattr("robovast.execution.cluster_execution.kube_client."
                        "load_kube_config", lambda ctx=None: "test")
    monkeypatch.setattr("kubernetes.client.CoreV1Api", lambda: core)
    monkeypatch.setattr("kubernetes.client.BatchV1Api", lambda: batch)
    monkeypatch.setattr(pj, "publish_live_log",
                        lambda *a, **k: published.append("log"))
    monkeypatch.setattr(pj, "record_job_outputs",
                        lambda cid, root, ok, message, should_stop=None: (ok, message))
    return pj.reattach_conversion_job("camp", "/nonexistent", "ns",
                                     pj.campaign_job_name("camp"))


def test_reattaching_waits_and_records_without_submitting_anything(monkeypatch):
    """A live Job's outcome is recorded, and nothing about the Job is written.

    No second Job, no Secret, and no touch of the scripts ConfigMap it is executing out
    of: the kubelet syncs new ConfigMap content into every mount of it, so a waiter that
    wrote them would kill the healthy conversion it is waiting for and then report the
    failure as the campaign's.
    """
    calls = []
    published = []
    core = _SubmitCore(calls=calls)
    batch = _SubmitBatch(existing_active=1, calls=calls)

    ok, message = _reattach(monkeypatch, core, batch, published)

    assert ok is True and "complete" in message
    assert calls == ['read-job']
    # And the live log keeps being published while it waits: nothing the pod writes leaves
    # it until it exits, so this is the only account of a running conversion anyone has.
    assert published == ["log"]


def test_a_job_that_cannot_be_read_yields_no_verdict(monkeypatch):
    """``ok is None``, so the caller writes nothing.

    A Job that is absent, finished, or on an unreadable API server is one this process did
    not observe. Recording a failure for it would mark a campaign whose conversion
    succeeded as having derived nothing -- worse than the stale record it replaces, because
    it reads as a fresh finding.
    """
    calls = []
    core = _SubmitCore(calls=calls)
    batch = _SubmitBatch(existing_active=None, calls=calls)

    ok, message = _reattach(monkeypatch, core, batch, [])

    assert ok is None
    assert "no longer active" in message
    assert calls == ['read-job']


# -- a campaign stopped while its postprocessing Job runs --------------------
#
# The stop that reaches a campaign's runs cannot reach this Job: that teardown is scoped to
# `jobgroup=scenario-runs` so that it cannot cancel a content-addressed image build a
# sibling campaign may be waiting on, and this Job is in `jobgroup=postprocessing`. So the
# waiter acts on the flag itself, or a stop during postprocessing does nothing at all --
# and because it is the waiter, a Job this process merely re-attached to is stoppable too.


def _stopping_after(n):
    """A predicate that lets *n* polls happen and then reports the campaign stopped."""
    polls = {"n": 0}

    def should_stop():
        polls["n"] += 1
        return polls["n"] > n

    return should_stop


def test_a_stop_deletes_the_job_it_is_waiting_on(monkeypatch):
    """Otherwise the driver waits out a conversion that can run for hours.

    Deleted on the campaign's terms -- now, and taking its pods with it -- because an
    interrupted bag costs nothing: a bag records itself as converted only once its handlers
    have finished, so the next run redoes it.
    """
    from unittest import mock

    batch = mock.Mock()
    batch.read_namespaced_job_status.return_value = _job_status(active=1)

    ok, _message = _run_the_wait(monkeypatch, batch, should_stop=_stopping_after(1))

    assert ok is False
    batch.delete_namespaced_job.assert_called_once()
    body = batch.delete_namespaced_job.call_args.kwargs["body"]
    assert body.grace_period_seconds == 0
    assert body.propagation_policy == "Foreground"


def test_a_cancelled_job_is_read_as_stopped_rather_than_failed_or_unknown(monkeypatch):
    """Not ``None``: nothing is going to produce the outcome now, so it is not an open
    question the way a Job this process merely lost sight of is.

    The message lands on ``postprocessing_error``, where a reader decides whether something
    is wrong -- so it says the campaign was stopped, that its results are untouched, and
    what brings the derived data back.
    """
    from unittest import mock

    batch = mock.Mock()
    batch.read_namespaced_job_status.return_value = _job_status(active=1)

    ok, message = _run_the_wait(monkeypatch, batch, should_stop=lambda: True)

    assert ok is False
    assert "cancelled" in message and "stopped" in message
    assert "untouched" in message and "re-run postprocessing" in message
    assert "failed" not in message and "unknown" not in message


@pytest.mark.parametrize("failure", ["refused", "unreachable"])
def test_a_job_that_cannot_be_deleted_still_reports_the_cancellation(monkeypatch, failure):
    """The campaign is being stopped either way, and the Job's own TTL collects it.

    Both shapes matter: the API server can refuse the delete, and a stop can be a Ctrl+C
    that takes the route to the cluster with it, which does not arrive as an
    ``ApiException`` at all. Raising here would replace the operator's own stop with a
    traceback from tidying up after it.
    """
    from unittest import mock

    from kubernetes.client.rest import ApiException

    batch = mock.Mock()
    batch.read_namespaced_job_status.return_value = _job_status(active=1)
    batch.delete_namespaced_job.side_effect = (
        ApiException(status=403, reason="Forbidden") if failure == "refused"
        else OSError("connection refused"))

    ok, message = _run_the_wait(monkeypatch, batch, should_stop=lambda: True)

    assert ok is False and "cancelled" in message


def test_a_job_nobody_stops_is_never_deleted(monkeypatch):
    """No predicate, and one that stays false, must both leave the Job alone."""
    from unittest import mock

    for predicate in (None, lambda: False):
        batch = mock.Mock()
        batch.read_namespaced_job_status.return_value = _job_status(active=0, succeeded=1)

        ok, _message = _run_the_wait(monkeypatch, batch, should_stop=predicate)

        assert ok is True
        batch.delete_namespaced_job.assert_not_called()


def test_a_cancelled_outcome_authors_no_failure_log(monkeypatch, tmp_path):
    """The failure log is the account of a fault, and a stop is not one.

    Nothing is written or echoed as a failure, which would send whoever reads the campaign
    log looking for a fault that is not there; what the Job wrote before it was deleted is
    the whole account a cancelled postprocess has.
    """
    authored = []
    monkeypatch.setattr(pj, "_write_failure_log", lambda *a, **k: authored.append(a))

    ok, message = _postprocess(
        monkeypatch, tmp_path,
        (False, "postprocessing cancelled: the campaign was stopped."),
        should_stop=lambda: True)

    assert ok is False
    assert not authored                       # no fault was written into the campaign log
    assert message == "postprocessing cancelled: the campaign was stopped."
    assert pj.POINTER_SLOT not in message     # nor pointed at as a failure would be


def test_the_predicate_reaches_the_waiter_and_the_record(monkeypatch, tmp_path):
    """Both halves need it: the waiter to end the Job, the record to tell that stop from a
    failure once it has."""
    seen = {}
    _inputs(monkeypatch)

    def _wait(*_a, **kwargs):
        seen["waiter"] = kwargs.get("should_stop")
        return True, "postprocessing complete"

    def _record(*_a, **kwargs):
        seen["record"] = kwargs.get("should_stop")
        return True, "postprocessing complete"

    monkeypatch.setattr(pj, "run_conversion_job", _wait)
    monkeypatch.setattr(pj, "record_job_outputs", _record)

    def predicate():
        return False

    pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns", token=_TOKEN,
                            should_stop=predicate)

    assert seen["waiter"] is predicate
    assert seen["record"] is predicate


def test_the_token_reaches_the_job(monkeypatch, tmp_path):
    """`postprocess_campaign` is the seam both entry points go through, so the token has
    one path to the Secret: through here, by keyword, never defaulted."""
    seen = {}
    _inputs(monkeypatch)

    def _wait(*_a, **kwargs):
        seen.update(kwargs)
        return True, "postprocessing complete"

    monkeypatch.setattr(pj, "run_conversion_job", _wait)

    pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns", token=_TOKEN)

    assert seen["token"] == _TOKEN


# -- the disk the stage reserves, from the campaign's own tree ----------------


def test_stage_ephemeral_request_scales_with_the_campaign():
    """The staged tree is the campaign, so the disk request has to describe this one.

    Left at the floor it describes a typical campaign, and a larger one is scheduled onto a
    node that cannot hold it and evicted partway through -- which loses the whole
    postprocessing rather than the excess.
    """
    gib = 1 << 30
    floor = to_bytes(pj.POSTPROCESS_EPHEMERAL_FLOOR)
    ceiling = to_bytes(pj.POSTPROCESS_EPHEMERAL_CAP)

    # A campaign smaller than the floor still asks for the floor.
    assert to_bytes(pj.stage_ephemeral_request(1 * gib)) == floor
    # One larger than it asks for its own size, with headroom for what the conversion
    # writes into the same mount.
    assert to_bytes(pj.stage_ephemeral_request(100 * gib)) > 100 * gib
    # Never above its own limit: a request over its limit is not a pod spec.
    assert to_bytes(pj.stage_ephemeral_request(10_000 * gib)) == ceiling
    # A size nobody knows leaves the floor standing.
    assert to_bytes(pj.stage_ephemeral_request(None)) == floor


def test_stage_container_carries_the_campaigns_own_disk_request():
    """The figure has to reach the pod spec, which is the only thing the scheduler reads."""
    gib = 1 << 30
    containers = _containers(_manifest(stage_bytes=100 * gib))
    asked = containers["stage"]["resources"]["requests"]["ephemeral-storage"]

    assert to_bytes(asked) > 100 * gib
    # and the guard on what staging may hold in memory is unchanged by it
    assert containers["stage"]["resources"]["requests"]["memory"] == \
        pj.stage_resources()["requests"]["memory"]


def _tree(tmp_path):
    root = tmp_path / "camp"
    for rel, size in (("cfg/0/rosbag2/b.mcap", 8192), ("cfg/0/out.csv", 512),
                      ("_jobs/batch-3/j/rosbag2/b.mcap", 4096),
                      ("_jobs/batch-0/j/rosbag2/b.mcap", 2048)):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return root


def test_a_per_batch_job_sizes_only_its_own_batch(tmp_path):
    """A search creates one of these per batch, while the campaign is still growing, and
    `_jobs/` is where the bags are: the batch's own is what its stage will extract."""
    root = _tree(tmp_path)

    assert pj.stage_bytes(str(root), skip_bags=False, batch_jobs="batch-3") == \
        8192 + 512 + 4096


def test_a_whole_campaign_job_sizes_the_whole_tree(tmp_path):
    root = _tree(tmp_path)

    assert pj.stage_bytes(str(root), skip_bags=False, batch_jobs="") == \
        8192 + 512 + 4096 + 2048


def test_bags_are_not_reserved_for_when_the_pod_will_not_stage_them(tmp_path):
    """No conversion container means no bag is staged, so none is reserved for."""
    root = _tree(tmp_path)

    assert pj.stage_bytes(str(root), skip_bags=True, batch_jobs="") == 512


def test_a_tree_that_cannot_be_walked_leaves_the_floor_standing(tmp_path):
    """Sizing is advisory: it must never be why a campaign is not postprocessed."""
    assert pj.stage_bytes(str(tmp_path / "gone"), skip_bags=False, batch_jobs="") == 0
    assert to_bytes(pj.stage_ephemeral_request(0)) == to_bytes(pj.POSTPROCESS_EPHEMERAL_FLOOR)
