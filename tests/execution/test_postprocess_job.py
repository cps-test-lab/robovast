# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for analysis postprocessing surfacing."""

import logging
import types

import pytest

import robovast.execution.cluster_execution.postprocess_job as pj
from robovast.common.index_db import DSN_ENV


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


def _patch_failed_conversion(monkeypatch, synced):
    # `postprocess_campaign` reads the three facts the manifest needs through
    # `_submit_inputs`, which fetches them from the store when the root does not hold the
    # campaign -- the pod is what stages it. Patched at that seam so these tests need
    # neither a campaign tree nor a store.
    monkeypatch.setattr(pj, "_submit_inputs",
                        lambda cfg, cid, cr, skip=None, skip_rosout=False:
                        ([{"plugins": []}], "img", ()))
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: (False, "boom"))
    monkeypatch.setattr(pj, "sync_outputs",
                        lambda cfg, cid, cr, force=False: synced.append(cr) or 1)


def test_postprocess_syncs_outputs_even_on_conversion_failure(monkeypatch, tmp_path):
    """A failed conversion Job must still sync its teed postprocessing.log.

    The Job mirrors that log (with the conversion error) to the object store even
    on failure; syncing it lands the POSTPROCESSING section in the campaign log the
    web UI shows — which is the only place the error is readable, since the Job is
    reaped by ttlSecondsAfterFinished minutes after it fails.
    """
    synced = []
    _patch_failed_conversion(monkeypatch, synced)

    ok, msg = pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns")

    assert ok is False and msg == "boom"
    assert synced == [str(tmp_path)]  # synced despite the failure


def test_postprocess_echoes_conversion_log_to_console_on_failure(
        monkeypatch, tmp_path, caplog):
    """The synced postprocessing.log is also echoed to the service console."""
    _patch_failed_conversion(monkeypatch, [])
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir()
    (exec_dir / "postprocessing.log").write_text(
        "rosbags_process.py: error: unrecognized arguments: --output-root\n")

    with caplog.at_level(logging.WARNING, logger=pj.logger.name):
        pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns")

    assert any("unrecognized arguments: --output-root" in r.message
               for r in caplog.records)


# ---------------------------------------------------------------------------
# a pod that cannot start
# ---------------------------------------------------------------------------

_CMDS = [{"plugins": [{"rosbags_tf_to_csv": {"frames": "all"}}], "bag_dir": "rosbag2"}]
_S3 = ("http://s3:9000", "ak", "sk", "bucket", "camp/")


def test_the_conversion_pod_can_pull_its_own_images():
    """The Job runs the controller image and the campaign's execution image, either of
    which may sit in a registry the kubelet has no credential for. Without this the pod stays
    ImagePullBackOff while the Job stays `active`, so the waiter reported a timeout and named
    neither image nor registry -- the same omission, in the same direction, that the build Job
    had."""
    m = pj.build_manifest("c1", "img:1", _CMDS, _S3, "ns", pull_secret_name="robovast-registry")
    assert m["spec"]["template"]["spec"]["imagePullSecrets"] == [{"name": "robovast-registry"}]


def test_no_credential_configured_leaves_the_pod_spec_alone():
    """A public-registry deployment needs none, and naming an absent Secret would itself keep
    the pod from starting."""
    m = pj.build_manifest("c1", "img:1", _CMDS, _S3, "ns")
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

    A Job that dies before its first ``tee`` uploads no log, so the section is never
    written; promising one anyway costs the reader a hunt through an empty panel and hides
    the real fault, which is that the conversion never started.
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


# -- a forced re-postprocess must force the download too ---------------------

def test_a_forced_repostprocess_forces_the_download(monkeypatch, tmp_path):
    """`force` means "bypass the per-rosbag caches and reconvert", so the Job REPLACES
    CSVs in the object store -- mutating objects in place.

    `download_prefix` skips a local file whose size already matches, which is right for
    the immutable durable home and wrong here: a regenerated CSV that happens to keep its
    byte count (a changed value of the same width, a re-derived column) is then skipped,
    and the campaign root keeps the file the user asked to replace. The re-run reports
    success and changes nothing that anyone can see, which is the worst shape a bug can
    take. `download_prefix`'s own docstring names this case as what `force=True` is for.
    """
    seen = {}
    monkeypatch.setattr(pj, "_submit_inputs",
                        lambda cfg, cid, cr, skip=None, skip_rosout=False:
                        ([{"plugins": []}], "img", ()))
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: (False, "stop here"))
    monkeypatch.setattr(pj, "sync_outputs",
                        lambda cfg, cid, cr, force=False: seen.setdefault("force", force))

    pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns", force=True)
    assert seen["force"] is True, "a forced reconversion synced without forcing the fetch"


def test_an_ordinary_postprocess_leaves_the_skip_in_place(monkeypatch, tmp_path):
    """The default stays skip-existing: a first postprocess, and the search loop's
    per-batch sync, both re-list a prefix that only grows, and re-downloading every
    earlier batch's CSVs each time would add latency to a loop already bounded by a
    no-progress deadline."""
    seen = {}
    monkeypatch.setattr(pj, "_submit_inputs",
                        lambda cfg, cid, cr, skip=None, skip_rosout=False:
                        ([{"plugins": []}], "img", ()))
    monkeypatch.setattr(pj, "run_conversion_job", lambda *a, **k: (False, "stop here"))
    monkeypatch.setattr(pj, "sync_outputs",
                        lambda cfg, cid, cr, force=False: seen.setdefault("force", force))

    pj.postprocess_campaign(object(), "camp", str(tmp_path), "ns")
    assert seen["force"] is False


class _FakeStore:
    """The three calls sync_outputs makes, and a record of them."""

    def __init__(self, manifest=None, objects=None):
        self._manifest = manifest
        self._objects = objects or {}
        self.calls = {}

    def read_object(self, bucket, key):
        self.calls.setdefault('read', []).append(key)
        return self._manifest

    def stat_object(self, bucket, key):
        return len(self._objects.get(key, b''))

    def download_object(self, bucket, key, dst):
        self.calls.setdefault('fetched', []).append(key)
        if key not in self._objects:
            return False
        with open(dst, 'wb') as f:
            f.write(self._objects[key])
        return True

    def download_prefix(self, bucket, prefix, local_dir, force=False, **_kw):
        # Per prefix, because `sync_outputs` makes two different calls: `_execution/`
        # unconditionally and first, then the legacy staging prefix only where no
        # manifest exists. One shared number could not tell a test which it saw.
        self.calls.setdefault('prefixes', []).append((prefix, force))
        if prefix.endswith('_execution'):
            self.calls['execution'] = (prefix, force)
            return 1
        self.calls['prefix'] = (prefix, force)
        return 3

    def delete_prefix(self, bucket, prefix):
        self.calls['deleted'] = prefix
        return 7


def _patch_store(monkeypatch, store):
    """Patched on the real module rather than substituted in ``sys.modules``: ``from . import
    in_pod_storage`` resolves through the parent package's attribute, so a sys.modules entry
    only wins while nothing has imported it yet -- which made an earlier version of this
    test pass alone and fail in the suite.
    """
    from robovast.execution.cluster_execution import in_pod_storage
    monkeypatch.setattr(in_pod_storage, 'campaign_storage_location',
                        lambda cfg, cid: ('b', 'p/'))
    monkeypatch.setattr(in_pod_storage, 'storage_client_for', lambda cfg: store)


def test_a_campaign_without_a_manifest_falls_back_to_the_staging_prefix(monkeypatch, tmp_path):
    """A campaign converted by an older service has its outputs only under the legacy
    prefix, so upgrading the service must not orphan them -- and `force` still has to reach
    the download, not merely be accepted."""
    store = _FakeStore(manifest=None)
    _patch_store(monkeypatch, store)

    # 3 from the legacy prefix plus the `_execution/` fetch every sync makes.
    assert pj.sync_outputs(object(), 'camp', str(tmp_path), force=True) == 4
    assert store.calls['prefix'] == ('p/_postproc', True)
    assert store.calls['execution'] == ('p/_execution', True)


def test_the_manifest_fetches_exactly_what_it_names(monkeypatch, tmp_path):
    """The point of the manifest: one known key read, then only the objects it names.

    Listing the campaign prefix instead would walk every rosbag to find the CSVs beside
    them, which is the cost the duplicated staging prefix existed to avoid.
    """
    store = _FakeStore(
        manifest=b'c1/r1/out.csv\n_execution/postprocessing.log\n',
        objects={'p/c1/r1/out.csv': b'x,y\n', 'p/_execution/postprocessing.log': b'ok\n'})
    _patch_store(monkeypatch, store)

    # The two the manifest names, plus the `_execution/` fetch.
    assert pj.sync_outputs(object(), 'camp', str(tmp_path)) == 3
    assert store.calls['fetched'] == ['p/c1/r1/out.csv', 'p/_execution/postprocessing.log']
    assert (tmp_path / 'c1' / 'r1' / 'out.csv').read_bytes() == b'x,y\n'


def test_a_manifest_path_cannot_write_outside_the_campaign(tmp_path):
    """The manifest is written by a container and consumed by the service, so it is input:
    an absolute or upward path would have the fetch write outside the campaign root."""
    paths = pj._manifest_paths(
        b'ok/one.csv\n/etc/passwd\n../../escape.csv\na/../b/two.csv\n\n')

    assert '/etc/passwd' not in paths and '../../escape.csv' not in paths
    assert 'ok/one.csv' in paths


def test_the_staging_prefix_is_cleared_once_the_outputs_are_canonical(monkeypatch, tmp_path):
    """It is scratch that nothing has ever emptied, so every postprocessed campaign kept a
    second copy of its derived data under a prefix every reader is told to ignore."""
    store = _FakeStore(manifest=b'c1/r1/out.csv\n',
                       objects={'p/c1/r1/out.csv': b'x\n'})
    _patch_store(monkeypatch, store)

    pj.sync_outputs(object(), 'camp', str(tmp_path))
    assert store.calls['deleted'] == 'p/_postproc'


def test_nothing_is_cleared_when_nothing_was_synced(monkeypatch, tmp_path):
    """A sync that fetched nothing is not evidence the outputs are safe elsewhere, and the
    staging prefix may still be the only copy of a failed conversion's log."""
    store = _FakeStore(manifest=b'')
    _patch_store(monkeypatch, store)

    # Only the unconditional `_execution/` fetch; the manifest named nothing.
    assert pj.sync_outputs(object(), 'camp', str(tmp_path)) == 1
    assert 'deleted' not in store.calls


# -- one campaign, many conversions ------------------------------------------

def test_two_conversions_of_one_campaign_get_different_job_names():
    """A search converts once per batch, and the Job name was the campaign's alone.

    Measured: batch 0's Job ran 16s and synced 146 outputs; batch 1's was "created" and
    "succeeded" in the SAME second and synced 0. `create_namespaced_job` returned 409, the
    code fell through to wait on the existing Job -- which was batch 0's, already complete --
    read `succeeded` and reported "rosbag conversion complete" having converted nothing. The
    extractor then refused the batch for a missing clearance, naming the world.

    The 409 fallthrough is right for what it was written for: a one-shot campaign
    postprocess, retried, should wait on the in-flight Job rather than duplicate it. It is
    wrong as soon as one campaign converts more than once, so the Job's identity has to say
    WHICH conversion it is.
    """
    names = {pj.build_manifest("camp-2026-08-25-1234", "img", [{"plugins": []}],
                               ("e", "a", "s", "b", "p/"), "ns",
                               discriminator=d)["metadata"]["name"]
             for d in ("batch-0", "batch-1", "batch-2")}
    assert len(names) == 3, f"batches collided on one Job name: {names}"


def test_the_same_conversion_keeps_a_stable_name():
    """So a genuine retry of one conversion still waits on the in-flight Job instead of
    launching a second copy of it -- the behaviour the 409 fallthrough exists for."""
    def name(disc):
        return pj.build_manifest("camp-x", "img", [{"plugins": []}],
                                 ("e", "a", "s", "b", "p/"), "ns",
                                 discriminator=disc)["metadata"]["name"]
    assert name("batch-3") == name("batch-3")


def test_no_discriminator_leaves_the_campaign_level_name_unchanged():
    """The campaign-level path converts once and its Job name is part of what an operator
    looks for; nothing about it should move because a search needed more names."""
    plain = pj.build_manifest("camp-x", "img", [{"plugins": []}],
                              ("e", "a", "s", "b", "p/"), "ns")["metadata"]["name"]
    assert plain == "robovast-postproc-camp-x"


def test_long_campaign_ids_stay_within_the_label_limit_and_stay_distinct():
    """Kubernetes copies the name into the pod template's `job-name` label, capped at 63.
    Truncation must not be what makes two batches collide again -- the hash is taken over
    the discriminated identity for exactly that reason."""
    long_id = "nav-search-adaptive-reps-2026-08-25-13573569-with-a-long-suffix"
    names = set()
    for disc in ("batch-0", "batch-1", "batch-10", "batch-1-reps-3", "batch-1-reps-5"):
        n = pj.build_manifest(long_id, "img", [{"plugins": []}],
                              ("e", "a", "s", "b", "p/"), "ns",
                              discriminator=disc)["metadata"]["name"]
        assert len(n) <= 63, f"{n} is {len(n)} chars"
        names.add(n)
    assert len(names) == 5, f"truncation collapsed distinct conversions: {names}"


def test_the_scripts_configmap_is_discriminated_too(monkeypatch):
    """Each conversion deletes its scripts ConfigMap when it finishes. Sharing one name
    across conversions means a finishing batch can delete the ConfigMap another is
    mounting, which surfaces as a pod stuck in ContainerCreating rather than as a name
    clash."""
    a = pj._scripts_cm_name("camp-x", discriminator="batch-0")
    b = pj._scripts_cm_name("camp-x", discriminator="batch-1")
    assert a != b
    assert pj._scripts_cm_name("camp-x") == "robovast-postproc-scripts-camp-x"


def test_the_staging_container_files_its_own_failure():
    """Its own channel, because its failure means the conversion container never starts:
    nothing tees a line, and a log it would have to upload needs the very store whose
    absence it may be reporting. Staging also pulls the campaign onto the node, so it is
    the step that meets a full disk first -- the failure most in need of an explanation
    had none.

    The channel is now the exit code the ``stage`` container leaves in the pod status, so
    what this pins is that the container in the manifest is the one whose vocabulary
    :func:`pod_failure_reason` reads.
    """
    from robovast.execution.cluster_execution import postprocess_stage

    m = pj.build_manifest("c1", "img:1", _CMDS, _S3, "ns")
    stage = m["spec"]["template"]["spec"]["initContainers"][0]

    assert stage["name"] == pj.STAGE_CONTAINER
    # Wrapped in a shell for the umask the shared tree needs (see the scheduling tests),
    # so what is pinned here is that this container is the entry point, not the wrapper.
    assert "robovast.execution.cluster_execution.postprocess_stage" in " ".join(
        stage["command"])
    # Every code the entry point can return is one the reader gets in words.
    assert set(postprocess_stage.STAGE_EXIT_REASONS)


def test_the_conversion_writes_the_campaign_tree_and_uploads_nothing():
    """Outputs go to the paths they are read from -- so a staging copy is not the price of
    a cheap fetch, and no mapping step stands between an output and its canonical key.

    The conversion reaches the store not at all: it reads and writes the one shared mount,
    and the host container that follows it is what uploads. That is what lets this
    container be an arbitrary user image, and it is why ``--output-root`` is the campaign
    tree itself rather than a separate output volume.
    """
    script = pj._conversion_script([{"plugins": []}], force=False, campaign_id="c1")

    assert f"--output-root {pj.CAMPAIGN_MOUNT}/c1" in script
    assert script.rstrip().endswith("exit $rc")
    # No store, in any form: no client, no credentials, no staging prefix. These are the
    # NAMES of the variables that must not appear, which is why the secret scanner has to
    # be told there is no secret here.
    for absent in ("mc ", "S3_BUCKET", "S3_ACCESS_KEY",  # gitleaks:allow
                   "S3_CAMPAIGN_PREFIX", pj.POSTPROC_PREFIX, pj.OUTPUT_MANIFEST):
        assert absent not in script, absent


def test_a_failed_conversion_still_writes_a_postprocessing_section(tmp_path):
    """The phase file IS the section: every surface assembles the campaign log from the
    files that exist, so a conversion that wrote nothing left the phases stopping after RUN
    with the failure reported only in a status field elsewhere.
    """
    log = tmp_path / '_execution' / 'postprocessing.log'
    pj._write_failure_log(object(), 'camp', str(tmp_path), str(log),
                          pj.job_failed_message('job-x'))
    text = log.read_text()

    assert 'job-x' in text
    # The slot is decided by whether this file exists, so inside it there is nothing for it
    # to say -- and it must never reach a reader.
    assert pj.POINTER_SLOT not in text
    # No cause is read into the missing log. Anything the pod would have to upload is
    # suppressed by an unreachable store and by an outright kill, and reading a diagnosis
    # into that absence is how a wrong one gets stated with confidence. The per-stage exit
    # code is what survives both, and the text says to read that instead.
    assert 'exits with a code of its own' in text and "pod's status" in text
    # And it names the two candidates, because initContainers are what can fail this early.
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
    core = _core([_pod(init=[_CS(pj.STAGE_CONTAINER, exit_code=1)],
                       main=[_CS(pj.HOST_CONTAINER)])])

    assert pj.pod_failure_reason(core, 'ns', 'job-x') == (
        f'container {pj.STAGE_CONTAINER} exited 1')


def test_an_unreadable_pod_list_never_becomes_the_failure():
    """This runs while reporting a failure, so it must not raise one of its own."""
    class _Broken:
        def list_namespaced_pod(self, namespace, label_selector):
            raise RuntimeError('no api')

    assert pj.pod_failure_reason(_Broken(), 'ns', 'job-x') == ''
    # And the message is still complete without it.
    assert 'job-x' in pj.job_failed_message('job-x', pod_reason='')


def test_each_staging_failure_exits_with_its_own_code(monkeypatch):
    """An exit code is the one channel that always survives -- it is in the pod status with
    no store to reach and no file to write. A stage that fails *because* the object store
    is unreachable can still say so; an uploaded log cannot, since the upload needs the
    very thing whose absence it would report.
    """
    from robovast.execution.cluster_execution import postprocess_stage

    # Not 1: a bare "exited 1" is every failure at once and names none of them.
    assert 1 not in postprocess_stage.STAGE_EXIT_REASONS
    # Every code the entry point can return has words, so nothing arrives as a bare number.
    assert set(postprocess_stage.STAGE_EXIT_REASONS) == {41, 42, 43}

    # An unusable environment is the one of the three a test can drive end to end, and it
    # is the code the other two exist to be distinguished FROM.
    monkeypatch.delenv(postprocess_stage.ENV_CAMPAIGN_ID, raising=False)
    monkeypatch.delenv(postprocess_stage.ENV_STAGE_DEST, raising=False)
    assert postprocess_stage.main() == 41


def test_a_staging_exit_code_is_reported_in_words():
    """`exited 42` is a code chosen in order to be read; the reader should not have to."""
    from robovast.execution.cluster_execution import postprocess_stage

    core = _core([_pod(init=[_CS(pj.STAGE_CONTAINER, exit_code=42)])])

    reason = pj.pod_failure_reason(core, 'ns', 'job-x')

    assert reason == (f'container {pj.STAGE_CONTAINER} '
                      f'{postprocess_stage.STAGE_EXIT_REASONS[42]}')
    assert 'object store' in reason


def test_an_unknown_exit_code_still_reports_the_number():
    """The translation is a courtesy on top of the code, never a filter in front of it."""
    core = _core([_pod(init=[_CS(pj.STAGE_CONTAINER, exit_code=7, reason='Error')])])

    assert pj.pod_failure_reason(core, 'ns', 'job-x') == (
        f'container {pj.STAGE_CONTAINER} exited 7 (Error)')


class _FakeBatch:
    """Enough of BatchV1Api to drive _adopt_or_replace."""

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


def test_a_running_job_is_adopted_not_replaced():
    """Two postprocesses of one campaign must not race each other's pods."""
    batch = _FakeBatch(active=1)

    assert pj._adopt_or_replace(batch, 'ns', 'job-x', {}) is True
    assert 'delete' not in batch.calls


def test_a_finished_job_is_replaced_rather_than_waited_on():
    """The Job name comes from the campaign and is reused every retrigger, so waiting on
    whatever answers to it reported the PREVIOUS attempt's outcome as this one's -- against
    a pod whose containers ran an earlier version of the script.
    """
    batch = _FakeBatch(active=None)

    assert pj._adopt_or_replace(batch, 'ns', 'job-x', {}) is True
    assert batch.calls.index('delete') < batch.calls.index('create')


def test_staging_memory_is_a_bound_and_not_headroom_for_the_campaign():
    """Staging must not need a limit that grows with the campaign it stages.

    A whole-prefix mirror's footprint grows with the number of objects it moves, so it had
    to be given headroom -- and headroom is a number that a large enough campaign still
    exceeds, killed rather than failing, with no chance to report it. Staging streams one
    object at a time into a bounded buffer, so its footprint is set by the largest single
    object and a *small* limit is the correct one: sizing it for the largest campaign
    anyone might postprocess would only hide a regression in that streaming.

    Pinned against the conversion's, because that is the number it used to have to match.
    """
    def _mem_mib(spec):
        value = spec["limits"]["memory"]
        assert value.endswith(("Gi", "Mi")), value
        return int(value[:-2]) * (1024 if value.endswith("Gi") else 1)

    assert _mem_mib(pj.POSTPROCESS_INIT_RESOURCES) < _mem_mib(pj.POSTPROCESS_RESOURCES)
    # The disk request, by contrast, is the campaign's and stays: it is what reserves the
    # shared emptyDir the campaign lands in.
    assert pj.POSTPROCESS_INIT_RESOURCES["requests"]["ephemeral-storage"]


def test_a_partly_populated_root_does_not_read_as_a_whole_campaign(tmp_path, monkeypatch):
    """The submitter's inputs are assembled per file, never chosen wholesale.

    "Is this root local?" has no single answer: the service's cache directory holds
    whatever earlier calls left there, so it can carry the `.vast` and not
    `execution.yaml`. Deciding from one file that the rest are present turns that partial
    state into "no such file" at submit time, on a campaign whose results are fine -- which
    is what a real retrigger did.
    """
    from robovast.execution.cluster_execution import in_pod_storage

    (tmp_path / "_config").mkdir()
    (tmp_path / "_config" / "x.vast").write_text(
        "version: 3\nmetadata: {name: x}\nresults_processing:\n"
        "  postprocessing:\n  - rosbags_tf_to_csv\n")
    fetched = []

    class _Store:
        def list_keys(self, bucket, prefix):
            return [f"{prefix}x.vast"]

        def download_object(self, bucket, key, dst):
            fetched.append(key)
            if key.endswith("execution.yaml"):
                with open(dst, "w", encoding="utf-8") as f:
                    f.write("image: img:1\n")
                return True
            return False

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("b", "p/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: _Store())

    rosbag_cmds, image, _tolerate = pj._submit_inputs(object(), "camp", str(tmp_path))

    # The local .vast was used, and only what was actually missing came from the store.
    assert rosbag_cmds and image == "img:1"
    assert "p/_execution/execution.yaml" in fetched
    assert not any(k.endswith(".vast") for k in fetched)


def test_a_root_without_the_ledger_does_not_read_as_no_interventions(tmp_path, monkeypatch):
    """Absence of the intervention ledger is not absence of interventions.

    Its reader answers "nobody intervened" for a missing file and for a file saying so
    alike, so a root that happens not to hold it drops every path the conversion was told
    to tolerate. Those paths are the bags of jobs the runner invalidated -- unfinalized,
    impossible to open, ever -- so dropping them does not lose a nicety: it fails the whole
    conversion on bags nothing was ever going to read. A 30 GB campaign's postprocess died
    that way, on eight bags out of 1452.
    """
    from robovast.execution.cluster_execution import in_pod_storage

    (tmp_path / "_config").mkdir()
    (tmp_path / "_config" / "x.vast").write_text(
        "version: 3\nmetadata: {name: x}\nresults_processing:\n"
        "  postprocessing:\n  - rosbags_tf_to_csv\n")
    (tmp_path / "_execution").mkdir()
    (tmp_path / "_execution" / "execution.yaml").write_text("image: img:1\n")
    # The ledger is NOT here, and the store has one.

    class _Store:
        def list_keys(self, bucket, prefix):
            return [f"{prefix}x.vast"]

        def download_object(self, bucket, key, dst):
            if key.endswith("execution.yaml"):
                with open(dst, "w", encoding="utf-8") as f:
                    f.write("image: img:1\n")
                return True
            if key.endswith("interventions.json"):
                with open(dst, "w", encoding="utf-8") as f:
                    f.write('[{"kind": "invalid", "job_dir": "_jobs/batch-1/job-27"}]')
                return True
            return False

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("b", "p/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: _Store())

    _cmds, _image, tolerate = pj._submit_inputs(object(), "camp", str(tmp_path))

    assert "_jobs/batch-1/job-27" in tolerate


def test_the_running_job_publishes_its_log_as_the_phase(tmp_path, monkeypatch):
    """Nothing the pod writes leaves it until it exits: the log is on a shared volume and
    the last container uploads it at the end. So a conversion measured in minutes showed an
    empty POSTPROCESSING section for all of them, and the only way to watch one was a pod
    name nobody off-cluster has.

    Read from the pod's stdout rather than its volume, because the volume is the pod's own
    and nothing outside can see it -- every container in declaration order, so staging and
    conversion read as the one section they are.
    """
    from robovast.execution.cluster_execution import in_pod_storage

    uploaded = {}

    class _Store:
        def upload_file(self, local_path, bucket, key):
            with open(local_path, encoding="utf-8") as f:
                uploaded[key] = f.read()

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            spec = types.SimpleNamespace(
                init_containers=[types.SimpleNamespace(name="stage"),
                                 types.SimpleNamespace(name="convert")],
                containers=[types.SimpleNamespace(name="host")])
            pod = types.SimpleNamespace(metadata=types.SimpleNamespace(name="p1"),
                                        spec=spec)
            return types.SimpleNamespace(items=[pod])

        def read_namespaced_pod_log(self, name, namespace, container):
            from kubernetes import client
            if container == "host":
                raise client.exceptions.ApiException(status=400)  # not started yet
            return f"{container} said something"

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("b", "p/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: _Store())

    assert pj.publish_live_log(_Core(), object(), "camp", "ns", "job-x") is True

    text = uploaded["p/_execution/postprocessing.log"]
    # Declaration order, and the container that has not started is simply absent -- which
    # is also how "this stage has not run" should look.
    assert text.index("stage said") < text.index("convert said")
    assert "host" not in text

def test_a_pod_that_cannot_be_read_does_not_fail_the_postprocess(monkeypatch):
    """This is a read for someone watching. It must not fail the work it is watching."""
    class _Broken:
        def list_namespaced_pod(self, namespace, label_selector):
            raise RuntimeError("no api")

    assert pj.publish_live_log(_Broken(), object(), "camp", "ns", "job-x") is False

def test_the_live_log_comes_from_the_newest_pod():
    """A Job can have more than one pod -- a backoffLimit retry makes another, and replacing
    a finished Job of the same name makes another still -- and the listing does not promise
    an order. Publishing from an arbitrary one would make the section alternate between two
    attempts each time this is called, which reads worse than either of them.
    """
    import datetime

    from robovast.execution.cluster_execution import in_pod_storage

    uploaded = {}

    class _Store:
        def upload_file(self, local_path, bucket, key):
            with open(local_path, encoding="utf-8") as f:
                uploaded[key] = f.read()

    def _pod(name, when):
        return types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name, creation_timestamp=when),
            spec=types.SimpleNamespace(
                init_containers=[], containers=[types.SimpleNamespace(name="host")]))

    old = datetime.datetime(2026, 9, 2, 10, tzinfo=datetime.timezone.utc)
    new = datetime.datetime(2026, 9, 2, 11, tzinfo=datetime.timezone.utc)

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            # Oldest first, which is the order that made this wrong.
            return types.SimpleNamespace(items=[_pod("older", old), _pod("newer", new)])

        def read_namespaced_pod_log(self, name, namespace, container):
            return f"log of {name}"

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                            lambda cfg, cid: ("b", "p/"))
        monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: _Store())

        assert pj.publish_live_log(_Core(), object(), "camp", "ns", "job-x") is True
    finally:
        monkeypatch.undo()

    assert uploaded["p/_execution/postprocessing.log"].strip() == "log of newer"

def test_each_publish_replaces_rather_than_accumulates(monkeypatch):
    """Called every thirty seconds for the length of a conversion, so anything but a
    replacement would grow the section by a copy of itself each time. The store has no
    append; this asserts the caller relies on that rather than on luck.
    """
    from robovast.execution.cluster_execution import in_pod_storage

    writes = []

    class _Store:
        def upload_file(self, local_path, bucket, key):
            with open(local_path, encoding="utf-8") as f:
                writes.append((key, f.read()))

    class _Core:
        def list_namespaced_pod(self, namespace, label_selector):
            pod = types.SimpleNamespace(
                metadata=types.SimpleNamespace(name="p1", creation_timestamp=None),
                spec=types.SimpleNamespace(
                    init_containers=[], containers=[types.SimpleNamespace(name="host")]))
            return types.SimpleNamespace(items=[pod])

        def read_namespaced_pod_log(self, name, namespace, container):
            return "the whole log so far"

    monkeypatch.setattr(in_pod_storage, "campaign_storage_location",
                        lambda cfg, cid: ("b", "p/"))
    monkeypatch.setattr(in_pod_storage, "storage_client_for", lambda cfg: _Store())

    for _ in range(3):
        pj.publish_live_log(_Core(), object(), "camp", "ns", "job-x")

    # Three writes, to one key, each carrying the log once.
    assert {key for key, _ in writes} == {"p/_execution/postprocessing.log"}
    assert [text.count("the whole log so far") for _key, text in writes] == [1, 1, 1]
