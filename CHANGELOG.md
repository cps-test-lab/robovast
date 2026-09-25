# Changelog

What changed in each released version of RoboVAST, newest first. Each section lists the
changes a user must know about; the git history holds the rest.

## 2.2.0

- **Local Docker lane removed** — campaigns run on a Kubernetes cluster, on one machine a minikube or kind; `vast serve` needs `robovast-cluster`
  ([#675](https://github.com/cps-test-lab/robovast/pull/675))
- **Settings refused** — a `.vast` that sets `execution.local` or `show_gui` is refused, since nothing reads them
  ([#675](https://github.com/cps-test-lab/robovast/pull/675))
- **Navigation MCP tools retired** — core answers them for any robot: `pose_track_view`, `get_track_deviation`, `get_config_contribution`, `draw_config`
  ([#640](https://github.com/cps-test-lab/robovast/pull/640))
- **Pinned deployments** — the admin page offers Upgrade only on a tag CI moves; move a pinned version with `vast service upgrade`
  ([#691](https://github.com/cps-test-lab/robovast/pull/691))
- **Workspace archives** — download, share and re-create a workspace as one archive
  ([#657](https://github.com/cps-test-lab/robovast/pull/657))
- **Simulator docs from the image** — a simulator's documentation and spawnable components are read from the image that runs them
  ([#521](https://github.com/cps-test-lab/robovast/pull/521), [#526](https://github.com/cps-test-lab/robovast/pull/526), [#683](https://github.com/cps-test-lab/robovast/pull/683))
- **Campaign management** — delete several campaigns at once, and sort the list by recency or size
  ([#648](https://github.com/cps-test-lab/robovast/pull/648), [#646](https://github.com/cps-test-lab/robovast/pull/646))
- **Stopping** — a stop takes effect in every wait, and a campaign's auxiliary containers end with it
  ([#616](https://github.com/cps-test-lab/robovast/pull/616), [#618](https://github.com/cps-test-lab/robovast/pull/618))
- **Parallel postprocessing** — a campaign's postprocessing is split across Jobs, per run where a plugin allows it
  ([#601](https://github.com/cps-test-lab/robovast/pull/601))
- **Simulator provenance** — a campaign records which simulator commit it ran, and flags images built from different ones
  ([#660](https://github.com/cps-test-lab/robovast/pull/660))
- **Run view** — a Run-view button on the campaign card, and the costmap panel drawn onto a rendered video
  ([#643](https://github.com/cps-test-lab/robovast/pull/643), [#578](https://github.com/cps-test-lab/robovast/pull/578))
- **TurtleBot 4 in the images** — the roqsim image carries the Create 3 / TurtleBot 4 stack
  ([#633](https://github.com/cps-test-lab/robovast/pull/633), [#664](https://github.com/cps-test-lab/robovast/pull/664))
