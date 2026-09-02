# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for analysis postprocessing surfacing."""

import logging

import robovast.execution.cluster_execution.postprocess_job as pj


def _patch_failed_conversion(monkeypatch, synced):
    monkeypatch.setattr(pj, "campaign_vast", lambda cr: "/x.vast")
    monkeypatch.setattr(pj, "rosbag_commands_for",
                        lambda vast, skip=None, skip_rosout=False: [{"plugins": []}])
    monkeypatch.setattr(pj, "campaign_execution_image", lambda cr: "img")
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
    """The Job runs the campaign's execution image and mirrors bags with the sidecar, both of
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

    A Job that dies before its first ``tee`` mirrors no log, so the section is never
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
    monkeypatch.setattr(pj, "campaign_vast", lambda cr: "/x.vast")
    monkeypatch.setattr(pj, "rosbag_commands_for",
                        lambda vast, skip=None, skip_rosout=False: [{"plugins": []}])
    monkeypatch.setattr(pj, "campaign_execution_image", lambda cr: "img")
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
    monkeypatch.setattr(pj, "campaign_vast", lambda cr: "/x.vast")
    monkeypatch.setattr(pj, "rosbag_commands_for",
                        lambda vast, skip=None, skip_rosout=False: [{"plugins": []}])
    monkeypatch.setattr(pj, "campaign_execution_image", lambda cr: "img")
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

    def download_prefix(self, bucket, prefix, local_dir, force=False):
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

    assert pj.sync_outputs(object(), 'camp', str(tmp_path), force=True) == 3
    assert store.calls['prefix'] == ('p/_postproc', True)


def test_the_manifest_fetches_exactly_what_it_names(monkeypatch, tmp_path):
    """The point of the manifest: one known key read, then only the objects it names.

    Listing the campaign prefix instead would walk every rosbag to find the CSVs beside
    them, which is the cost the duplicated staging prefix existed to avoid.
    """
    store = _FakeStore(
        manifest=b'c1/r1/out.csv\n_execution/postprocessing.log\n',
        objects={'p/c1/r1/out.csv': b'x,y\n', 'p/_execution/postprocessing.log': b'ok\n'})
    _patch_store(monkeypatch, store)

    assert pj.sync_outputs(object(), 'camp', str(tmp_path)) == 2
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

    assert pj.sync_outputs(object(), 'camp', str(tmp_path)) == 0
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
    nothing tees a line and the end-of-run mirror that would carry one never runs. Staging
    also pulls every bag onto the node, so it is the step that meets a full disk first --
    the failure most in need of an explanation had none.
    """
    script = pj._staging_script()

    assert pj.STAGING_LOG in script
    # The push is best-effort and must not become the reported status: if the store is what
    # is unreachable, the upload cannot work either.
    assert '|| true' in script
    assert 'exit "$rc"' in script


def test_the_conversion_mirrors_to_the_canonical_prefix_with_a_manifest():
    """Outputs go to the paths they are read from, and the mirror carries the index of
    them, so a staging copy is no longer the price of a cheap fetch."""
    script = pj._conversion_script([{"plugins": []}], force=False)

    assert '"mystore/$S3_BUCKET/$S3_CAMPAIGN_PREFIX"' in script
    assert pj.POSTPROC_PREFIX not in script
    # In the trap with the mirror: an index that is not written whenever the outputs are
    # would leave the service unable to find what did get uploaded.
    assert pj.OUTPUT_MANIFEST in script
    trap = next(line for line in script.splitlines() if line.startswith("trap "))
    assert pj.OUTPUT_MANIFEST in trap and "mc mirror" in trap


def test_a_failed_conversion_still_writes_a_postprocessing_section(monkeypatch, tmp_path):
    """The phase file IS the section: every surface assembles the campaign log from the
    files that exist, so a conversion that wrote nothing left the phases stopping after RUN
    with the failure reported only in a status field elsewhere.
    """
    from robovast.execution.cluster_execution import in_pod_storage
    monkeypatch.setattr(in_pod_storage, 'campaign_storage_location',
                        lambda cfg, cid: ('b', 'p/'))
    monkeypatch.setattr(in_pod_storage, 'storage_client_for',
                        lambda cfg: type('S', (), {'read_object': lambda *a: None})())

    log = tmp_path / '_execution' / 'postprocessing.log'
    pj._write_failure_log(object(), 'camp', str(tmp_path), str(log),
                          pj.job_failed_message('job-x'))
    text = log.read_text()

    assert 'job-x' in text
    # The slot is decided by whether this file exists, so inside it there is nothing for it
    # to say -- and it must never reach a reader.
    assert pj.POINTER_SLOT not in text
    # No staging log is a finding, not missing information: a SIGKILLed container cannot
    # file one, and staging is what meets a full disk first.
    assert 'disk' in text.lower()


def test_the_staging_log_is_carried_into_the_section_when_there_is_one(monkeypatch, tmp_path):
    """When staging did survive to report, its account is what the reader came for."""
    from robovast.execution.cluster_execution import in_pod_storage
    monkeypatch.setattr(in_pod_storage, 'campaign_storage_location',
                        lambda cfg, cid: ('b', 'p/'))
    monkeypatch.setattr(
        in_pod_storage, 'storage_client_for',
        lambda cfg: type('S', (), {
            'read_object': lambda *a: b'mc: <ERROR> no space left on device\n'})())

    log = tmp_path / '_execution' / 'postprocessing.log'
    pj._write_failure_log(object(), 'camp', str(tmp_path), str(log),
                          pj.job_failed_message('job-x'))

    assert 'no space left on device' in log.read_text()
    assert 'no space left on device' in (tmp_path / pj.STAGING_LOG).read_text()


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
    core = _core([_pod(init=[_CS('s3-init', exit_code=1)],
                       main=[_CS('convert')])])

    assert pj.pod_failure_reason(core, 'ns', 'job-x') == 'container s3-init exited 1'


def test_an_unreadable_pod_list_never_becomes_the_failure():
    """This runs while reporting a failure, so it must not raise one of its own."""
    class _Broken:
        def list_namespaced_pod(self, namespace, label_selector):
            raise RuntimeError('no api')

    assert pj.pod_failure_reason(_Broken(), 'ns', 'job-x') == ''
    # And the message is still complete without it.
    assert 'job-x' in pj.job_failed_message('job-x', pod_reason='')
