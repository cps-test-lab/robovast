# robovast_sim_roqsim

The [roqsim](https://github.com/cps-test-lab/roqsim) (MuJoCo) simulator backend for RoboVAST.

Registers one `robovast.simulators` entry point, so a campaign selects the simulator by name:

```yaml
execution:
  mode: ros2
  containers:
    simulation:
      backend: roqsim
      config: worlds/depot.yaml
```

Everything else — the image, the command, the GL/record environment, which files the world is
made of — comes from the backend rather than from the `.vast`.

## Installation

It ships as a RoboVAST extra:

```bash
pip install 'robovast[roqsim]'
```

`pip install robovast` deliberately gets you nothing from here: RoboVAST names no simulator, so a
backend is always something you add. The default service/controller image installs this extra,
which is what lets `backend: roqsim` resolve on a cluster without the campaign shipping anything.

### From source

```bash
pip install -e .
```

## Scope

This package must import **without roqsim installed** — it runs in the long-lived RoboVAST
service process, which has no reason to carry a MuJoCo runtime. It declares strings and container
specs; anything that genuinely needs the simulator (such as enumerating the files a world is built
from) runs inside roqsim's own image.
