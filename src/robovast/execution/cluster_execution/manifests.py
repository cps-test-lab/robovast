
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
      containers:
        - name: robovast
          image: {image}
          command: ["/bin/bash", "/config/entrypoint.sh"]
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
