.. _setup:

Setup
=====

Installation
------------

RoboVAST is best installed within a virtual environment.

.. code-block:: bash

   sudo apt install python3-venv
   python3 -m venv venv
   . venv/bin/activate

Clone the RoboVAST repository:

.. code-block:: bash

   git clone https://github.com/cps-test-lab/robovast.git
   cd robovast

Install RoboVAST and its sibling packages in editable mode:

.. code-block:: bash

   pip install -e .
   pip install -e src/robovast_nav
   pip install -e src/robovast_sim_roqsim
   pip install -e src/robovast_cluster     # only if you will drive a Kubernetes cluster
   pip install -e src/robovast_client      # LAST -- see below

Order matters, and ``robovast-client`` last is the part that surprises. It is a
**non-optional path dependency** of ``robovast``, so ``pip install -e .`` resolves it and
installs a plain *copy* into ``site-packages`` — silently replacing an editable install done
earlier. Editing ``src/robovast_client`` then has no effect, with nothing said. Installing it
after everything that depends on it is what makes the editable install the one that survives.

The others go in the order shown because each depends on ``robovast`` and never the reverse —
which is what keeps the dependency graph acyclic. ``poetry install`` at the root will **not**
give you ``robovast-cluster``: it is a separate distribution built on this one, not an extra of it.

``pip install -e src/robovast_client`` alone is a complete, supported install — a ``vast`` that
can log in, push workspaces, have the service build their images, wait for campaigns and fetch
results, in 13 packages and about 30 MB. Every verb it offers only *drives* a service; nothing
it can run needs a simulator, Docker or a kubeconfig.

Adding ``pip install -e .`` gives you the core: configuration and variation, results
processing, the MCP server and the service's own code, with no Kubernetes client anywhere
in the environment. It runs no campaign by itself — the service implementation is
``robovast-cluster``, its own distribution, and a core without it says so when asked to
serve. Declining it is a supported setup for everything that is not a service: ``vast
doctor`` reports ``cluster support: not installed`` as a warning, and every verb that
drives a service still works.

This will install the ``vast`` command and all its plugins.

.. note::

   ``make venv`` from a checkout does all of the above in the right order. Prefer it — a missing
   sibling shows up as an *entry point* that is absent rather than an import error, which reads as
   broken code rather than an incomplete environment.

The web UI and the navigation panels are not built by ``pip``. For a source checkout,
build them once with ``make frontend`` — a service started from the checkout then finds
them there and picks up every rebuild. A wheel carries them instead: ``make build`` runs
``make ui-stage`` first, which copies the built assets into the package so an installed
service has a UI. Without either, the service warns and serves the API alone.

The same set is on PyPI, for a machine that will not edit the code. The released wheels
carry the frontend, and each name adds exactly what its editable counterpart above does:

.. code-block:: bash

   pip install robovast-client              # drive a service: the CLI alone
   pip install "robovast[nav,roqsim]"       # the core: config, results, the service's code
   pip install robovast-cluster             # the service implementation: deploy and run it

The ``vast`` command provides a unified interface to all RoboVAST functionality.

.. code-block:: bash

   vast --help

   # enable shell completions
   vast install-completion
   source ~/.bashrc  # or source the appropriate file for your shell

To be able to execute tests in a kubernetes cluster, execute the following command to install the required dependencies:

.. code-block:: bash

   # get available cluster configs
   vast cluster setup --list 
   
   # setup cluster with given config
   vast cluster setup <cluster-config>

Dependencies
------------

Kubernetes
^^^^^^^^^^

Campaigns run on Kubernetes. A one-node cluster on your own machine (minikube, kind) is enough to evaluate and develop with; parallel execution needs more nodes.

Either follow some Kubernetes Distribution Setup Instructions to set up your own cluster, e.g.

- RKE2: `RKE2 Quick Start Guide <https://docs.rke2.io/install/quickstart>`_
- K3S: `K3S Quick Start Guide <https://docs.k3s.io/quick-start>`_
- Kubespray: `kubespray Quick Start Guide <https://kubespray.io/>`_

or use a managed Kubernetes service, e.g.,

- AWS EKS: `Getting Started with Amazon EKS <https://docs.aws.amazon.com/eks/latest/userguide/getting-started.html>`_
- Azure AKS: `Quickstart: Create an AKS cluster using the Azure portal <https://learn.microsoft.com/en-us/azure/aks/kubernetes-walkthrough-portal>`_
- GCP GKE: `Quickstart for GKE <https://docs.cloud.google.com/kubernetes-engine/docs/concepts/kubernetes-engine-overview>`_

For single-node testing and debugging, we recommend minikube.
Follow the instructions here: `minikube Installation Guide <https://minikube.sigs.k8s.io/docs/start/>`_ or this short summary:

.. code-block:: bash

   # install minikube.
   curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
   sudo install minikube-linux-amd64 /usr/local/bin/minikube && rm minikube-linux-amd64
   minikube start --extra-config=kubelet.housekeeping-interval=10s

   # enable container registry
   minikube addons enable registry

   # install k9s
   wget https://github.com/derailed/k9s/releases/latest/download/k9s_linux_amd64.deb
   sudo dpkg -i k9s_linux_amd64.deb
   rm k9s_linux_amd64.deb

   # install kubectl
   curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
   sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
   source /usr/share/bash-completion/bash_completion
   echo 'source <(kubectl completion bash)' >>~/.bashrc
   echo 'alias k=kubectl' >>~/.bashrc
   echo 'complete -o default -F __start_kubectl k' >>~/.bashrc
   source ~/.bashrc

.. note::

   By default, minikube does not encapsulate network communication by default. Communication between ROS2 nodes across tests might happen. Therefore ensure, that only a single run is executed at a time.

After setup, the following command should show the cluster information:

.. code-block:: bash

   kubectl cluster-info

For debugging and monitoring, we recommend installing `k9s <https://k9scli.io/>`_.

GPU nodes (optional)
""""""""""""""""""""

Only needed to render simulation cameras in hardware; campaigns run without it, in software.
On each GPU node install the NVIDIA driver and the `NVIDIA container toolkit
<https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html>`_
— the toolkit is what allows a container to be handed the device at all. RKE2 and k3s
register a ``nvidia`` RuntimeClass once it is present, which is what RoboVAST looks for:

.. code-block:: bash

   kubectl get runtimeclass          # a 'nvidia' entry means the node is ready

Everything above the host — making the GPU schedulable and requesting it per job — is done by
``vast cluster setup``. See :ref:`cluster-gpu`.


Docker
^^^^^^

Docker is needed on a development machine to build the container images, and by ``vast configuration generate`` when a variation composes in a helper image; a campaign's containers run in the cluster.

Follow the instructions here: `Docker Installation Guide <https://docs.docker.com/engine/install/>`_ or this short summary:

.. code-block:: bash

   sudo apt-get update
   sudo apt-get install \
       ca-certificates \
       curl \
       gnupg \
       lsb-release

   curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg

   echo \
     "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu \
     $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

   sudo apt-get update
   sudo apt-get install docker-ce docker-ce-cli containerd.io

   sudo usermod -aG docker $USER
   newgrp docker

   docker --version
