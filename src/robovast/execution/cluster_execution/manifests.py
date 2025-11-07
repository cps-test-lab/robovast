
JOB_TEMPLATE = """apiVersion: batch/v1
kind: Job
metadata:
  name: $SCENARIO_ID
  labels:
    jobgroup: scenario-runs
spec:
  backoffLimit: 0
  # activeDeadlineSeconds: 10000000
  template:
    metadata:
      name: scenario-runs
      labels:
        jobgroup: scenario-runs
    spec:
      restartPolicy: Never
      containers:
        - name: test-container
          image: {image}
          # command: ["/bin/bash", "-c", "sleep 1000"]
          securityContext: # required for rke2
            privileged: true
          resources:
            requests:
              cpu: {cpu}
            limits:
              cpu: {cpu}
      volumes: {volumes}
"""
