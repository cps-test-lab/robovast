# robovast-cluster

**Run [RoboVAST](https://github.com/cps-test-lab/robovast) campaigns across a Kubernetes
cluster — hundreds of simulation runs in parallel, the same `.vast` you ran on your laptop —
and the operator commands that stand the service up and keep it running.**

```bash
pip install robovast robovast-cluster
vast cluster setup my-cluster.yaml       # deploys the service, its ingress and TLS
vast serve --backend cluster             # or run the service locally against the cluster
```

## What it adds

- **The `cluster` execution lane.** Each run becomes a Kubernetes Job; the campaign
  controller schedules them across the nodes, pulls the pinned images, collects bags,
  screenshots and logs, and publishes the results to the campaign store. Node placement,
  resource requests and per-cluster storage are configuration, not code.
- **`vast cluster setup / cleanup / jobs-cleanup / monitor`** — deploy the service with an
  Ingress and certificate, tear it down, sweep finished Jobs, watch a campaign's Jobs.
- **`vast service upgrade / token`** — roll the deployed service to a new image, mint access
  tokens.
- **Cluster configurations as plugins** — `minikube` for a laptop, `rke2` for your own
  machines, `gcp` and `azure` for the clouds — each knowing its object store (S3-compatible or
  Google Cloud Storage), its registry and its node shapes, with credentials as cluster Secrets.
  Another cluster flavour is another entry point.

Everything a user does — launching campaigns, waiting, fetching results, the web UI, the MCP
endpoint — is unchanged; a campaign does not know which lane it runs on. Users need only
[`robovast-client`](https://pypi.org/project/robovast-client/); this package is for the
operator who has the kubeconfig.

## Where it fits

It is a separate distribution so that `pip install robovast` carries no Kubernetes client:
without it, the service runs the local Docker lane and says so — `vast serve --backend cluster`
names the lanes that are installed, and `vast doctor` reports the cluster lane as absent rather
than broken. It ships into the `robovast` namespace, so import paths do not change.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/),
"Cluster execution" and "Deployment".

## Licence

Apache-2.0.
