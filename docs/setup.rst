
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

Order matters: each sibling depends on ``robovast``, never the other way round, which is what
keeps the dependency graph acyclic. ``poetry install`` at the root will **not** pull them in for
the same reason — they are separate distributions built on this one, not extras of it.

``pip install -e .`` alone gives you a working ``vast`` and a service that can run **local Docker
campaigns**, with no Kubernetes client anywhere in the environment. That is the point of the split,
so declining the cluster package is a supported setup rather than a broken one: ``vast exec`` simply
lists ``local`` and not ``cluster``, and ``vast doctor`` reports ``cluster support: not installed``
as a warning. Add ``robovast-cluster`` when you need the Kubernetes lane, and ``vast serve
--backend cluster`` starts working.

This will install the ``vast`` command and all its plugins.

.. note::

   ``make venv`` from a checkout does all of the above in the right order. Prefer it — a missing
   sibling shows up as an *entry point* that is absent rather than an import error, which reads as
   broken code rather than an incomplete environment.

The web UI is not built by ``pip``. For a source checkout, build it once with
``cd frontend/ui && npm ci && npm run build`` — ``vast serve`` then finds it there and
picks up every rebuild. A wheel carries it instead: ``make build`` runs ``make ui-stage``
first, which copies the built assets into the package so an installed service has a UI.
Without either, ``vast serve`` warns and serves the API alone.
The ``vast`` command provides a unified interface to all RoboVAST functionality.

.. code-block:: bash

   vast --help

   # enable shell completions
   vast install-completion
   source ~/.bashrc  # or source the appropriate file for your shell

To be able to execute tests in a kubernetes cluster, execute the following command to install the required dependencies:

.. code-block:: bash

   # get available cluster configs
   vast execution cluster setup --list 
   
   # setup cluster with given config
   vast execution cluster setup <cluster-config>

Dependencies
------------

Kubernetes
^^^^^^^^^^

Local execution is possible, but RoboVAST's full capabilities—parallel, cluster-based run execution—require Kubernetes.

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


Docker
^^^^^^

RoboVAST uses Docker containers, e.g. for local run execution.

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
