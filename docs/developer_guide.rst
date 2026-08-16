.. _devguide:

Developer Guide
===============

Test your Robotic Software with RoboVAST
----------------------------------------

RoboVAST is designed to facilitate testing and validation of robotic software systems by generating diverse scenarios and executing them in simulation environments. This guide provides an overview of how to utilize RoboVAST for testing your robotic applications.

1. Containerize your Software
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

As RoboVAST relies on containerization to ensure consistent and reproducible environments, the first step is to create a Docker container for your robotic software.

There are some requirements your container image must fulfill to be compatible with RoboVAST:
- the image must contain scenario-execution package installed in `/ws/install` (which currently is available for ROS2 jazzy)
- the image must be accessible by Kubernetes, e.g. by pushing it to a container registry.

2. Define a Test Scenario
^^^^^^^^^^^^^^^^^^^^^^^^^

Use the examples and the documentation of `scenario-execution <https://cps-test-lab.github.io/scenario-execution/>`_ to create a scenario that tests your robotic software.

Keep in mind, that variations are currently supported for all overwritable scenario parameters as described `here <https://cps-test-lab.github.io/scenario-execution/how_to_run.html#override-scenario-parameters>`_.

To test your scenario locally, you can run:

.. code-block:: bash

    ros2 run scenario_execution_ros scenario_execution_ros <scenario-file> -t -d

3. Create Initial RoboVAST Configuration
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Create a RoboVAST configuration file, based on the existing examples in the `configs/` directory.
Do not set any configuration, as this will be done in the next step.


.. code-block:: bash

    vast exec local prepare-run --config config1 ./test_run

Afterwards you can verify the scenario, the RoboVAST-configuration and the docker image.

.. code-block:: bash

    # execute a basic run
    ./test_run/run.sh

    # use different container image
    ./test_run/run.sh --image <your-container-image>

    # analyze issues by using an interactive shell
    ./test_run/run.sh --start-only

    # check that a standalone non-GUI environment (like in Kubernetes) works
    ./test_run/run.sh --no-gui

    # enable extra scenario-execution output: live py-tree (-t) and debug log (-d)
    ./test_run/run.sh -t -d

To enable GUI visualization (RViz, a simulator window) for local runs while keeping cluster
runs headless, add ``execution.local.gui.parameter_overrides`` to your ``.vast`` (see
:doc:`configuration`). ``vast execution local run`` opts in by default; through a service
the equivalent is ``start_campaign(show_gui=True)`` or ``exec_in_container(show_gui=True)``.

Either way the window opens on the host running the **service** (or, for the CLI, on the
host you ran it from), whose ``DISPLAY`` the containers inherit.

The two entry points differ on purpose when there is no display:

* ``show_gui=True`` is a **request**, so it is refused — a ``vast serve`` started outside a
  desktop session or reached over an SSH tunnel has no screen to draw on, and a cluster
  backend refuses it outright. Accepting it would produce a run that looks fine and shows
  nothing.
* ``vast execution local run`` has GUI as its **default**, so it prints a notice and
  continues headless. A build machine must keep running it unattended without passing
  ``--no-gui``.

Next, it is important to verify that the output (e.g. ROS bag) is stored correctly. 

.. code-block:: bash

    vast exec local run --config config1 ./test_out

    # check that output is created in ./test_out/<campaign-name>-<timestamp>/<config-name>/<run_number>
    ls -l ./test_out/*-*/config1/0/

Once you are satisfied that the scenario and configuration work as expected, you can proceed to the next step.

4. Define Configurations
^^^^^^^^^^^^^^^^^^^^^^^^

Define configurations in your ``.vast`` file.
A good procedure is to add configurations one-by-one and analyze the result.

.. code-block:: bash

    # 1. add configuration in config file

    # 2. list created configurations
    vast config list

    # 3. try local execution with one of the created configurations
    vast exec local run --config <config-name> --runs 1 ./test_out

5. Execute in Cluster
^^^^^^^^^^^^^^^^^^^^^

Once you have defined your configurations and verified local execution, you can run the tests in a Kubernetes cluster.

A good practice is, to first run a single configuration to verify that everything works as expected. 


.. code-block:: bash

    # 1. run single configuration in cluster, once
    vast exec cluster run --config config1 --runs 1

    # 2. upload results to share service (or use download-cleanup to just remove S3 buckets)
    vast exec cluster upload-to-share
    # Results can then be retrieved with: vast results download
    # Files are organized as: <results-dir>/<campaign-name>-<timestamp>/<config-name>/<run_number>/

``vast exec cluster run`` is fire-and-forget: it starts the campaign on the
``robovast-service``, which drives it in-process, and returns immediately. The
campaign runs in the background in the cluster:

.. code-block:: bash

    # Monitor job status (shows progress per run when multiple runs are active)
    vast exec cluster monitor

    # Clean up after jobs complete (all campaigns, or use --campaign for a specific campaign)
    vast exec cluster run-cleanup

By default, a new run does not clean up previous runs, so you can run multiple
runs in parallel. Use ``--cleanup`` to remove previous runs before starting
(e.g. ``vast exec cluster run --cleanup``).

Running local container images in minikube
"""""""""""""""""""""""""""""""""""""""""""

To test local container images in a minikube cluster, you can load the image into minikube's Docker environment.

.. code-block:: bash

    # first terminal
    docker run --rm -it --network=host alpine ash -c "apk add socat && socat TCP-LISTEN:5000,reuseaddr,fork TCP:$(minikube ip):5000"

    # second terminal
    ./container/build.sh --push

    # specify the image in your RoboVAST configuration file


6. Analysis
^^^^^^^^^^^

RoboVAST provides a GUI for analyzing run results, which is based on user-provided Jupyter notebooks.

To develop the notebooks, it is recommended to use e.g. VSCode. For the RoboVAST GUI to work, it is expected to contain a ``DATA_DIR`` definition. The RoboVAST GUI will replace this line with the actual path to the results directory. During development you can set this variable manually to point to your results directory.

.. code-block:: python

    # for single-run (specific run of a configuration)
    DATA_DIR = '<path-to-your-results-directory>/<campaign-name>-<timestamp>/<config-name>/<run_number>'
    # for configuration (all configurations)
    DATA_DIR = '<path-to-your-results-directory>/<campaign-name>-<timestamp>/<config-name>'
    # for complete run
    DATA_DIR = '<path-to-your-results-directory>/<campaign-name>-<timestamp>'

In case you are using ROS bags as output format, it is recommended to postprocess the results before analysis. This can be done with the postprocessing commands defined in the configuration file. RoboVAST provides several conversion scripts for common use-cases.

Postprocessing is cached based on the results directory hash. To bypass the cache and force postprocessing (e.g., after updating postprocessing scripts), use the ``--force`` or ``-f`` flag:

Afterwards you can start the GUI:

.. code-block:: bash

    vast results postprocess
    # or, to force postprocessing even if results are unchanged:
    vast results postprocess --force
    vast evaluation gui

.. note::

   The GUI discovers campaigns **exclusively from a per-campaign
   ``campaign.db`` store** — it does not walk the results filesystem. Search
   campaigns write this store live; batch campaigns are indexed post-hoc from
   their results tree. ``vast evaluation gui`` indexes any missing batch stores
   automatically before launching, but you can also (re)build them explicitly:

   .. code-block:: bash

       vast evaluation index            # build/refresh campaign stores
       vast evaluation index --force    # rebuild even if up to date

   The store also carries the campaign **mode** (``batch``/``search``), so the
   GUI renders the search ``batch`` level and resolves the
   ``evaluation.visualization`` notebooks from the recorded ``config_dir``. See
   :ref:`campaign-store` for the schema and internals.


.. _devguide-distributions:

Working across the distributions
--------------------------------

RoboVAST is four packages in one checkout (see :ref:`architecture-distributions`), which
changes two things about the development loop. Both have bitten; both are silent.

**Install the client last.** ``robovast-client`` is a *non-optional path dependency* of
``robovast``, so ``pip install -e .`` resolves it and installs a plain **copy** into
``site-packages`` — silently replacing an editable install done earlier. Editing
``src/robovast_client/`` then has no effect, and nothing says so. ``make venv`` installs it
after everything that depends on it for exactly this reason; if you install by hand, do
the same, and check with:

.. code-block:: bash

   python -c "import robovast.client.cli as m; print(m.__file__)"

A path under ``src/robovast_client/`` is editable; one under ``site-packages`` is not.

**Entry points live in installed metadata, not in ``pyproject.toml``.** Adding, moving or
removing one does nothing until the owning distribution is reinstalled, and an editable
checkout looks entirely normal in the meantime. The failure mode depends on which group:

* ``robovast.cli_plugins`` degrades **loudly** — ``load_plugins()`` prints
  ``Warning: Failed to load plugin '<name>'`` and carries on.
* ``robovast.cli_startup`` used to degrade **silently**, and cost real damage: with the
  core installed but its ``.env`` hook unregistered, no ``./.env`` was read, and
  ``vast exec cluster upgrade`` — which reconciles Secrets from the environment —
  concluded the registry and git credentials were gone and deleted both. It now refuses
  rather than running on, naming the reinstall.

The rule that follows: after touching any ``[tool.poetry.plugins."..."]`` block, reinstall
before you conclude anything from a test run. ``make venv`` re-runs when a manifest *or
the Makefile* changes, so it is the safe way to do it.

**A client install must stay a working install.** ``robovast-client`` ships without the
core, and every leak found so far has been a *deferred* import of it — the module imports
perfectly and the command dies at call time, in exactly the install the distribution
advertises. An import check cannot see that;
``tests/service/test_client_needs_no_core.py`` drives the commands with the core made
un-importable. Anything the client needs must live in the client: a wire constant like
``COMMAND_LIMIT_S`` belongs in ``interface.py``, not in the server module that enforces it.


Container Image Compatibility Version
-------------------------------------

RoboVAST enforces a compatibility version between the host Python code and the
Docker container image.  This prevents cryptic runtime failures when the two
sides are out of sync (e.g. after updating one without the other).

How it works
^^^^^^^^^^^^

A single integer ``COMPAT_VERSION`` is defined in
``src/robovast/common/execution.py``.  The same value is baked into the
container image as the file ``/etc/robovast_compat_version``.

Before any container starts, the version is checked by reading
``/etc/robovast_compat_version`` from inside the container:

- **Local execution**: the generated ``run.sh`` script checks the file
  before ``docker-compose up``.
- **Cluster execution**: a Kubernetes init container reads the file and
  compares it to the expected value.
- **Postprocessing**: ``docker_exec.sh`` checks the file before
  ``docker run``.

If the versions do not match (or the file is missing), execution fails
immediately with a clear error message.

When to bump the version
^^^^^^^^^^^^^^^^^^^^^^^^

Bump ``COMPAT_VERSION`` when the contract between host scripts and the
container changes:

- A new Python or system package is required inside the container
- The ROS distribution changes
- The interface of mounted scripts changes (e.g. ``ros2_exec.sh``,
  ``entrypoint.sh``)
- A postprocessing script requires a new ROS package

How to bump the version
^^^^^^^^^^^^^^^^^^^^^^^

1. Increment ``COMPAT_VERSION`` in ``src/robovast/common/execution.py``
2. Update the ``LABEL`` and ``RUN echo`` lines in
   ``container/robovast/Dockerfile`` to match
3. Rebuild and push the container image

The CI workflow (``image.yml``) validates that all three values are in sync
before building the image.


Extending RoboVAST
------------------

.. _extending-variation:

Add Variation Plugin
^^^^^^^^^^^^^^^^^^^^

Provide your custom variation type by creating a class that inherits from `robovast.common.variation.Variation`.

To your `pyproject.toml`, add an entry under `[tool.poetry.plugins."robovast.variation_types"]` to register your variation type. The key is the name used in the RoboVAST configuration file, and the value is the import path to your variation class.

.. code-block:: toml

    [tool.poetry.plugins."robovast.variation_types"]
    "YourVariation" = "robovast_<yourplugin>.your_variation:YourVariation"

A variation can also be loaded from a **local file relative to the .vast**
without packaging it — reference it as ``<path>.py:<Class>`` wherever a variation
name is expected (in a ``configuration[].variations`` list or a ``search.variations``
template). This is the same ``./path.py:Class`` convention used by search
strategies, extractors and postprocessing plugins:

.. code-block:: yaml

    variations:
    - variations/wind.py:WindFieldVariation:
        wind_speed: 5.0

See ``configs/examples/quadrotor_landing/variations/wind.py`` for a runnable
example (a wind model that derives the simulator's ``wind_strength``), wired into
the quadrotor search vasts.

.. note::

   **Packaging a variation plugin as its own distribution.** If your variation
   types live in a separate installable package (as ``robovast-nav`` does for
   ``FloorplanVariation``, ``PathVariation*``, ``ObstacleVariation*``), it must
   be importable everywhere scenario variations get **composed** — not just where
   scenarios *run*. Composition happens in the process that expands the
   ``variations:`` cross-product: the ``vast`` CLI on your host for
   ``vast exec local run``; and the ``robovast-service`` for every service/cluster
   campaign — the service drives the campaign in-process, so composition (which for
   search runs once per generation) happens in the service, not in a separate pod.

   There is **one mechanism** for all of these — the ``.vast``'s ``plugins:`` list
   (next section) — and it is uniform across the CLI and the MCP/service paths:

   * **On your host (``vast`` CLI), a plugin you have already installed is
     detected and used as-is.** ``config_plugins.ensure_workspace_plugins`` checks
     whether each declared distribution is importable; if you ran
     ``pip install`` / ``make venv`` yourself, nothing is re-fetched. Only the
     declared specs that are *missing* are installed — into the project's
     ``.robovast_plugins/`` directory via ``pip install --target`` (never your
     active venv).
   * **For a service/cluster campaign, the service installs each declared plugin
     into the workspace's ``.robovast_plugins/`` and imports it off ``sys.path``**
     when it composes (``config_plugins.ensure_workspace_plugins``). The install
     runs once, on the credentialed service, so a private-repo clone needs
     credentials only there; the driver then uses the installed plugin directly —
     there is no staging round-trip through the object store and no separate pod.

   Dependencies come from each package's own metadata into that same directory, so
   the one real constraint is that your plugin must **declare its dependencies
   correctly** (e.g. ``scenario_mt``'s ``shapely`` and its ``fpm @ git+…``). There
   is no separate host-venv "injection" step and no wheel-shipping; a plugin
   installed only in your host venv but **not** declared in ``plugins:`` will not
   reach the service.

   **Declaring a plugin in the ``.vast`` (CLI, MCP, and ``robovast-service``).**
   Declare the plugin **inside the ``.vast``** with a top-level ``plugins:`` list of
   pip requirement specs. This is the single portable mechanism: it works for the
   CLI (where you may instead just install the plugin yourself — it is detected) and
   for an MCP/service campaign, where the LLM only authors inputs into a workspace
   and ``.vast`` / ``.osc`` are the only file types it can write inline::

       plugins:
         - scenario_mt @ git+https://github.com/secorolab/metamorphic_testing@main
         - some_published_plugin==1.2.3
         - ./plugins/my_plugin-1.0.0-py3-none-any.whl   # a wheel you uploaded

   These are **installed into the workspace**. Before variation types are resolved
   from entry points, ``config_plugins.ensure_workspace_plugins`` (called once at
   the top of ``generate_scenario_variations`` — the sole convergence for local and
   service composition) installs the declared specs into
   ``<workspace>/.robovast_plugins/`` with ``pip install --target`` (**with
   dependencies**) and puts that directory on ``sys.path`` so the entry points
   resolve. A ``.installed`` marker (a hash of the specs) makes it idempotent.

   The key property: ``.robovast_plugins/`` lives in the workspace the driver
   composes from, so the plugin is imported straight off ``sys.path`` with no
   staging step. The install runs where the workspace lives:

   * the ``robovast-service`` runs it in ``create_campaign`` (and for
     validate/preview) — its environment is what reaches the source and holds any
     git credentials;
   * on your host (``vast`` CLI), a declared plugin **already installed** in the
     active venv is *detected and used as-is* — install it yourself and it is not
     re-fetched. Only missing specs are installed, and always into
     ``.robovast_plugins/`` (never your site-packages), so a run is **non-invasive**.

   **Sources.** An index pin needs no source access. A git URL works when the
   install environment can reach it — for a **private** repo in the service, provide
   a GitHub token at ``vast exec cluster setup`` (below). A **workspace-relative
   wheel** you ``create_upload``\ ed is the fully offline path (no git, no
   credentials) for a private or unpublished plugin. On a failed install the error
   is raised synchronously from the service call (create/validate/preview), so an
   MCP user sees an actionable message instead of an ``Unknown variation class``
   surfacing later on the campaign's status.

   Because the ``robovast-service`` is one long-lived process serving many
   workspaces and Python cannot un-import, a plugin already loaded (from another
   workspace) cannot be swapped; ``ensure_workspace_plugins`` **logs a warning** in
   that case (the already-loaded version wins until the service restarts).

   **Private-repo credentials (``vast exec cluster setup``).** When a ``git+https``
   plugin points at a private repo, provide a token via ``ROBOVAST_GIT_TOKEN`` (or
   ``GITHUB_TOKEN`` / ``GH_TOKEN``) — either exported in the environment or, more
   conveniently, set in the project's ``.env`` file (every ``vast`` command loads
   ``./.env`` before it runs). ``deploy_service``
   stores it in a ``robovast-git-credentials`` Secret and **mounts it read-only as a
   file** into the service pod (``/var/run/secrets/robovast-git/token``). The token is
   handled so it is **not accessible to any workspace or command**:

   * it is **never** put in an environment variable (which every child process /
     command would inherit), **never** written to ``~/.gitconfig``, and **never**
     placed on a command line (``ps``-visible);
   * it is supplied to ``git`` only for the one ``pip install`` subprocess, via a
     throwaway ``GIT_ASKPASS`` helper in an owner-only temp dir that is removed
     immediately after;
   * it never enters a workspace or a campaign's inputs (the driver imports the
     already-installed ``.robovast_plugins/`` from the workspace and needs no
     credentials at composition time).

   (Because variation composition runs in-process, an operator who needs hard
   isolation from untrusted plugin code should prefer the uploaded-wheel source,
   which needs no credential at all.)

   For a single dependency-free variation you can skip packaging entirely and use a
   ``<path>.py:<Class>`` file reference resolved relative to the ``.vast`` directory
   (see below).

   Two pitfalls when exposing the package as a poetry extra of *this* repo (e.g.
   ``nav = ["robovast-nav"]``) rather than as an independent third-party plugin:

   * The extra's package name must also be declared as an optional dependency
     in ``[tool.poetry.dependencies]`` (e.g.
     ``robovast-nav = {path = "src/robovast_nav", optional = true}`` for an
     in-repo sibling package) — ``poetry check`` catches the mismatch if not.
   * For a cluster run, the built-in ``robovast`` / ``robovast-nav`` code comes
     from the **service image** (``vast exec cluster setup`` deploys it), so dev
     changes to those sources reach a run by rebuilding/redeploying that image —
     point ``ROBOVAST_CONTROLLER_IMAGE`` at a dev image to iterate. This applies
     only to the built-in sources; independent plugins are handled by
     ``discover_plugin_installs`` above (installed into the workspace, no image
     rebuild needed).

   If your plugin's package pulls in a dependency that itself needs system
   shared libraries (e.g. ``robovast-nav`` hard-depends on
   ``pyside6-essentials``, whose bundled Qt6 libs need ``libGL.so.1`` and
   friends to even *import*, regardless of whether any GUI is ever shown), the
   controller image needs those apt packages too — see the
   ``container/controller/Dockerfile`` apt-get block for the list verified
   against ``robovast-nav``. A missing system lib shows up the same way as a
   missing extra: the plugin's entry point fails to load and the variation
   type is reported as unknown.


Add Command-line Plugin
^^^^^^^^^^^^^^^^^^^^^^^

To create a plugin for the `vast` CLI:

1. Create a Click group or command in your package
2. Register it in your `pyproject.toml` under `[tool.poetry.plugins."robovast.cli_plugins"]`
3. The plugin will be automatically discovered and added to the `vast` command

Example plugin registration:

.. code-block:: toml

    [tool.poetry.plugins."vast.plugins"]
    variation = "variation_utils.cli:variation"


.. _extending-metadata-processing:

Add Metadata Processing Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Metadata processing plugins run after the generic and variation-plugin metadata
phases and can modify the ``metadata.yaml`` produced for each campaign.  They are
configured in the ``.vast`` file under ``results_processing.metadata_processing``:

.. code-block:: yaml

   results_processing:
     metadata_processing:
       - my_metadata_plugin
       - my_metadata_plugin:
           param1: value1
           param2: value2

Each plugin must subclass ``robovast.common.metadata.MetadataProcessor`` and
implement the ``process_metadata`` method:

.. code-block:: python

   from pathlib import Path
   from robovast.common.metadata import MetadataProcessor

   class MyMetadataPlugin(MetadataProcessor):

       def process_metadata(self, metadata: dict, campaign_dir: Path) -> dict:
           # Modify metadata as needed
           metadata["custom_field"] = "custom_value"
           return metadata

Register the plugin in your package's ``pyproject.toml``:

.. code-block:: toml

   [tool.poetry.plugins."robovast.metadata_processing"]
   my_metadata_plugin = "my_package.metadata:MyMetadataPlugin"


.. _extending-variation-metadata:

Add Variation Plugin Metadata Hook
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The ``Variation`` base class defines an overridable classmethod that returns an
empty dict by default.  Subclasses implement it to attach domain-specific metadata
to each configuration entry in ``metadata.yaml``:

.. code-block:: python

   from pathlib import Path
   import yaml
   from robovast.common.variation import Variation

   class MyVariation(Variation):

       @classmethod
       def collect_config_metadata(cls, config_entry, config_dir: Path,
                                    campaign_dir: Path) -> dict:
           """Load extra metadata from a YAML sidecar in _config/."""
           data_file = config_dir / "_config" / "my_data.yaml"
           if data_file.exists():
               with open(data_file) as f:
                   return {"my_data": yaml.safe_load(f)}
           return {}

``collect_config_metadata`` is called once per configuration that used the
variation and returns a dictionary that is merged into the configuration's
metadata entry.


.. _extending-prov-metadata:

Add PROV-O Provenance Hook to a Variation Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Variation plugins can contribute domain-specific nodes to the campaign's
PROV-O provenance graph by overriding ``collect_prov_metadata`` on the
``Variation`` base class.  The default implementation returns ``None``
(no contribution).

This hook is the right place for provenance that is tightly coupled to a
specific variation — for example, a floorplan generation variation knows
which map and mesh files it produced and can declare their lineage in the
graph.

**Return type:** ``ProvContribution`` (or ``None`` to contribute nothing):

.. code-block:: python

   from robovast.common.variation import Variation, ProvContribution

   class MyVariation(Variation):

       @classmethod
       def collect_prov_metadata(
           cls,
           config_entry: dict,
           campaign_namespace,   # rdflib.Namespace for the campaign
           config_namespace,     # rdflib.Namespace for this config
           gen_activity_id: str, # IRI of the config-generation activity
           vast_id: str, # IRI of the vast file that contains it
       ):
           """Contribute domain-specific PROV-O nodes."""
           from rdflib import PROV, Namespace

           _ID, _TYPE = "@id", "@type"
           MY_NS = Namespace("https://example.org/metamodels/")

           config_cfg = config_entry.get("config", {})
           my_file = config_cfg.get("my_output_file", "")
           if not my_file:
               return None

           file_iri = config_namespace[my_file]

           return ProvContribution(
               # Extra graph nodes (entities, activities) appended to @graph
               graph_nodes=[{
                   _ID: file_iri,
                   _TYPE: PROV["Entity"],
                   "wasGeneratedBy": gen_activity_id,
                   MY_NS["someProperty"]: "value",
               }],
               # Properties merged onto the concrete scenario node
               scenario_properties={MY_NS["outputCount"]: 1},
               # IRIs that each run activity should declare as "used"
               run_used_iris=[file_iri],
           )

``ProvContribution`` fields:

``graph_nodes``
   List of JSON-LD node dictionaries appended to the PROV ``@graph``.  Use
   ``rdflib.PROV``, ``rdflib.DCTERMS``, or your own ``Namespace`` objects
   as keys/values.

``scenario_properties``
   Dict merged onto the *concrete scenario* entity node for this
   configuration.  Useful for adding counts or classification properties
   (e.g. number of goals, number of obstacles).

``run_used_iris``
   List of IRIs that every run activity in this configuration will
   declare as ``prov:used``.  Typically the IRIs of entities generated
   by this variation that are consumed at runtime (e.g. a map file, a
   mesh file).

.. note::

   ``collect_prov_metadata`` receives ``rdflib.Namespace`` objects
   (``campaign_namespace``, ``config_namespace``) so you can construct
   campaign-relative IRIs with ``campaign_namespace["some/path"]``.
   ``rdflib`` is a required dependency of the core ``robovast`` package.


.. _extending-simulators:

Add a Simulator Backend
^^^^^^^^^^^^^^^^^^^^^^^

A backend supplies what a campaign would otherwise restate for every simulator run: the
image, the packages, the environment, and how the simulator is started. Registered in the
``robovast.simulators`` entry-point group; implementation:
:mod:`robovast.common.simulators`. The user-facing side, the two shapes, and a worked
example are in :doc:`simulators`.

Two constraints that are not obvious from the base class:

**It must import without its simulator installed.** ``apply_backend`` runs in the
long-lived service process, which has no reason to carry a MuJoCo or an Isaac runtime.
A backend declares strings and container specs; anything genuinely needing the simulator
returns a ``ContainerSpec`` from ``input_files`` and runs *inside the simulator's image*.

**It cannot arrive through** ``plugins:``. The image and environment hooks run during
composition, long before ``_install_plugins`` — and that install is deliberately
``add_to_path=False``. So a backend is an ordinary installed distribution of the service
environment, like ``robovast_nav``, or is named by a ``.vast``-relative file ref.

**Where it runs in composition.** ``apply_backend()`` is called once at the top of the
``execution`` extraction in ``generate_scenario_variations()``, so the container plan, the
image builds and the run environment all read one already-merged mapping instead of each
re-asking the backend and risking different answers. The campaign always wins: a backend
fills in keys the author left out and never overrides one they set.

.. _extending-input-generation:

Add Input Generator Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^

Input generators produce a campaign's **derived** inputs before it is composed — the mirror
image of postprocessing, at the other end of the campaign. They are declared as
:ref:`execution.generate <execution-generate>` and registered in the
``robovast.input_generators`` entry-point group. Implementation:
:mod:`robovast.common.input_generation`.

**Where it runs in composition.** ``run_input_generators()`` is called from
``generate_scenario_variations()`` *before* ``collect_filtered_files()``, and each entry's
outputs are appended to ``run_files``. That position is the whole design, and everything else
follows from it: the outputs reach ``hash_run_files()`` (so they enter the configuration
identity), ``prepare_campaign_configs()`` copies them into ``<campaign>/_config/``, and the
run container bind-mounts them at ``/config/<path>``. There is no second code path for
generated files anywhere downstream. It also means generation is **host-side, before
publication**, so the cluster lane gets the artifacts with no extra work.

**Generation vs. variation.** Both can produce artifacts, and the test for which you want is:
*does it produce configurations, or only files?* A generator produces files, once per
campaign. Something that multiplies the configuration set — one config per floorplan, with the
generated paths bound to scenario parameters — is a :ref:`variation <extending-variation>`
even when it also generates, because ``execution.generate`` has no way to emit configurations.

**Return value:** ``(success: bool, message: str)``, or ``None`` for success. Raising and
returning ``False`` are reported identically, so use whichever carries the better message.

**Reserved key.** ``out`` belongs to RoboVAST, not the plugin: it is validated, created as a
temporary directory, and swapped into place only on success. So outputs can be expanded into
``run_files`` without importing the plugin (which the isolated compose subprocess relies on),
and a half-written artifact can never be mistaken for a finished one.

**Staleness.** Declare what was read by calling ``write_manifest(out_dir, paths)``, which
writes ``.generated.json`` into the output directory; the next composition hashes those paths
and skips the generator when nothing moved. Report the *real* set — for anything compiled from
a description that can reference others, that includes the transitive ones. A generator that
reports nothing is never cached, and a cached result is honoured only while its outputs are
still on disk unchanged: staleness fails towards doing the work, never towards serving a stale
artifact.

**Creating an Input Generator:**

.. code-block:: python

    from robovast.common.input_generation import BaseInputGenerator, write_manifest


    class MyGenerator(BaseInputGenerator):
        """Compile <thing> into a campaign input."""

        #: Bump when the OUTPUT FORMAT changes, so an upgraded plugin regenerates
        #: even though none of its inputs moved.
        FORMAT_VERSION = 1

        @classmethod
        def get_required_container(cls, parameters):
            """Optional: an aux image, when the tool is not installed alongside RoboVAST.

            Same contract as a variation's — ephemeral ``docker run`` locally, a
            container in the campaign's aux pod in-cluster. Reached via
            ``self.container_runner``,
            whose ``workspace`` is visible at the same path on both sides (use
            ``stage_for_container`` / ``collect_from_container``).
            """
            return None

        def __call__(self, vast_dir, out_dir, source=None, **params):
            sources = compile_thing(os.path.join(vast_dir, source), out_dir)
            write_manifest(out_dir, sources)
            return True, f"compiled {source}"

**Registration:**

.. code-block:: toml

    [tool.poetry.plugins."robovast.input_generators"]
    my_generator = "your_package.generators:MyGenerator"

A project can also skip packaging entirely and reference a local file:
``- ./tools/gen.py:MyGenerator: {out: files/thing}``.

**Usage in .vast config:**

.. code-block:: yaml

    execution:
      generate:
      - my_generator:
          out: files/thing
          source: things/input.yaml

.. note::

   The generator must be importable **by the process that composes the campaign**, which for
   a running service is ``vast serve`` — not the shell the user typed in. The unresolved-name
   error prints ``sys.prefix`` for exactly this reason. An aux container sidesteps the
   question entirely, which is the main argument for declaring one.

.. _extending-postprocessing:

Add Postprocessing Command Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Postprocessing plugins are Python functions that process run result directories (e.g., convert rosbag data to CSV). They are registered as entry points and executed before analysis.

**Return value:** A plugin must return ``(success: bool, message: str)``. It may optionally return a third value, a list of **provenance entries**, so that each produced file is recorded (e.g. which CSV was created from which rosbag). Each entry is a dict with keys: ``output`` (path relative to results_dir), ``sources`` (list of paths), ``plugin`` (plugin name), ``params`` (optional dict). If returned, these entries are merged and written into ``postprocessing.yaml`` in each run folder (``<campaign-name>-<timestamp>/<config>/<run-number>/``).

**Provenance for container scripts:** Plugins that run scripts inside Docker (e.g. via ``docker_exec.sh``) cannot return data directly. The orchestrator passes a **provenance file** path to each plugin (optional kwarg ``provenance_file``). Container-invoking plugins must pass this to ``docker_exec.sh`` as ``--provenance-file HOST_PATH``; ``docker_exec.sh`` mounts the directory at ``/provenance`` in the container and the script receives ``--provenance-file /provenance/<basename>``. The script should write a JSON file at that path with format ``{"entries": [{"output": "...", "sources": [...], "plugin": "...", "params": {}}]}`` (paths relative to the results/input directory). Use the helper ``write_provenance_entry`` from ``rosbags_common`` (same directory as the scripts, so it works in the container) to append entries; the script gets the path from ``--provenance-file`` and uses its own plugin name when calling the helper.

**Creating a Postprocessing Plugin:**

.. code-block:: python

    from typing import Tuple, Optional, List
    
    def my_postprocessing_command(
        results_dir: str,
        config_dir: str,
        custom_param: Optional[str] = None,
        provenance_file: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Convert custom data to CSV.
        
        Args:
            results_dir: Path to the <campaign-name>-<timestamp> run directory to process
            config_dir: Config file directory (for resolving relative paths)
            custom_param: Optional custom parameter
            provenance_file: Optional path for provenance JSON (for container scripts)
        
        Returns:
            Tuple of (success, message) or (success, message, provenance_entries)
        """
        import subprocess
        import os
        
        script = os.path.join(config_dir, "tools/script.sh")
        cmd = [script, results_dir]
        if custom_param:
            cmd.extend(["--param", custom_param])
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            return False, f"Failed: {result.stderr}"
        return True, "Success"

**Register in pyproject.toml:**

.. code-block:: toml

    [tool.poetry.plugins."robovast.postprocessing_commands"]
    my_postprocessing_command = "your_package.postprocessing_plugins:my_postprocessing_command"

**Usage in .vast config:**

.. code-block:: yaml

    analysis:
      postprocessing:
        - my_postprocessing_command:
            custom_param: value

.. _extending-publication:

Add Publication Plugin
^^^^^^^^^^^^^^^^^^^^^^

Publication plugins package or distribute the results directory after
postprocessing.  They are plain callables (functions or class instances) that
operate on the full results directory.

**Return value:** A plugin must return ``(success: bool, message: str)``.

**Creating a Publication Plugin:**

.. code-block:: python

    from typing import Optional, Tuple

    def my_publication_plugin(
        results_dir: str,
        config_dir: str,
        destination: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Upload results to a remote storage location.

        Args:
            results_dir: Path to the results directory (parent of campaign directories).
            config_dir: Directory containing the .vast config file; relative
                paths should be resolved from here.
            destination: Remote destination URL or path.

        Returns:
            Tuple of (success, message).
        """
        import subprocess
        dest = destination or "s3://my-bucket/results/"

        result = subprocess.run(
            ["aws", "s3", "sync", results_dir, dest],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"Upload failed: {result.stderr}"
        return True, f"Uploaded results to {dest}"

**Register in pyproject.toml:**

.. code-block:: toml

    [tool.poetry.plugins."robovast.publication_plugins"]
    my_publication_plugin = "your_package.publication_plugins:my_publication_plugin"

**Usage in .vast config:**

.. code-block:: yaml

    results_processing:
      publication:
        - my_publication_plugin:
            destination: s3://my-bucket/results/

Add Cluster Config Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^

To add a new cluster configuration option for RoboVAST, create a class that inherits from `robovast.execution.cluster_config.base.BaseConfig`.
Register your cluster config in your `pyproject.toml` under `[tool.poetry.plugins."robovast.cluster_configs"]`. The key is the name used to select the configuration, and the value is the import path to your configuration class.

.. code-block:: toml

    [tool.poetry.plugins."robovast.cluster_configs"]
    "YourClusterConfig" = "robovast_<yourplugin>.your_cluster_config:YourClusterConfig"

To test your cluster configuration, you can use:

.. code-block:: bash

    vast exec cluster prepare-setup --cluster-config YourClusterConfig ./setup_output

The output directory will contain all necessary files and instructions to manually execute the setup steps for your cluster configuration and execution.

**Storage for experiment-image builds.** ``build_context_bucket()``
(``cluster_execution.cluster_image_build``) decides where a build stages its context,
from two methods of your config:

* ``get_s3_bucket()`` non-empty → that shared bucket is used, under the
  ``image-builds/<build-id>/`` prefix.
* ``get_s3_bucket()`` returning ``None`` (per-campaign buckets) **and**
  ``get_storage_backend() == "s3"`` → the dedicated ``BUILD_CONTEXT_BUCKET``
  (``robovast-image-builds``), created on first upload by the S3 client's
  ``_ensure_bucket``.
* ``None`` on any other backend → ``ValueError``. Naming a bucket ourselves is only
  sound where the namespace belongs to the deployment's own endpoint and the client can
  create it. GCS satisfies neither: its names are global to all of Google Cloud, and
  ``_GcsStorageClient`` has no bucket creation, so a guessed name would collide or 403
  and then not exist. Such a backend must configure its bucket.

So a new config needs no build-specific method — but if it fronts storage with a global
namespace or a client that cannot create buckets, it must return a bucket from
``get_s3_bucket()``, and ``get_storage_backend()`` must not claim ``"s3"``.

A staged context is deleted again when the build ends (``cluster_image_build``:
``discard_context`` / ``staged_context_build_ids``, driven by ``ClusterService``), so a
new ``StorageClient`` implementation must provide ``delete_prefix`` — refusing an empty
prefix, since on a shared bucket the campaign results sit beside the contexts.

**Pod DNS for unresolvable hosts.** ``get_host_aliases()`` parses
``ROBOVAST_EXTRA_HOST_ALIASES`` (``<host>=<ip>``, comma-separated, grouped by IP into the
shape of the k8s field) and is spliced into the build Job
(``cluster_image_build.build_job_manifest``) and campaign Jobs
(``kubernetes_backend``). Override it if a deployment knows its aliases from somewhere
other than the environment. A malformed entry raises rather than being skipped: a dropped
alias reappears as an unexplained ``no such host`` inside a pod, far from its cause.

The boundary is worth keeping in mind when adding pod specs: ``hostAliases`` writes
``/etc/hosts`` **in the pod**, so it governs what the pod's own processes resolve — the
BuildKit push, or a node inside the scenario. The **image pull** is performed by the
node's container runtime *before* the pod exists, so no pod-level field can influence it;
that stays node configuration, exactly like registry TLS trust.

Historically this path instead *required* ``get_s3_bucket()`` to be set, refusing
per-campaign-bucket deployments with "in-cluster image builds require a fixed S3 bucket
(external-S3 mode)". That was never a real constraint — the embedded MinIO is an ordinary
S3 endpoint and the build Job takes bucket/prefix/endpoint/credentials as plain env — and
the workaround (switching to a shared bucket) silently changed the storage layout of every
campaign, since ``get_s3_bucket()`` drives ``bucket_ops`` too.


Add Share Provider Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^

Share providers are discovered as **entry-point plugins** under the
``robovast.share_providers`` group.  They determine where a finished campaign is
uploaded (see :ref:`cluster-sharing`).  To add a new provider:

1. **Create a provider class** that inherits from
   :class:`~robovast.execution.share_providers.base.BaseShareProvider`
   and implements the three abstract methods:

   .. code-block:: python

      import os

      from robovast.execution.share_providers.base import (
          BaseShareProvider,
          StreamProgressReader,
          UploadProgressReader,
      )

      class MyShareProvider(BaseShareProvider):
          SHARE_TYPE = "myshare"

          def required_env_vars(self) -> dict[str, str]:
              return {
                  "ROBOVAST_SHARE_URL": "URL of the target folder",
                  "MY_SHARE_TOKEN":     "API token for the share service",
              }

          def build_pod_env(self) -> dict[str, str]:
              return {
                  "MY_SHARE_URL":   os.environ["ROBOVAST_SHARE_URL"],
                  "MY_SHARE_TOKEN": os.environ["MY_SHARE_TOKEN"],
              }

          def upload_archive(self, archive_path, object_name, progress_callback=None):
              total = os.path.getsize(archive_path)
              with open(archive_path, "rb") as fh:
                  body = UploadProgressReader(
                      fh, total, progress_callback=progress_callback)
                  ...  # PUT/stream `body` to the share, raising on failure

          def upload_archive_stream(self, fileobj, object_name, progress_callback=None):
              # The launch-time upload-to-share path: the campaign is tarred + gzipped
              # on the fly, so `fileobj` is a readable stream of UNKNOWN length. Use a
              # chunked/streaming transfer (no Content-Length) and wrap `fileobj` in
              # StreamProgressReader to report bytes-sent (total is 0/unknown).
              reader = StreamProgressReader(fileobj, progress_callback=progress_callback)
              ...  # stream `reader` to the share with chunked transfer, raising on failure

2. **Implement** both upload methods on
   :class:`~robovast.execution.share_providers.base.BaseShareProvider`.
   They run **in-process** in the driver (no sidecar, no subprocess) and read
   credentials from ``os.environ`` (populated by ``build_pod_env()``):

   * :meth:`~robovast.execution.share_providers.base.BaseShareProvider.upload_archive`
     uploads a local ``archive_path`` (the ``download_archive`` counterpart; known
     size, resumable). Wrap the body in
     :class:`~robovast.execution.share_providers.base.UploadProgressReader`.
   * :meth:`~robovast.execution.share_providers.base.BaseShareProvider.upload_archive_stream`
     uploads a **streamed** archive of unknown length (the launch-time
     upload-to-share, which never writes a ``tar.gz`` to disk — decisive for ~1TB
     campaigns). Use chunked transfer (no ``Content-Length``; resume is not
     available) and wrap the body in
     :class:`~robovast.execution.share_providers.base.StreamProgressReader`.

   Optionally override
   :meth:`~robovast.execution.share_providers.base.BaseShareProvider.verify_access`
   with a cheap authenticated check so a bad configuration fails the pre-flight
   credential check before any batches run.

3. **Register the provider** in your package's ``pyproject.toml``:

   .. code-block:: toml

      [tool.poetry.plugins."robovast.share_providers"]
      myshare = "mypackage.myshare:MyShareProvider"

4. Re-install the package (``pip install -e .``) so the entry point is
   registered.

After that, ``ROBOVAST_SHARE_TYPE=myshare`` in ``.env`` will select your
provider automatically.

Share provider API reference
""""""""""""""""""""""""""""

.. autoclass:: robovast.execution.share_providers.base.BaseShareProvider
   :members:
   :undoc-members:

.. autoclass:: robovast.execution.share_providers.nextcloud.NextcloudShareProvider
   :members:

.. autoclass:: robovast.execution.share_providers.gcs.GcsShareProvider
   :members:

.. automodule:: robovast.execution.cluster_execution.in_pod_upload
   :members:


Add a MCP Plugin
^^^^^^^^^^^^^^^^

Create a class with a ``name`` property and a ``register(mcp)`` method:

.. code-block:: python

   # my_package/mcp_plugin.py
   from fastmcp import FastMCP

   class MyMCPPlugin:
       @property
       def name(self) -> str:
           return "my_plugin"

       def register(self, mcp: FastMCP) -> None:
           @mcp.tool()
           def my_tool() -> str:
               """A custom tool."""
               return "hello"

Then register the class as an entry point in ``pyproject.toml``:

.. code-block:: toml

   [tool.poetry.plugins."robovast.mcp_plugins"]
   my_plugin = "my_package.mcp_plugin:MyMCPPlugin"

The plugin is picked up automatically the next time the server starts.


.. _extending-search-strategy:

Add Search Strategy Plugin
^^^^^^^^^^^^^^^^^^^^^^^^^^^

A search strategy drives the closed-loop search (see :doc:`search`): it proposes
parameter sets, is told their evaluations, and produces a final report. Strategies
are algorithm-agnostic and share one config schema; only the per-strategy
``strategy_parameters`` block differs.

Subclass ``robovast.search.strategy.SearchStrategy`` and implement the four
abstract methods. Optionally set ``PARAMS_MODEL`` to a Pydantic model — the
framework validates ``search.strategy_parameters`` against it and passes the
parsed object as ``params``:

.. code-block:: python

   from pydantic import BaseModel
   from robovast.search.strategy import SearchStrategy
   from robovast.search.types import ParamSet, SearchReport

   class MyParams(BaseModel):
       step: float = 0.1

   class MyStrategy(SearchStrategy):
       PARAMS_MODEL = MyParams  # optional; omit (None) for no parameters

       def ask(self, n: int) -> list[ParamSet]:
           """Propose n parameter sets (keys match search_space dims)."""
           ...

       def tell(self, evaluations) -> None:
           """Ingest the evaluations of the batch just run."""
           ...

       def is_done(self) -> bool:
           """True when the budget is exhausted / converged."""
           ...

       def report(self) -> SearchReport:
           """Return the deliverable (ranked best, archive, Pareto front)."""
           ...

``self.search_space``, ``self.objectives`` and the validated ``self.params`` are
available on the instance. For single-objective strategies, ``self.objective_value(ev)``
returns the sole objective sign-oriented so that **higher is always better**.

Register the class under ``robovast.search_strategies``; the key is the
``search.strategy`` name:

.. code-block:: toml

   [tool.poetry.plugins."robovast.search_strategies"]
   my_strategy = "your_package.strategies:MyStrategy"

A strategy can also be loaded from a **local file relative to the .vast** without
packaging, using ``strategy: ./search/my_strategy.py:MyStrategy`` (the same
``load_ref`` mechanism used for extractors and search postprocessing).


.. _extending-extractor:

Add Extractor Plugin
^^^^^^^^^^^^^^^^^^^^^

The *extractor* is the single, SUT-specific scoring step: it reads a parameter
set's per-config result directory and returns named **objectives** (optimized)
and **measures** (quality-diversity behavior axes; ignored by non-QD strategies).
This is the one place system-under-test logic lives.

Subclass ``robovast.search.extractor.Extractor``. It is constructed with the
``extract.params`` from the ``.vast`` (so thresholds / column names can be swept
without editing code), and aggregation over the config's runs is its
responsibility:

.. code-block:: python

   from pathlib import Path
   from robovast.search.extractor import (Extractor, ExtractResult,
                                          completed_run_dirs)

   class MyExtract(Extractor):
       # __init__(self, **params) is inherited; params land on self.params

       def extract(self, config_dir: Path) -> ExtractResult:
           runs = completed_run_dirs(config_dir)        # helper: finished runs
           failures = sum(1 for r in runs if _failed(r))
           return ExtractResult(
               objectives={"failure_rate": failures / max(len(runs), 1)},
               measures={},                              # {} when unused
           )

``objectives`` and ``measures`` are named dicts, so single- and multi-objective
use the same shape. The framework records how many runs backed each result.

Register under ``robovast.extractors`` (referenced by ``search.extract.plugin``),
or load from a local file with ``extract.plugin: ./search/extract.py:MyExtract``:

.. code-block:: toml

   [tool.poetry.plugins."robovast.extractors"]
   my_extract = "your_package.extractors:MyExtract"

The extractor **reads** what a postprocessing plugin produced (e.g. per-run
``metrics.csv``) — it no longer computes raw metrics itself. Pair it with a
postprocessing plugin (below) that writes ``metrics.csv`` from raw artifacts:
list that plugin in ``search.postprocessing`` (run before extract) and/or in
``results_processing.postprocessing`` (analysis), so search and the analysis
notebooks read the same metrics. Postprocessing plugins load identically in both
lists — by entry-point name or a local ``./path.py:Class`` file reference.


.. _campaign-store:

Campaign Store and Results Indexing
-----------------------------------

Every campaign — batch or search — is described by a single sqlite store,
``campaign.db`` (``robovast.common.store.STORE_FILENAME``), written at the
root of the campaign directory. It is the **single source of truth** the results
GUI reads, and the seam an in-cluster controller or web UI can later read/stream.

A campaign runs one or more **batches**: a batch-mode campaign (no ``search:``
block) has exactly one batch of the enumerated configs; a search campaign has one
batch per ask/tell round.

Schema
^^^^^^

``robovast.common.store.CampaignStore`` is a thin wrapper over five tables::

    campaign (1) --< batch (1) --< unit (one per param set / config) (1) --< run (one per repetition)
    campaign (1) --< job  (one per execution job)  ...............<  run (via run.job_id)

* **campaign** — ``mode`` (``batch``/``search``), ``config_dir`` (base directory
  against which ``evaluation.visualization`` notebooks resolve), ``config_json``
  (the full config), an opaque ``strategy_state`` blob for resumable
  strategies, and the campaign's **execution provenance**:
  ``robovast_version``, ``execution_type`` (``local``/``cluster``), ``image``,
  ``image_revision`` (the ``repo@sha256`` the runs actually used),
  ``execution_started_at``, ``elapsed_s``, plus ``execution_json`` holding the rest
  of ``_execution/execution.yaml``. Those are the fields one compares *across*
  campaigns, and the SQL interface can attach several campaigns at once — see
  :ref:`database-or-address-space`. They are written by ``record_execution`` once
  the backend has produced ``execution.yaml``, so they are NULL for a campaign that
  died before execution began. Note ``execution.yaml``'s own ``execution_time`` is a
  *start timestamp*, not a duration, hence the column name. What the campaign was
  **asked for** is not here: it lives in ``_execution/launch.yaml``, because the
  service can write that before the run starts while these columns can only be filled
  after it (see :ref:`the launch record <campaign-launch-record>`).
* **job** — one row per **execution job**, holding that job's ``sysinfo.yaml``
  verbatim in ``sysinfo_json``. It is a table rather than a column on ``run``
  because sysinfo is written once per *job*: a packed multi-config job runs several
  ``(config, run)`` pairs which reach the same file through each run dir's ``job``
  symlink. A per-run copy would repeat the blob and destroy the fact that those runs
  shared a machine — which is what makes "did the slow runs land together?"
  answerable. ``job_dir`` is campaign-relative (``_jobs/batch-0/job-3``), or the
  run's own directory for an older layout that wrote sysinfo beside the run.
* **batch** — one ask/tell round (search), or the single batch (``idx=0``) of a
  batch-mode campaign.
* **unit** — one evaluated parameter set (search) or one configuration (batch):
  the sampled ``params``, ``objectives``/``measures`` (JSON; ``{}`` for batch),
  and the ``result_dir``. ``n_samples`` and the aggregate ``status`` are roll-ups
  of the unit's ``run`` rows, kept for convenience.
* **run** — one repetition of a unit (schema v2+). Mirrors that run's
  ``test.xml``: ``status`` (``passed``/``failed``/``error``/``unknown``),
  ``passed`` (0/1), ``errors``/``failures``/``tests``, ``duration_s``,
  ``start_time`` and ``failure_message``. ``run_id`` is the numeric run index
  within the config dir — so it is **not unique on its own**; ``config_name`` lives
  on ``unit``. ``job_id`` points at the job it ran in. A run whose ``test.xml`` is
  missing or unparseable is still recorded, as ``unknown`` — never dropped.

.. rubric:: Two definitions of the schema, on purpose

``_SCHEMA`` is the **full current layout**, applied in one step to a fresh database, so a
reader can see what a table looks like without replaying history. ``_MIGRATIONS`` is the
append-only ladder that upgrades an *existing* store; entry *i* takes ``user_version`` *i*
to *i+1* and is never edited once shipped, because some database on disk has already
applied it. Note migration 0→1 is a frozen copy of the v1 layout rather than ``_SCHEMA``
— reusing ``_SCHEMA`` there would jump an old store to today's tables and the later
``ALTER TABLE`` steps would then fail on columns that already exist.

Adding a column therefore means touching both, in the same position.
``test_fresh_and_migrated_schemas_match`` builds a store each way (from fresh, and from
every older ``user_version``) and compares the resulting ``sqlite_master`` column by
column, so the two cannot drift apart.

.. rubric:: Why ``run`` exists — the data-model layering

``run`` is the **operational source of truth for per-run outcomes**, and it is why
listing campaigns is cheap. ``test.xml`` (JUnit, one per run) is the runner's
on-disk contract; the controller already parses it at record time, so capturing a
``run`` row there is free. Pass/fail counts are then one ``GROUP BY status`` over
``run`` — no filesystem walk — and are available **live**, before postprocessing.
The postprocessed ``_execution/data.db`` ``runs`` table is the analytics-wide
*view* over these rows (joining sysinfo, exploding params into ``param_*``
columns); ``generate_data_db`` reads outcomes from ``campaign.db.run`` rather than
re-parsing every ``test.xml``. Heavy per-run measurement data (metric time-series)
stays in ``data.db`` only — ``campaign.db`` remains the lightweight live store.

::

    test.xml         runner artifact (per run) — the on-disk contract
      -> captured live at record time (data already in hand)
    campaign.db.run  operational source of truth — queryable DURING the run
      -> postprocessing joins sysinfo/params/metrics
    data.db.runs     analytics-ready wide view (param_* columns, metrics) — DERIVED

.. note::

   Schema v1 stores (written before the ``run`` table) migrate forward on open to
   an empty ``run`` table.
   :func:`robovast.common.campaign_index.backfill_run_rows` fills them from disk
   ``test.xml``; the service does this lazily for finished campaigns, and the
   summary path falls back to the ``test.xml`` walk
   (:func:`~robovast.common.campaign_data.get_vast_configuration_info`) until it is
   backfilled, so counts are never under-reported.

Who writes it
^^^^^^^^^^^^^

* **The controller** (``robovast.execution.controller.CampaignController``) writes
  the store *live* for both modes as each batch is evaluated, so progress is
  queryable while a campaign runs. It owns the campaign id, the flat results
  layout (``<campaign>/<config>/<run>/``) and the batch loop; an
  :class:`~robovast.execution.backends.ExecutionBackend` (``DockerBackend``
  locally) only dispatches one batch's jobs.
* **The post-hoc indexer** ``robovast.common.campaign_index.build_campaign_store(campaign_dir)``
  reconstructs the same store by scanning a finished results tree (reusing the
  ``campaign_data`` readers). It is used for campaign dirs not produced by the
  controller — e.g. cluster results downloaded from S3 — and is idempotent
  (mtime-guarded; ``force=True`` to rebuild), invoked by ``vast evaluation index``
  and automatically on ``vast evaluation gui`` launch. Controller-written stores
  are left untouched.

Store-driven GUI
^^^^^^^^^^^^^^^^^

The results GUI (``RunResultsAnalyzer``) discovers campaigns by scanning
``<results_dir>/*/campaign.db`` — there is no filesystem-walk or depth-based
heuristic. It reads the campaign/batch/unit rows to build the tree
(campaign → *batch*, search only → config), resolves notebook workloads from
``config_json`` against ``config_dir``, and enumerates only the run-level leaves
from each unit's ``result_dir``.


.. _controller-control-interface:

Campaign state and control
--------------------------

A campaign is driven by a :class:`~robovast.execution.controller.CampaignController`
running **in the driving process** — the ``vast`` CLI locally, the
``robovast-service`` for a cluster campaign (one worker thread each). There is no
separate controller pod and no in-pod HTTP control channel: monitoring and control
are ordinary :class:`~robovast.service.interface.RobovastInterface` calls the
service answers directly, so the CLI, MCP and web UI all use one path.

Live state
^^^^^^^^^^

:class:`~robovast.execution.control_server.ControllerState` is the thread-safe
holder the controller **writes** and readers **snapshot**. The controller calls
``update`` / ``set_phase`` at each batch boundary; a reader takes a consistent
``snapshot`` (deep copy), which is exactly what ``get_status`` returns. The one
status model, :class:`~robovast.execution.control_server.Status`, is reused
verbatim by the interface, the MCP tools and the TS client.

Because the service drives many campaigns as threads in one process, per-campaign
state that used to be isolated by *being in its own pod* is now isolated
explicitly: the container-runner factory is a ``ContextVar``, ``controller.log`` is
filtered to its worker thread, and each aux pod / result prefix is keyed by
campaign id.

Status: phase and stage
^^^^^^^^^^^^^^^^^^^^^^^^^

``phase`` is an **open** string advanced through a documented vocabulary, with
``stage`` carrying finer markers so new states slot in without a schema change:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - ``phase``
     - Meaning
   * - ``initializing``
     - Accepted: registered, listed, and addressable by id, with the lane's pre-flight
       (project push, registry/base-image resolution) still to do. The first phase every
       campaign has, and the one that guarantees a caller can always find what it just
       started — see :ref:`a-started-campaign-is-findable`.
   * - ``building``
     - Building the experiment image (only when the ``.vast`` has a ``build:`` section).
   * - ``starting``
     - Image settled; handing off to the worker (campaign context, backend construction).
   * - ``plugin install``
     - Installing the declared ``plugins:`` (only when any are declared). ``pip``'s
       output streams live into ``_execution/plugin_install.log``.
   * - ``variation``
     - Expanding the config variations (batch mode); its log is ``variation.log``.
   * - ``running``
     - Batch loop executing.
   * - ``finishing``
     - Search stop condition met (or a ``stop`` was requested); winding down.
   * - ``sharing``
     - Streaming the raw pre-postprocessing archive to the configured share (only when
       ``upload_to_share`` was set).
   * - ``postprocessing``
     - Chained analysis postprocessing (rosbag→CSV Job + ``data.db``) running.
   * - ``finished``
     - Done; the campaign is published to the object store. **A post-run step may still
       have failed:** the runs are the deliverable, so a failed upload-to-share or
       postprocessing keeps the phase ``finished`` and records the reason on
       ``share_error`` / ``postprocessing_error`` (durable, re-triggerable) rather than
       failing the campaign.
   * - ``failed``
     - The *runs* were aborted. ``stage``/``error`` say why (e.g. a bad ``config_filter``
       lists the available config names). Recorded to ``_execution/outcome.json`` so it
       is readable after the fact.
   * - ``stopped`` / ``crashed`` / ``unknown``
     - Terminal: cooperatively stopped; the driver died without recording an outcome; or
       (after a restart) not reconstructable from disk.

``phase_since`` records when the current phase was entered (and only moves on an actual
change, so a defensive re-set does not restart the clock). It exists because a phase name
alone cannot separate *slow* from *wedged*: an image build in progress and one that will
never finish both read ``building``. Readers render it as an age — the MCP status returns
``phase_age_s``, and the web Monitor shows it beside the phase dot while a pre-run phase is
in effect, where there is no progress bar to watch instead.

.. _a-started-campaign-is-findable:

A started campaign is findable
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

**Invariant: from the moment the service accepts a campaign, its id resolves on every
read path** — ``get_status`` and ``list_campaigns`` (also the MCP's ``running_only`` filter). This is what
makes the standing advice ("a timed-out start is not a failed start; check, never retry")
actually true: retrying a start that in fact succeeded creates a second campaign, and the
only defence is that the first one can be found.

Three things back it. Registration happens *before* the slow work — ``create_campaign``
records the campaign in the lane's registry and returns, and the driver builds the image —
so the campaign is live from ``t=0`` rather than from whenever its results directory
appears (see :ref:`campaign-building-phase`). A multi-lane service unions its sibling
lanes' registries into the listing via ``_extra_live_ids``: ``list_campaigns`` derives its
id set from the *local* lane's "disk ∪ in-memory" view, so without that a cluster campaign
was missing from every listing for the whole length of its pre-flight — registered and
addressable by id, but undiscoverable by anyone who did not already know the id, which is
precisely the caller whose start response was lost. And a campaign whose durable home is
not this disk is unioned in the same way via ``_durable_campaign_ids``
(:ref:`campaign-discovery`), which is what keeps the invariant true across a service
restart rather than only within one process's lifetime.

.. _campaign-discovery:

Discovering campaigns whose home is the object store
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``list_campaigns`` builds its id set from three sources: the results directory on disk,
the in-memory registry of what is being driven (plus ``_extra_live_ids`` for a sibling
lane's), and ``_durable_campaign_ids`` for campaigns **stored** somewhere that is not this
disk. The last one exists for the cluster lane: a finished cluster campaign lives in the
object store, and in-pod the service's disk is scratch, so every campaign from a previous
service life was invisible to a listing that only scanned it.

**Why not enumerate buckets.** ``StorageClient`` has no bucket listing; with a
per-campaign-bucket deployment each campaign *is* a bucket named
``campaign_id.lower().replace("_","-")``, so recovering the id means inverting a lossy
transform and returning a *different* id for any name with ``_`` or upper case; and buckets
should stay internal. Instead each campaign publishes a **marker** under one known prefix,
and that prefix is what gets listed:

.. code-block:: text

   campaign-index/<campaign_id>/<created_at>      # a zero-byte object

The **key is the whole record** (``in_pod_storage.mark_campaign_indexed``). There is no
body, so there is nowhere to put a status: ``_execution/outcome.json`` stays the one
canonical terminal record and the index cannot become a second source of truth for the
phase. Two details earn their place in the key:

* the id is a segment *verbatim* — nothing was sanitised on the way in, so nothing has to
  be undone on the way out;
* ``created_at`` is there because the **listing** needs it. ``list_campaigns`` asks every
  candidate for its start time to order them *before* it paginates, so a start time that
  cost an object read would be one round-trip per campaign on every cold listing — with a
  100-campaign SSE poll behind it. In the key, one cached listing answers the whole
  ordering pass.

The marker is written by ``_on_campaign_started``, a launch hook called at the top of the
driver — before the image build and the run — so every later outcome belongs to a campaign
that can still be found: a failed build, a crash mid-run, a stop, a failed finalize upload.
It is best-effort with a warning, because a campaign is not worth failing over its index
entry, and a store broken enough to refuse it will fail the campaign's own uploads with a
real error moments later. ``delete_campaign`` retires it, wholesale or (``data_only``) its object-store data —
the latter driven by what the sweep actually removed, so a campaign whose delete *failed*
keeps its marker and its data stays listed.

**The records themselves come from ``_record_dir``.** Four readers need a campaign's
recorded facts — ``_summary_for``, ``_started_at_for``, ``_description_for``,
``_status_from_disk`` — and each used to resolve ``<results>/<id>`` inline, so the cluster
lane had to override all four or none. They now go through one seam, and ``ClusterService``
overrides just it: for a campaign with no local copy it materializes exactly two small
objects — ``campaign.db`` and ``_execution/outcome.json`` — into the campaign's cache dir,
after which every inherited reader is correct with no second implementation. Deliberately
a **single-object** fetch (``_materialize``, shared with ``_query_dir``) and never
``fetch_campaign``: a 2 KB record must not drag a 1 TB campaign. It is skipped for a
campaign this process is driving (its driver owns ``campaign.db``), for one whose local dir
already has it, and for one the index does not list — that last check is what keeps a
listing behind an *unreachable* store to one connect timeout for the page instead of one
per row.

.. _campaign-building-phase:

A campaign waits for its image; the request does not
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

``create_campaign`` is fire-and-forget, so the image build runs on the campaign's worker,
not on the caller's thread. Awaiting it inline meant a cluster start for a project with a
``build:`` section died on the HTTP client's 30 s read timeout **while the server kept
going and the campaign succeeded** — a reported failure for work that worked — and, since
the campaign was created only after the build, none of that work was observable while it
ran.

What the caller gets instead: the campaign id immediately, and a campaign in phase
``building`` whose ``stage`` reads ``waiting for image <tag>``. ``list_campaigns(running_only=True)``
needed no change for it to appear there — a building campaign is a campaign, and the
listing already unions the in-memory registry. (Teaching that tool to enumerate *builds*
would have needed a listing endpoint that does not exist, returned entries that are not
campaigns, and created a second in-flight registry to keep consistent with the first.)

Builds are **shared** — ``build_hash`` is content-addressed over the spec and context, so
two campaigns needing the same image both wait on one build. Two consequences:

* The phase means *waiting for* its image, not *performing* the build; otherwise two
  campaigns each appear to be building the same one. The ``stage`` and the ``build.log``
  header both say so.
* Stopping a building campaign **detaches** it. ``_await_build_image`` raises
  ``CampaignStopped`` and touches neither the build Job nor the local build thread: a
  sibling may be waiting on that build, and the image is a cache entry rather than this
  campaign's property. Nothing cancels a build today — the cluster teardown is label-scoped
  to ``jobgroup=scenario-runs`` and cannot reach a ``jobgroup=image-builds`` Job, and the
  local ``docker rm -f robovast`` cannot reach a ``buildx`` thread — and it must stay that
  way.

One wait loop serves both lanes: ``_await_build_image`` drives ``get_image_build_status``
and ``get_image_build_log``, which are interface operations each transport implements, so
the local Docker build and the in-cluster BuildKit Job are waited on by the same code
rather than by two that drift. It replaced ``_ensure_build_image`` plus a per-lane await
loop on each side — three methods where there were four, one of them shared.

The loop also **tees the build log into the campaign's own** ``_execution/build.log``,
which ``INFRA_PHASES`` serves as a leading ``BUILD`` section (see :ref:`the MCP build workflow <mcp-build-phase>`). That
is the campaign's only durable record of the image it ran on: the live source dies with
the build, since a build Job is reaped at ``ttlSecondsAfterFinished``, and a failed build
is exactly when someone comes looking. It is also why
``controller._record_controller_outcome`` uploads ``build.log`` alongside ``outcome.json``
— a campaign that died waiting for its image never reaches ``finalize_campaign`` at all.

This changes an error path on purpose: a failed build is now an inspectable ``failed``
campaign — reason in its status, output in its own log — rather than a 500 and no campaign.
It applies to the local docker build too: building is part of the campaign's driven work,
not a precondition of its existence.

Control operations
^^^^^^^^^^^^^^^^^^^

* ``stop`` (``client.stop``) — sets a cooperative flag on the campaign's
  ``ControllerState`` (``request_stop``); the loop ends after the current batch. In
  the service this is a direct in-process call.
* ``get_campaign_logs`` — serves ``controller.log`` from a byte offset (live file
  while the campaign runs, the object-store copy afterwards). The web UI polls it to
  stream the log; ``vast … monitor`` renders live status from ``get_status``.
* ``upload_to_share`` (launch flag on ``create_campaign``) — when set, the driver
  streams a raw, pre-postprocessing archive of the campaign to the configured share
  the moment the runs finish, *before* analysis postprocessing. A share failure never
  loses the campaign: it stays ``finished`` and the reason is recorded on
  ``share_error`` (durable). Local backends write the ``tar.gz`` to
  ``<results>/_archives/`` instead; cluster backends stream it to the share provider
  with no on-disk copy. The download counterpart is the ``/campaigns/{id}/archive``
  stream (the postprocessed campaign, tarred on the fly from the object store).
* ``run_share`` (``client.run_share``) — re-triggers the upload-to-share on a finished
  campaign, from the stored campaign alone (works after a service restart, no live
  entry). The provider comes from the environment, so adjusting ``ROBOVAST_SHARE_TYPE``
  and re-triggering uploads to a different destination. Mirrors ``run_postprocessing``.

API reference
^^^^^^^^^^^^^

.. automodule:: robovast.execution.control_server
   :members: Status, ControllerState
   :undoc-members:


.. _cluster-resource-resolution:

Per-Cluster Resource Resolution
-------------------------------

The resolution logic for per-cluster resources (the ``{context-name: value}``
mappings documented under *Per-Cluster Resource Limits* in
:doc:`cluster_execution`) lives in :mod:`robovast.execution.cluster_execution.cluster_context`:

.. automodule:: robovast.execution.cluster_execution.cluster_context
   :members: get_active_kube_context, list_all_contexts, get_config_context_names,
             require_context_for_multi_cluster, resolve_resource_value, resolve_resources
   :undoc-members:


.. _kueue-internals:

Job admission (Kueue) internals
-------------------------------

User-facing behaviour is in :doc:`cluster_execution`; this is the machinery
behind it. RoboVAST targets Kueue |kueue_version|.

.. |kueue_version| replace:: 0.16.1

**Objects.** A single ``ResourceFlavor`` (``default-flavor``) represents the
homogeneous node pool; a ``ClusterQueue`` (``robovast-cluster-queue``) holds the
combined CPU/memory quota, sized from ``allocatable − requested`` at setup time;
a ``LocalQueue`` named ``robovast`` in the execution namespace is the submission
target. Each generated Job carries the **label**
``kueue.x-k8s.io/queue-name: robovast`` — Kueue 0.16 keys queue membership off
the label, not an annotation.

**Why there is an admission preflight.** Because every Job is labelled into the
LocalQueue, Kueue creates it **suspended** and starts it only once the queue
admits it. A broken admission path therefore does not fail the submit — the jobs
simply never start, with no pod, no event and no error. Nothing about that state
looks like a failure: the Job counts as active, the campaign log says "still
running", and the ``activeDeadlineSeconds`` backstop cannot fire because its
timer does not run while a Job is suspended.

``verify_kueue_admission_ready()`` therefore runs before any Job is created (and
again every 30 s while a batch waits, so a queue broken *mid*-campaign is caught
too). It fails the campaign with an actionable message when:

* the ``LocalQueue`` does not exist in the execution namespace — setup was never
  run, or the campaign targets a different namespace;
* the ``ClusterQueue`` it points at does not exist;
* the ``ClusterQueue`` is stopped (``stopPolicy: Hold``);
* the ``ClusterQueue`` reports ``Active=False`` — most often a missing
  ``ResourceFlavor``, which leaves every object present but unusable.

The same check runs as a post-condition of ``vast execution cluster setup``, so
setup cannot report success while leaving the queues unusable.

**Quota exhaustion is not a failure.** A queue whose capacity is currently used
up is healthy, and the correct response is to wait — that is Kueue's normal
operating state. The preflight only ever looks at the *structure* of the
admission path. While jobs wait, the batch logs Kueue's own reason (read from
each Workload's ``QuotaReserved`` condition) instead of a bare "still running",
and ``list_campaign_jobs`` reports them ``waiting`` with that reason as
``detail`` — not ``blocked``, which is reserved for a job that cannot start on its
own (image pull / config error) and therefore needs a human.

The preflight reads ``localqueues`` (namespaced) and ``clusterqueues``
(cluster-scoped); both grants are on the service's Role and ClusterRole. An
**existing** deployment must be redeployed to pick them up — until then the check
reports "cannot verify" and the campaign proceeds, since a missing read
permission should never block a run that would otherwise work.

**Holding the queue during cleanup.** ``stopPolicy`` lives on the single,
cluster-scoped ``ClusterQueue`` that every campaign shares. Holding it stops
*all* admissions — ``Hold`` versus ``HoldAndDrain`` decides only whether
already-running workloads are preempted, not whose workloads are affected.

Cleaning up **one** campaign therefore does not touch it: doing so would stall
every other campaign's pending jobs for the length of the cleanup, and a cleanup
that died in between used to leave the queue held permanently — suspending every
later campaign forever, indistinguishable from a missing ClusterQueue.
Per-campaign quota safety does not need the hold: the deletions are label-scoped
and ordered Workloads-before-Jobs, which is what lets Kueue release that
campaign's quota cleanly.

A **cluster-wide** cleanup (no campaign given) does hold the queue, since pausing
everything is the intent. It restores the *previous* ``stopPolicy`` in a
``finally``, so a concurrent teardown's hold survives and an error can never
leave the queue stopped.


.. _web-ui-internals:

Web UI internals
----------------

The web frontend (user guide: :ref:`web_ui`) is a **plain Vite + React + TypeScript
app** (MUI + `TanStack Query <https://tanstack.com/query>`_) living at
``frontend/ui/``. It has its own ``package.json`` / ``node_modules`` and is
independent of the Python build; ``npm run build`` emits ``frontend/ui/dist``.

It is deliberately **not** a plugin framework or a shared UI shell — most of what a
bespoke shell would provide maps to stable, widely-used OSS (server-state polling →
TanStack Query, charts → Plotly, forms → Monaco/rjsf), so the app is an ordinary
React app that reuses those libraries directly.

**Thin client of the interface.** All service access goes through one seam,
``frontend/ui/src/lib/robovastClient.ts``, which mirrors
:class:`robovast.service.interface.RobovastInterface` — the ``Routes`` paths and the
request/response models (including :class:`~robovast.execution.control_server.Status`)
— **1:1**, exactly as the Python ``HTTPTransport`` does. When the interface changes,
update this file. Pages call the client via TanStack Query (``refetchInterval`` drives
the Monitor's live polling; mutations drive create/stop). Current pages:
``frontend/ui/src/pages/Monitor.tsx`` and ``frontend/ui/src/pages/Launcher.tsx``, sharing
``frontend/ui/src/components/StatusView.tsx`` for the live ``Status`` render.

**The service serves the SPA.** :func:`robovast.service.app.build_app` mounts
``frontend/ui/dist`` at ``/`` (``_mount_ui`` / ``_ui_dist``) **after** registering the API
routes, so the interface routes win and the SPA is served from everything else. The
UI is therefore same-origin with the API (no CORS in production) and **starts with the
service**. The build location is ``frontend/ui/dist`` relative to the repo, overridable with
``ROBOVAST_UI_DIST``; package the built assets into the service/controller image so the
in-cluster service ships the UI. If no build is present the service runs API-only.

**Dev loop.** ``cd frontend/ui && npm run dev`` runs Vite (:5173) and proxies the API path
prefixes to a running ``vast serve`` (``ROBOVAST_SERVICE_URL`` to retarget), keeping the
browser same-origin for hot-reload development.

.. _frontend-tests:

**Frontend tests stay minimal.** ``npm run test`` runs `vitest
<https://vitest.dev>`_ over the pure modules in ``frontend/ui/src/lib/`` — and nothing else.
The checks that matter most here are still ``npm run lint`` (``tsc --noEmit``) and
``npm run build``; a spec is the exception, not the habit.

A spec earns its place only where the logic is non-obvious and regression-prone **and**
checking it by hand means clicking through the UI: node-id and route stability, grouping,
rollups, formatting. ``src/lib/resultsTree.test.ts`` is the model — the Results tree's node
ids are a contract between two components that build them independently (the tree renders
them, the Run view reconstructs one to highlight the current run), so a drift shows up only
as "the picker stopped opening on the selected run", which no compiler catches.

What not to do, because each of these costs far more than it protects here:

* no component or DOM tests, no ``jsdom``, no ``@testing-library``, no snapshots of rendered
  output — the UI is a **thin client** of :class:`~robovast.service.interface.RobovastInterface`,
  so behaviour belongs in the Python tests, which is where it is enforced;
* no network mocking — if a test needs the service, it is testing the service;
* nothing ``tsc`` already proves (a required field being present, a union being exhaustive);
* no test framework stack beyond vitest's defaults over the existing ``vite.config.ts`` (which
  is also where the ``@`` alias comes from, so a spec needs no path config of its own).

Note that CI does not build ``frontend/ui`` at all, so these run locally and in agent
sessions rather than as a gate — another reason to keep them few and fast.

**Extending.** Add an operation by giving ``robovastClient.ts`` a method mirroring the
new interface op, then a page/tab that queries it.

**Resource usage (``/usage``).** ``resource_usage`` is a backend-agnostic interface op
returning :class:`~robovast.service.interface.ResourceUsage` (CPU cores + memory bytes,
capacity vs. used, and a ``parallel_runs`` flag). The local↔cluster split lives entirely
in the implementations: ``LocalTransport._compute_resource_usage`` reads the host via
``psutil``; ``ClusterService`` overrides it to sum node ``allocatable`` (capacity, reusing
``kubernetes_kueue._parse_resource``) and the requests of the non-terminal pods *bound to
those same nodes* (used) — so callers (the top-bar chip, the ``resource_usage`` MCP tool)
never branch on backend. Summing both sides over one node set is what keeps ``used <=
capacity``: a pod still queued for a node requests cores nothing has granted, and counting
it reported "29.7/24" on a 24-core cluster. Pending work is ``jobs_pending``, not usage.

That scenario-run tally is the other half of the op, and it is counted from **Jobs, not pods**, on
both lanes: ``running`` = executing, ``pending`` = accepted but not executing. A Kueue-suspended Job
has no pod at all — the state every cluster batch *starts* in — so the original pod-based count
reported a freshly launched sweep as ``0/0`` with its whole queue waiting for quota.
``ClusterService._scenario_job_tally`` therefore delegates to
``cluster_execution.list_jobs_with_phase`` (the single place Jobs + pods become a phase, so no
consumer re-derives it and drifts) and folds ``waiting``/``blocked`` into ``pending``, namespace-
scoped because it answers "what is *this* service running" while CPU/memory must stay cluster-wide.
``LocalTransport._scenario_job_tally`` reads the same pair off the live campaigns' controller
snapshots — ``running`` is 0 or 1, the lane being single-flight — rather than off disk, which would
call a run that died without a ``test.xml`` "running" forever. The UI's sidebar meter reads
``running/(running+pending)`` and hides itself when both are zero.

Both share one
TTL-cached path on the base class (``LocalTransport.resource_usage`` memoises for
``_USAGE_CACHE_TTL`` under a lock), so many polling clients cost one sampling per window.
The cluster read needs cluster-scoped RBAC (nodes are not namespaced): setup grants the
service ServiceAccount a read-only ``ClusterRole`` over ``nodes``/``pods``
(``service_deploy._service_rbac_manifests``), so **upgrading an already-deployed service to
this version requires setting it up again** (``vast exec cluster cleanup`` then ``setup``,
or ``setup --force``) to add the grant.

**Config editor** (``frontend/ui/src/pages/ConfigEditor.tsx``) is the browser ``vast config gui``:
a **Monaco** editor (``frontend/ui/src/lib/monaco.ts`` bundles the editor + YAML workers and, via
``monaco-yaml``, drives completion/inline-validation from ``get_config_schema()``). It edits a
workspace's ``.vast`` (workspace = the project, since a browser has no CWD), autosaves +
debounce-validates through ``validate_project``, and previews resolved configurations through
``preview_configurations``. These four ops — ``validate_project`` / ``preview_configurations`` /
``get_config_schema`` / ``list_variation_types`` — were **promoted onto** ``RobovastInterface``
(previously MCP-only, calling local functions); ``LocalTransport`` wraps ``validate_project_file``
/ ``generate_scenario_variations`` / ``ConfigV1.model_json_schema()`` / the
``robovast.variation_types`` entry points, and ``validate_project`` swallows unexpected errors into
a structured problem so the editor's live validation never 500s on in-progress YAML.

**Per-variation previews.** ``preview_configurations`` returns, per configuration, a ``previews``
list (``{variation_type, params, remote}``) read from the config's declared ``variations``. The
editor (``frontend/ui/src/preview/``) renders **built-in** variation types host-native — ``builtins.tsx``
maps a variation-type name to a Plotly component (distribution curves for
``ParameterVariationDistribution*``, a value list for ``ParameterVariationList``). An **external**
variation plugin may instead ship a web preview: it sets ``Variation.WEB_PREVIEW`` to a
package-relative dir holding a built **Module Federation** remote (``remoteEntry.js`` exposing a
``./preview`` React component); the service serves it at
``GET /variation_types/{name}/assets/{path}`` and ``preview_configurations`` fills in the ``remote``
descriptor, which ``RemotePreview.tsx`` loads at runtime (sharing the host's React singletons).
Built-ins ship no assets; a type with neither shows just the resolved parameters.

**Results viewer** (``frontend/ui/src/pages/Eval.tsx``) is the browser ``vast eval gui``: pick a
campaign, browse its ``data.db`` schema, run read-only SQL, and chart the result with
**Vega-Lite** (``frontend/ui/src/preview/VegaLiteChart.tsx`` — rows bound in as ``data.values``).
The two data-query ops — ``describe_campaign_data`` / ``query_campaign_data_sql`` — were
**promoted onto** ``RobovastInterface``; the actual SQL lives in one shared, directory-based
helper, :mod:`robovast.results_processing.data_query` (``mode=ro`` + a ``sqlite3`` authorizer,
``campaign.db`` attached as schema ``campaign``). Both callers reuse it: the service methods
resolve the campaign dir per transport (``LocalTransport`` on disk, the cluster service via
``fetch_campaign`` from the object store), and the MCP ``run_data`` plugin resolves it via
``results_resolver`` **or delegates to a configured service** — so CLI, MCP, and the web UI query
results identically, local or cluster. User-declared plots (``evaluation.plots`` in the ``.vast``,
:class:`robovast.common.config.PlotSpec`) are surfaced by ``list_campaign_plots`` and rendered by
the same Vega-Lite component.

**Run view panel framework** (``frontend/ui/src/lib/dashboard/``) — the Run view (user guide:
:ref:`web_ui`) is a small plugin framework with three deliberate seams, all designed so a
future **live view** (watching a running system instead of replaying a rosbag) slots in
without touching any panel:

* **Panels** are plugins with **two delivery mechanisms behind one contract**. A
  ``PanelPlugin`` = manifest (type name + layout defaults) + React component implementing
  ``PanelProps`` (``{spec, clock, data}``). *Core built-in* panels self-register via
  ``registerPanel`` (``registry.ts``) by importing ``frontend/ui/src/panels/index.ts``. *Remote*
  panels — package-provided or user-authored — are loaded at runtime as Module-Federation
  remotes (see below) and never touch the static registry. The ``.vast``'s
  ``visualization.panels`` specs are normalized by ``parseVastPanels.ts`` (single-key
  shorthand → ``{type, ...fields}``; the same shorthand is accepted by the Pydantic schema,
  see ``PanelConfig._flatten_shorthand``). Valid ``type`` values are the core built-ins
  (``BUILTIN_PANEL_TYPES``) ∪ installed ``robovast.panel_types`` entry-point names ∪
  ``custom`` — validated in ``PanelConfig._known_type`` in :mod:`robovast.common.config`
  (so a package panel is valid only when its plugin is installed). Adding a core built-in
  is still one file + one ``BUILTIN_PANEL_TYPES`` entry.
* **Data** comes only through ``DataProvider`` (``dataProvider.ts``): rows by table+time,
  nearest-sample lookups, a **generic run-scoped** ``fetchRun(endpoint, params)`` (GET a
  campaign endpoint with ``config_name``+``run_id`` applied — how a panel reaches a
  specialized endpoint without the generic seam knowing about it, e.g. the costmap panel's
  nav grids), and ``runFileUrl`` for per-run artifact files — today implemented over the
  read-only data-query endpoints (``dbDataProvider``); a live implementation would wrap a
  rosbridge buffer. ``timeSeries.ts`` wraps one table as a time-indexed ``TimeSeriesSource``
  (``at(t)``/``upTo(t)``).
* **Time** comes only from the shared ``PlaybackClock`` (``clock.ts``), an external store
  (not React state) so display-rate updates don't re-render the tree; canvas panels
  subscribe imperatively via ``useCanvasClock``.

**Package-provided & user-authored panels (Module Federation).** A remote panel is loaded
at runtime by exactly the seam the variation-preview path uses (above) — the two now share
their machinery. Server side, ``_resolve_plugin_asset(group, name, rel, asset_attr)`` (in
``service/app.py``) and ``_plugin_remotes(group, asset_attr, url_builder, module_attr)`` (in
``service/client.py``) are the generalized forms of the old variation-only helpers;
``_variation_remotes`` and ``_panel_remotes`` are thin wrappers. A **package panel** is a
class in the ``robovast.panel_types`` entry-point group declaring ``WEB_PANEL`` (its built
bundle dir, shipped as package data) + ``PANEL_MODULE``; the service serves it at
``GET /panel_types/{name}/assets/{path}`` and ``list_campaign_panels`` attaches its ``remote``
descriptor. A **user-authored** ``custom`` panel names a bundle path relative to the ``.vast``;
``_collect_analysis_input_files`` stages the bundle into the campaign's ``_config/`` and it is
served at ``GET /campaigns/{id}/panel_assets/{path}`` (``resolve_campaign_panel_asset``,
path-confined). Both descriptors are ``{name, remote_entry_url, module}``; on the client,
``useRemoteComponent`` (``frontend/ui/src/lib/remote.ts``, factored out of ``RemotePreview.tsx``) loads
the module — seeding the host's React singletons — and ``PanelHost`` mounts it with the full
``PanelProps``. **Reference example:** the ``costmap`` panel, relocated out of the core UI into
``robovast_nav`` — Python descriptor in ``robovast_nav/panels.py``, build sources in
:repo_link:`src/robovast_nav/web` (a ``@module-federation/vite`` remote exposing ``./costmap``,
built into ``robovast_nav/web/dist``). It is the first real remote, and validates the (formerly
untested) variation-preview loading path too. Its **data endpoint** is likewise package-provided —
see *Package-provided service data endpoints* below — so both halves of the costmap panel live in
``robovast_nav``.

**Write a remote against** :repo_link:`frontend/panel-kit` (``@robovast/panel-kit``), never against a copy
of the host's types. Sharing only ``react``/``react-dom`` at runtime is a *runtime* constraint, not
a source one: the contract (``PanelProps``, ``DataProvider``, ``PlaybackClock``) and the
clock-driven scaffolding (``useCanvasClock``, the time-index binary search, ``keyframes`` for
samples too large to preload) are in-tree source that both the host and each remote compile into
their own bundle. Resolve it with a ``tsconfig`` ``paths`` entry plus a matching vite
``resolve.alias`` — see ``src/robovast_nav/web`` for the two lines. Do **not** add it to the MF
``shared`` map: bundling a private copy is what keeps a version skew between an installed package
and a newer host UI an ordinary build rather than a remote-load failure. The earlier arrangement — a
hand-maintained ``contract.ts`` mirror plus a re-implementation of the host's canvas/clock
scaffolding — is what let a fetch-staleness bug exist in the costmap panel and nowhere else.

A panel type may also declare an optional ``REMOTE_NAME`` — the Module-Federation *container*
name, defaulting to the entry-point name (one container per type). Panels that share a single
built bundle set the same ``REMOTE_NAME`` so ``_plugin_remotes`` emits that shared name while each
type keeps its own asset URL (``/panel_types/<name>/assets/...`` all resolve to the one bundle).
``robovast_nav`` uses this to host its panels in one ``robovast_nav`` container (see
``robovast_nav/web/vite.config.ts``), sharing React/vendor chunks.

**Nav2 behavior tree (when a package should ship no panel at all).** nav2's *internal* behavior
tree is shown in the run view, and ``robovast_nav`` contributes none of the rendering. nav2's
``/behavior_tree_log`` is the only generic, always-on source of BT state, but it is
**topology-free** — a flat stream of status transitions keyed by ``node_name``. The data half
splits across the two extension seams the same way costmap's does: the core rosbag handler
``nav2_bt_to_csv`` (``rosbags_process.py``; core because ``HANDLER_REGISTRY`` isn't
plugin-extensible and it needs the in-container ``rosbags_process`` step) writes the raw
``nav2_behavior_tree`` transitions table, and ``robovast_nav``'s ``nav2_bt_tree`` postprocessing
plugin (a ``robovast.postprocessing_commands`` entry point) reconstructs structure from the BT
**XML** nav2 ran and joins it into a ``nav2_behaviors`` table.

That table uses the **same schema as scenario_execution's** ``behaviors`` table, and that is the
whole point: the built-in ``scenario_tree`` panel renders *any* tree expressible in it. So
``robovast_nav`` ships the ``nav2_behavior_tree`` **type** but no renderer — its panel module is a
handful of lines that render the built-in one with nav2's table, title and empty-state hint.

**Deriving a panel** is what makes that possible. ``PanelProps.builtins`` carries the host's own
panel components to a remote (``PanelHost``'s ``RemotePanel`` reads them from the registry at
mount), because a Module-Federation remote shares only ``react``/``react-dom`` and cannot import
the host's modules. A derived panel inherits every later improvement to the built-in — child
ordering, node-kind glyphs, feedback, source lines, scrolling — instead of needing them
implemented twice.

The package did once ship a full port of the built-in panel in plain React; it was deleted for
exactly that reason. Deleting the *type* along with it was a mistake worth recording: a type name
is not a duplicate. Configs name it (frozen ``_config`` copies of past campaigns included), and
``PanelConfig._known_type`` validates against installed entry points, so removing the entry point
turned every one of those configs into a validation error and an "Unknown panel type" box.

The rule, then: **a package needs a renderer only when the host cannot draw its data at all.**
``costmap`` qualifies — binary grids need their own service endpoint. A package whose data is a
table in an existing schema still gets a type of its own, but derives the panel.

**Package-provided service data endpoints** (``robovast.service_endpoints``,
:mod:`robovast.service.endpoint_plugin`) — an installed package contributes a run-scoped data
endpoint served at ``GET /campaigns/{id}/<name>?config_name=…&run_id=…&…`` → JSON, with **no core
edit and no frontend change** (the run view already reaches any such endpoint via
``data.fetchRun(name, params)``, ``frontend/ui/src/lib/dashboard/dataProvider.ts``). This closes the last
core-coupling for a self-contained analysis package: it ships a **postprocessing** plugin (writes a
``data.db`` table), a **service endpoint** (serves it), and a **panel** (renders it) — all via entry
points. The mechanism mirrors the MCP-plugin loader: a ``ServiceEndpoint`` ``Protocol``
(``name`` + ``handle(ctx)``) and ``load_service_endpoints()``; ``build_app`` registers one route per
plugin (before the SPA mount) and dispatches to ``handle`` with a **``RunDataContext``** facade —
``ctx.open_db()`` (read-only sqlite over ``data.db``, from the public ``data_query.open_data_db``),
``ctx.run_dir(config, run)``, ``ctx.params``. Handlers raise ``KeyError``/``ValueError``/
``DataQueryError`` → 404/400 via the shared ``_guard``. **Cluster-transparent** because dispatch
resolves the campaign dir through the public ``impl.resolve_data_dir(campaign_id)`` seam, which
``ClusterService`` overrides to fetch from the object store — so a plugin endpoint works on both
deployments unchanged. Endpoint names should be **package-namespaced** (``nav/foo``) to avoid
collisions; core route names are reserved (``RESERVED_CAMPAIGN_ENDPOINTS``). **Scope:** run-scoped
GET→JSON only — *binary/large per-run artifacts* are already served by the file address space,
``GET /results/<campaign>/<config>/<run>/<path>`` (``DataProvider.runFileUrl``), and
*producing* data is a postprocessing plugin's job. **Reference:** ``robovast_nav``'s
``CostmapEndpoint`` (``robovast_nav/service_endpoints.py``), relocated verbatim from core's old
``read_costmap_frame`` — it reads the ``costmaps`` table via ``ctx.open_db()`` and returns the frame
dict the costmap panel decodes.

**3D scene viewer core** (``frontend/ui/src/lib/scene3d/``) — renders the browser scene
descriptor (``scene.json``/``scene.bin``), which a simulator backend's exporter produces;
the format is RoboVAST's, see :doc:`run_capture`. ``sceneLoader.ts``
builds a three.js ``Group`` and returns an imperative animation API (``jointMap`` /
``basePose``); ``viewport.ts`` is a plain-three viewport (renderer/camera/lights/grid/orbit
controls + the Z-up wrapper). The wheel is the viewport's own, not the orbit controller's
(``cursorDolly.ts``): scaling the orbit radius toward a fixed pivot makes the travel a geometric
series converging on that pivot, so a notch moves millimetres once you are close and the pivot can
never be passed through — flying the eye *and* the pivot along the cursor ray keeps the radius, and
with it the step size, constant. The same change makes a fixed far plane visible, so ``viewport.ts``
sizes the frustum each frame to enclose the world's bounding sphere — measured from the *scene*, not
from the pivot, which the wheel now carries along and which is therefore constant by design.
**Extractability rule: files in this directory import only
``three`` — never ``@/…``** (see its README) — it is shared-candidate code, so all
robovast-specific wiring lives in the consumer, ``frontend/ui/src/panels/Scene3DPanel.tsx``, which
binds the vast spec, fetches the descriptor via ``DataProvider.runFileUrl``
(``GET /results/<campaign>/<config>/<run>/<path>`` — the address is the run's real
directory, so the loader's *relative* sibling fetches, ``scene.bin``/textures, stay in
it), and drives ``basePose`` from the clock.

Deferred: web notebook execution (a server-side kernel; the desktop ``vast eval gui`` keeps that
role) and a bundle code-split (Monaco + Plotly + Vega + Module Federation make the SPA large).

.. _cluster-postprocessing:

Analysis postprocessing: local vs cluster
-----------------------------------------

``data.db`` (what the eval viewer and ``query_campaign_data_sql`` read) is produced by *analysis*
postprocessing. It splits along a natural seam: **only the batched ``rosbags_*`` → CSV step needs
ROS2**; everything after it (``generate_data_db``, metadata) is plain Python.

**What ``generate_data_db`` ingests.** One table per data file, discovered per run directory:
``*.csv``, and ``*.jsonl`` for producers that need more than a flat table can express. A JSONL
file declares its layout in the ``format`` key of its **first record**, and ``_JSONL_READERS``
maps that to the function turning its records into rows — dispatch on the file's own declaration
rather than on its name, so adding a producer does not mean hardcoding a filename in the ingest.
Rows are then typed and inserted through exactly the same path as a CSV's, which is why column
types are inferred per column and a JSONL file whose records have differing keys still gets a
column for each (the column list is the union over rows, not the first row's keys).

The one such producer today is ``behaviors.jsonl``, written by ``scenario_execution``'s
``--bt-log`` (``execution.bt_log`` in the ``.vast``). It replaced a rosbag route — recording
``/scenario_execution/snapshots`` and converting it with a ``bt_to_csv`` handler — which could
only work for ROS runs, and so left ``mode: base`` campaigns with no behaviour-tree data at all.
Since the runner writes the file itself, both kinds of run now produce the ``behaviors`` table by
the same path, and the snapshots topic no longer needs to be in the bag. The table keeps the
seven columns it always had (so ``nav2_behaviors``, see above, still shares its schema and the
same panel renders both) and adds what the JSONL carries: ``child_index``, ``type``,
``additional_detail``, ``feedback_message``, ``is_active``, ``tip_id``, ``osc_file``,
``osc_line``, ``osc_column`` and ``removed``. The numeric ``status`` column is re-derived in the
reader from the status name, because those 1–4 codes come from ``py_trees_ros_interfaces`` — plain
py_trees ``Status`` values are strings.

Both backends run the rosbag conversion **in the campaign's own execution image** — the
system-under-test's image, recorded in ``<campaign>/_execution/execution.yaml`` — because rosbags
carry the SUT's *custom ROS2 message types* and only deserialize there. On the cluster backend the
image is **pinned to the immutable digest the run pods used** (captured from the pods and stored as
``image_revision``; ``campaign_execution_image`` prefers it), so a re-postprocess months later
deserializes against the exact image the runs recorded the bags with — not whatever a floating
``:latest`` resolves to then.

**Local.** ``run_postprocessing`` reads that ``image:`` and passes it to
``docker_exec.sh --image``, which bind-mounts the campaign dir (``-v $INPUT_DIR:/input``) — so the
container writes CSVs **in place**. Nothing to sync. The **scripts** are bind-mounted from the
driver's own package (``-v $SCRIPT_DIR:/scripts:ro``, ``$SCRIPT_DIR`` =
``robovast.results_processing.data``), so the script version always matches the driver.

**Cluster.** A pod cannot bind-mount the caller's filesystem, so the conversion runs as a Job
(:mod:`robovast.execution.cluster_execution.postprocess_job`) modelled on the run Jobs:

* **image** = the campaign's execution image (never a default — a missing ``image:`` is an error,
  not a silent wrong-image conversion);
* the conversion scripts are delivered as a per-campaign **ConfigMap** built from the driver's own
  ``robovast.results_processing.data`` and mounted read-only at ``/scripts`` — the K8s analog of the
  local ``-v $SCRIPT_DIR:/scripts:ro`` bind-mount. This is deliberate: sourcing the scripts from the
  *driver* (not from a separately-versioned controller image, as an earlier design did) guarantees
  the in-cluster scripts match the driver that generates the conversion command, so an
  off-cluster/dev driver running ahead of a published image can no longer skew (the failure mode that
  produced a spurious ``--output-root`` error). The scripts are self-contained (stdlib + ROS2 libs, no
  ``robovast`` import) and small; nothing is ever baked into the user's image;
* inputs (``/bags``, mirrored from the object store) and outputs (``/out``) are **separate dirs** —
  the run-Job pattern — enabled by ``rosbags_process.py --output-root``. Its default is the input
  root, i.e. "beside the bag", so the local path is unchanged. The Job then mirrors ``/out``
  wholesale to a ``<campaign_prefix>_postproc/`` staging prefix; no rosbag is ever re-uploaded.

Stage 2 (``data.db`` + metadata) is pure Python and reuses the normal pipeline with the rosbag steps
skipped (``run_host_postprocessing``), so there is no second copy of the postprocessing sequence.

Two entry points share that one implementation (``postprocess_campaign``):

* **auto-chain** — the campaign driver, when ``create_campaign(postprocess=True)`` sets
  ``RunOptions.postprocess`` (an option, not a process-global env var, so concurrent campaigns in the
  one service process stay distinct). It runs **after ``store.close()``** (``campaign.db`` must be
  flushed — ``data.db``'s ``runs`` table reads it) and **before ``_finalize``**, so the results ride
  the campaign's existing upload rather than needing one of their own.
* **explicit re-run** — :class:`~robovast.execution.cluster_execution.cluster_service.ClusterService.run_postprocessing`,
  which overrides the ``LocalTransport`` implementation — unusable in the service, which has no local
  results root and no ROS runtime. It fetches the campaign, runs the same two stages, and publishes
  ``_execution/`` back. This backs the web **Retrigger postprocessing** dialog, the MCP
  ``run_postprocessing`` tool, and the CLI. It reads the campaign's own ``_config/<name>.vast`` (which
  the edit dialog overwrites in place — the single source of truth resolved by
  ``common.results_utils.campaign_vast``), refreshes the durable outcome (clearing/setting
  ``postprocessing_error``), and is **dispatched in the background**: both transports run it via
  ``LocalTransport._dispatch_background``, which registers a tracked campaign entry set to the
  ``postprocessing`` phase (busy-guarded against a second concurrent op) and returns at once, so the
  campaign view shows it live. A minutes-to-hours re-run therefore never blocks the caller.

The **upload-to-share** step mirrors this: a failure records ``share_error`` (durable) instead of
being swallowed, and :meth:`~robovast.execution.cluster_execution.cluster_service.ClusterService.run_share` re-triggers it
(web *Retrigger upload-to-share*, MCP ``run_share``, ``POST /campaigns/{id}/share/run``) — also via
``_dispatch_background`` (``sharing`` phase). Both re-triggers need no live in-memory campaign entry,
so they work after a service restart.

A post-run step failure is deliberately **not** a campaign failure: the phase stays ``finished`` and
the reason lives on ``postprocessing_error`` / ``share_error``. After a restart the cluster service
reconstructs a campaign's status from ``_execution/outcome.json`` in the object store, falling back to
the on-disk run artifacts (``reconstruct_status_from_disk``) when no outcome was recorded — so a
finished campaign reads as ``finished``, never a bare ``unknown``.

Querying RoboVAST campaigns
---------------------------

Using [rdflib](https://rdflib.readthedocs.io/), you can query the generated metadata graph using [SPARQL](https://www.w3.org/TR/sparql11-query/).

Load the metadata graph
^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: python

  from rdflib import Graph

  g = Graph()
  g.parse("metadata.prov.json)



Loading SPARQL queries
^^^^^^^^^^^^^^^^^^^^^^^

To do so, you can load any of the queries below as text, and use the `query` method for any graph `g`:

.. code-block:: python

  with open("query-file.rq", "r") as f:
    query_string = f.read()

  qres = g.query(query_string)
  for row in qres:
    # Process your results
    print(row)

Below are a few example queries demonstrating the PROV relationships in the metadata graph.

Scenario inputs
^^^^^^^^^^^^^^^

FloorPlan models:

.. code-block:: sparql

   SELECT ?floorplan ?creator ?date
   WHERE {
        ?floorplan rdf:type env:FloorPlanModel .
        OPTIONAL {?floorplan prov:wasAttributedTo ?creator .}
        OPTIONAL {?floorplan dcterms:modified ?date .}
  }

Vast file:

.. code-block:: sparql

    SELECT ?vast_file ?creator ?date ?abstract_scenario
    WHERE {
        ?vast_file rdf:type robovast:VastConfiguration .
        ?vast_file dcterms:references ?abstract_scenario .
        ?abstract_scenario rdf:type scenarios:AbstractScenario .
        OPTIONAL {?vast_file prov:wasAttributedTo ?creator .}
        OPTIONAL {?vast_file dcterms:modified ?date .}
    }

Generation
^^^^^^^^^^^

FloorPlan Model-to-Model Transformation:

.. code-block:: sparql

    SELECT ?floorplan ?activity ?jsonld_file ?agent
    WHERE {
        ?floorplan rdf:type env:FloorPlanModel .
        ?activity rdf:type robovast:FloorPlanTransformation .
        ?activity prov:used ?floorplan .
        ?jsonld_file prov:wasGeneratedBy ?activity .
        OPTIONAL {?activity prov:wasAssociatedWith ?agent .}
    }

FloorPlan Artefact Generation:

.. code-block:: sparql

    SELECT ?source_files ?activity ?gen_file ?agent
    WHERE {
        ?activity rdf:type robovast:FloorPlanGeneration .
        ?activity prov:used ?source_files .
        ?gen_file prov:wasGeneratedBy ?activity .
        OPTIONAL {?activity prov:wasAssociatedWith ?agent .}
    }

Generation of Concrete Scenario

.. code-block:: sparql

    SELECT ?vast_file ?ref_file ?activity ?agent ?gen_file
    WHERE {
        ?vast_file rdf:type robovast:VastConfiguration .
        ?activity prov:used ?vast_file .
        ?vast_file dcterms:references ?ref_file .
        ?gen_file prov:wasGeneratedBy ?activity .
        OPTIONAL {?activity prov:wasAssociatedWith ?agent .}

Test Execution
^^^^^^^^^^^^^^

Test results generated from a test run:

.. code-block:: sparql

    SELECT ?scenario ?config_files ?activity ?agent ?gen_file ?start_time ?end_time
    WHERE {
        ?scenario rdf:type smm:ConcreteScenario .
        ?activity prov:used ?scenario .
        ?activity prov:used ?config_files .
        OPTIONAL{?activity prov:startedAtTime ?start_time .}
        OPTIONAL{ ?activity prov:endedAtTime ?end_time . }
        ?gen_file prov:wasGeneratedBy ?activity .
        OPTIONAL {?activity prov:wasAssociatedWith ?agent .}

Postprocessing
^^^^^^^^^^^^^^

Postprocessing of a bagfile:

.. code-block:: sparql

    SELECT ?bag_file ?activity ?agent ?gen_file ?start_time ?end_time
    WHERE {
        ?bag_file rdf:type robovast:ROSBag .
        ?activity prov:used ?bag_file .
        ?gen_file prov:wasGeneratedBy ?activity .
        OPTIONAL {?activity prov:wasAssociatedWith ?agent .}
        OPTIONAL{?activity prov:startedAtTime ?start_time .}
        OPTIONAL{ ?activity prov:endedAtTime ?end_time . }
    }


Metadata and Graph postprocessing:

.. code-block:: sparql

    SELECT ?metadata_file ?graph_file ?md_activity ?graph_activity ?agent ?start_time ?end_time
    WHERE {
        ?md_activity rdf:type robovast:PostprocessingMetadata .
        ?graph_activity rdf:type robovast:PostprocessingGraph .
        ?metadata_file prov:wasGeneratedBy ?md_activity .
        ?graph_file prov:wasGeneratedBy ?graph_activity .
        ?graph_activity prov:used ?metadata_file
        OPTIONAL {?md_activity prov:wasAssociatedWith ?agent .
        ?graph_activity prov:wasAssociatedWith ?agent .}
        OPTIONAL{?md_activity prov:startedAtTime ?start_time .}
        OPTIONAL{ ?md_activity prov:endedAtTime ?end_time . }
    }

Analysis
^^^^^^^^

Identifying which variation types were used on each config:

.. code-block:: sparql

    SELECT ?config ?variation_type
    WHERE {
        ?config rdf:type smm:ConcreteScenario .
        ?config robovast:variations/rdf:rest*/rdf:first ?variation .
        ?variation rdf:type ?variation_type .
        FILTER (?variation_type != prov:Activity)
        FILTER (?variation_type != robovast:Variation)
    }

Getting the failure rate by environment:

.. code-block:: sparql

    SELECT  ?env_model (SUM(?failures)/COUNT (?activity) * 100 AS ?result) (COUNT (?activity) AS ?total)
    WHERE {
        ?conf rdf:type smm:ConcreteScenario .
        ?activity prov:used ?conf .
        ?activity rdf:type robovast:TestExecution .
        ?activity robovast:success ?success .
        BIND(IF(?success=true, 0, 1) AS ?failures) .
        ?conf dcterms:references ?env_model .
        ?env_model rdf:type env:FloorPlanModel .
    } GROUP BY ?env_model
