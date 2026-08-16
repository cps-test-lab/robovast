# robovast-cluster

The Kubernetes execution lane for [RoboVAST](https://github.com/cps-test-lab/robovast):
running campaigns as Kubernetes Jobs, deploying the service, and the operator commands
that set a cluster up and take it down.

Install it alongside `robovast` when you have a cluster:

```bash
pip install robovast robovast-cluster
```

Without it, `robovast` runs the local Docker lane and says so — `vast serve --backend
cluster` reports which lanes are installed, and `vast doctor` reports the cluster lane as
absent rather than broken.

It ships into the `robovast` namespace, so nothing about the import paths changes.
