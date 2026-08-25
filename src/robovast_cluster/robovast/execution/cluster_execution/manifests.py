
#: The name of the pod's one REGULAR container -- the ``scenario`` role. Every other
#: container in a scenario pod is a native sidecar named for the role that declared it
#: (``sut``, ``simulation``, an ad-hoc key), so this is the single name that does not
#: appear in a ``.vast``. Must match the template below; ``test_job_manifest`` pins it.
MAIN_CONTAINER_NAME = "robovast"

JOB_TEMPLATE = """apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB_NAME
  namespace: {namespace}
  labels:
    jobgroup: scenario-runs
    campaign-id: $CAMPAIGN_ID
  annotations:
    total-job-num: "$TOTAL_JOB_NUM"
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 60
  template:
    metadata:
      name: scenario-runs
      labels:
        jobgroup: scenario-runs
        campaign-id: $CAMPAIGN_ID
      annotations:
        job-name-full: $JOB_FULL_NAME
    spec:
      restartPolicy: Never
      initContainers:
        - name: compat-check
          image: {image}
          imagePullPolicy: {pull_policy}
          command: ["/bin/bash", "-c"]
          args:
            - |
              # Runs INSIDE the image, so the label is not reachable here -- `docker inspect`
              # is a host-side call and a pod has no daemon socket. The file is the right
              # marker for this one site; the host-side checks prefer the label.
              MAX="{compat_version}"
              MIN="{min_compat_version}"
              ACTUAL=$(cat /etc/robovast_compat_version 2>/dev/null || echo "")
              # A RANGE, not equality: this pod may be running an image a year-old campaign
              # recorded, and equality refused every one of those after the first bump.
              if [ -z "$ACTUAL" ]; then
                echo "ERROR: this image reports no container protocol version"
                echo "  (/etc/robovast_compat_version is missing); host speaks $MIN..$MAX."
                exit 1
              elif [ "$ACTUAL" -gt "$MAX" ] || [ "$ACTUAL" -lt "$MIN" ]; then
                echo "ERROR: image speaks container protocol $ACTUAL, host speaks $MIN..$MAX."
                if [ "$ACTUAL" -gt "$MAX" ]; then
                  echo "  The image is NEWER than this robovast -- upgrade robovast."
                else
                  echo "  Check out the robovast revision the campaign recorded"
                  echo "  (_execution/execution.yaml: robovast_revision) and run it there."
                fi
                exit 1
              fi
              echo "Container protocol check passed: $ACTUAL (host speaks $MIN..$MAX)"
      containers:
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
