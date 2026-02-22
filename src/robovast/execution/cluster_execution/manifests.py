
JOB_TEMPLATE = """apiVersion: batch/v1
kind: Job
metadata:
  name: $TEST_ID
  namespace: {namespace}
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
            requests:
              cpu: {cpu}
            limits:
              cpu: {cpu}
"""
