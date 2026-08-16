
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
          command: ["/bin/bash", "-c"]
          args:
            - |
              EXPECTED="{compat_version}"
              ACTUAL=$(cat /etc/robovast_compat_version 2>/dev/null || echo "")
              if [ -z "$ACTUAL" ] || [ "$EXPECTED" != "$ACTUAL" ]; then
                echo "ERROR: Compatibility version mismatch!"
                echo "  Host robovast expects compat version: $EXPECTED"
                echo "  Container image provides: ${{ACTUAL:-<missing>}}"
                exit 1
              fi
              echo "Compat version check passed: $ACTUAL"
      containers:
        - name: robovast
          image: {image}
          command: ["/usr/bin/tini", "--", "/bin/bash", "/config/entrypoint.sh"]
          env:
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
