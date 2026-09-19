# robovast-cluster

**Run your robot software through hundreds of simulated runs at once, on a Kubernetes
cluster.**

[RoboVAST](https://cps-test-lab.github.io/robovast/) tests robotics stacks by running them
through varied, recorded simulation runs. On one machine those runs go one after another; with
`robovast-cluster` the same campaign — the same file, unchanged — becomes jobs spread across a
cluster, as many in parallel as the cluster has room for. Bags, screenshots and logs come back
to one place, and the results are queryable as before.

```bash
pip install robovast robovast-cluster
```

## What it adds

The Kubernetes execution lane, the commands that deploy the RoboVAST service onto a cluster
with its ingress and certificate and keep it running, and ready-made configurations for the
clusters people have: a laptop's minikube, your own machines under RKE2, Google Cloud, Azure.
Object storage, registry and node shapes are settings, not code, and another cluster flavour is
another plugin.

## Is this the package for me?

Yes, if you are the person with the kubeconfig — the one who sets the service up for a team.
The people using it need only [`robovast-client`](https://pypi.org/project/robovast-client/);
they launch campaigns and fetch results without knowing which lane runs them. If you only run
campaigns on your own Docker host, [`robovast`](https://pypi.org/project/robovast/) alone does
that, and says so if you ask it for a cluster.

Documentation: [cps-test-lab.github.io/robovast](https://cps-test-lab.github.io/robovast/),
"Cluster execution" and "Deployment".

## Licence

Apache-2.0.
