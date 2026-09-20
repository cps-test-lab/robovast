
#: The name of the pod's one REGULAR container -- the ``scenario`` role. Every other
#: container in a scenario pod is a native sidecar named for the role that declared it
#: (``sut``, ``simulation``, an ad-hoc key), so this is the single name that does not
#: appear in a ``.vast``. Must match the template below; ``test_job_manifest`` pins it.
MAIN_CONTAINER_NAME = "robovast"

#: Which KIND of ``jobgroup: scenario-runs`` Job this is. Absent means the campaign's own
#: work -- what every Job the template below produces is -- and the one other value is a
#: node-calibration probe.
#:
#: A second label rather than a second ``jobgroup``: a probe holds real capacity on a real
#: node and is torn down with the campaign, so it has to stay inside every selector that
#: counts or cleans up ``scenario-runs``. What it must not be is one of the campaign's
#: trials, and that is the single distinction this label carries.
JOB_KIND_LABEL = "job-kind"
#: The label's value for a calibration probe. Deliberately the same string as
#: ``JobKind.CALIBRATION`` puts on the wire -- one vocabulary, not two that can drift --
#: which ``test_the_wire_kind_is_the_cluster_label`` pins.
CALIBRATION_JOB_KIND = "calibration"

#: How long a finished scenario Job stays readable. Short: the batch loop reads each Job as
#: it finishes and its results are delivered before the pod completes.
SCENARIO_JOB_TTL_SECONDS = 60

#: The scenario pod's spec, which :func:`~.campaign_job.campaign_job_manifest` wraps in the
#: Job every admitted campaign Job shares. ``{image}`` and ``{pull_policy}`` are filled on
#: load; the per-job values are stamped on each Job built from it.
POD_TEMPLATE = """containers:
  - name: robovast
    image: {image}
    imagePullPolicy: {pull_policy}
    command: ["/usr/bin/tini", "--", "/bin/bash", "/config/entrypoint.sh"]
    env:
    # Which machine ran this trial. The downward API is the only source: a pod
    # cannot see its own node otherwise, and ``instance_type`` does not answer it
    # on bare metal, where the provider command is ``uname -m`` and every node
    # reports the same architecture. Without it, runs from a heterogeneous cluster
    # cannot be grouped by the hardware they ran on, so a slower node reads as
    # run-to-run variance.
    - name: NODE_NAME
      valueFrom:
        fieldRef:
          fieldPath: spec.nodeName
    - name: AVAILABLE_CPUS
      valueFrom:
        resourceFieldRef:
          resource: limits.cpu
    - name: AVAILABLE_MEM
      valueFrom:
        resourceFieldRef:
          resource: limits.memory
    resources:
      requests: {{}}
      limits: {{}}
"""
