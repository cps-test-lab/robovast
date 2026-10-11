# Changelog

What changed in each released version of RoboVAST, newest first. Each section lists the
changes a user must know about; the git history holds the rest.

## 2.2.1

- **Pinned simulator** — the roqsim image builds the roqsim commit its Dockerfile pins, the last this line supports
  ([#900](https://github.com/cps-test-lab/robovast/pull/900))
- **MCP image tag** — `start_campaign` takes `image_project_tag`, as the CLI's `--image-project-tag` does
  ([#709](https://github.com/cps-test-lab/robovast/pull/709))
- **Screenshots kept** — a simulation screenshot is stored and addressable by path, not only returned inline
  ([#708](https://github.com/cps-test-lab/robovast/pull/708))
- **World check** — the project check reports what the simulator warns about a world's start state
  ([#715](https://github.com/cps-test-lab/robovast/pull/715))
- **Exit codes** — `vast campaign wait` and `vast image wait` define their codes once; help, docs and MCP render that list
  ([#719](https://github.com/cps-test-lab/robovast/pull/719))
- **Admission** — a pass is linear in the queue, sizes a queued job once per node, and reads the cluster outside its lock
  ([#699](https://github.com/cps-test-lab/robovast/pull/699), [#881](https://github.com/cps-test-lab/robovast/pull/881))
- **Free disk** — admission takes a node's free disk as the kubelet measures it, less its eviction threshold
  ([#892](https://github.com/cps-test-lab/robovast/pull/892))
- **Postprocessing** — a failed conversion delivers what it converted; an OOM-killed stage step fails its pod instead of holding it
  ([#880](https://github.com/cps-test-lab/robovast/pull/880), [#899](https://github.com/cps-test-lab/robovast/pull/899))
- **Stall report** — a campaign whose batch has finished every run is not called queued
  ([#707](https://github.com/cps-test-lab/robovast/pull/707))
- **Archived campaigns** — read as archived on the exec path too, so a key they ran without does not refuse them
  ([#692](https://github.com/cps-test-lab/robovast/pull/692))
- **roqsim socket** — a ros-shape run names its control socket on `/ipc` for simulator and scenario alike
  ([#894](https://github.com/cps-test-lab/robovast/pull/894))
- **roqsim image** — no longer installs `roqsim_webctrl`, which roqsim does not ship
  ([#898](https://github.com/cps-test-lab/robovast/pull/898))

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
