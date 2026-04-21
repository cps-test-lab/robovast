================
Execution Modes
================

RoboVAST supports two execution modes that control how the full matrix of
(config × run) combinations is distributed across containers or cluster jobs.
The mode is selected via the ``execution.mode`` field in the ``.vast``
configuration file (see :doc:`configuration`).

Overview
--------

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Mode
     - Local (Docker Compose)
     - Cluster (Kubernetes)
   * - ``one_job_per_run`` *(default)*
     - One container per (config, run) pair, run sequentially
     - One Kubernetes Job per (config, run) pair
   * - ``fixed_jobs``
     - One container executing all variants
     - N Jobs, each handling a round-robin slice of all variants

one_job_per_run (default)
--------------------------

Each (config, run) pair is executed in its own isolated container.

* **Local:** Containers are launched sequentially via Docker Compose.
* **Cluster:** One Kubernetes Job is submitted per pair; Kueue schedules
  them onto available nodes.

No extra configuration fields are required beyond the standard
``execution`` section.

.. code-block:: yaml

   execution:
     runs: 5
     image: ghcr.io/cps-test-lab/robovast:latest
     resources:
       cpu: 2
     scenario_file: scenario.osc

fixed_jobs
----------

All (config × run) variants are batched into N containers.  Each container
receives a *multi-document* scenario parameter file
(``job{id}_scenario.configs``) that covers its round-robin slice of the
variant matrix.  scenario-execution's ``--output-result-per-scenario`` flag
ensures that each variant's output lands in the correct
``<config>/<run>`` subdirectory of the campaign root.

This mode is well-suited to large campaigns where the per-job Kubernetes
overhead of ``one_job_per_run`` is significant, or when you want to fill
the cluster with a small number of long-running containers.

Configuration requirements
~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``execution.mode: fixed_jobs`` — selects this mode.
* ``execution.simulation`` — **required**; the simulator backend name
  (e.g. ``gz-headless``) forwarded to scenario-execution via
  ``--simulation``.

.. code-block:: yaml

   execution:
     runs: 20
     mode: fixed_jobs
     simulation: gz-headless
     image: ghcr.io/cps-test-lab/robovast:latest
     resources:
       cpu: 4
     scenario_file: scenario.osc

Number of jobs (cluster)
~~~~~~~~~~~~~~~~~~~~~~~~~

On cluster, RoboVAST queries the Kueue ``robovast`` ClusterQueue at
submission time to determine the total CPU quota.  The number of jobs is
then:

.. math::

   N = \left\lfloor \frac{\text{Kueue CPU quota}}{\text{execution.resources.cpu}} \right\rfloor

If the Kueue API is unreachable, RoboVAST falls back to ``N = 1``.

Variant distribution
~~~~~~~~~~~~~~~~~~~~~

Variants are distributed round-robin.  With ``runs = 3``,
``configs = [A, B]``, and ``N = 2`` jobs:

.. list-table::
   :header-rows: 1
   :widths: 10 45 45

   * - Job
     - Variants
     - Notes
   * - job 0
     - (run 0, A), (run 0, B), (run 2, A), (run 2, B)
     - even-indexed variants
   * - job 1
     - (run 1, A), (run 1, B)
     - odd-indexed variants

Output directory layout
~~~~~~~~~~~~~~~~~~~~~~~~

Regardless of execution mode, the output directory structure is identical:

.. code-block::

   <campaign>/
   ├── config1/
   │   ├── 0/   ← run 0 output
   │   └── 1/   ← run 1 output
   └── config2/
       ├── 0/
       └── 1/

In ``fixed_jobs`` mode, scenario-execution's ``_output_dir`` meta-key
(one per scenario document in the multi-doc parameter file) is used to
route each variant's output to its correct subdirectory.
