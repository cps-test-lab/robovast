
Setup
=====


Prerequisites
-------------

Kubernetes
^^^^^^^^^^

Local execution is possible, but RoboVAST's full capabilities—parallel, cluster-based test execution—require Kubernetes.

Either follow some Kubernetes Distribution Setup Instructions to set up your own cluster, e.g.

- RKE2: `RKE2 Quick Start Guide <https://docs.rke2.io/install/quickstart>`_
- K3S: `K3S Quick Start Guide <https://docs.k3s.io/quick-start>`_
- Kubespray: `kubespray Quick Start Guide <https://kubespray.io/>`_

For single-node testing and debugging, we recommend minikube.
Follow the instructions here: `minikube Installation Guide <https://minikube.sigs.k8s.io/docs/start/>`_

Or use a managed Kubernetes service, e.g.,

- AWS EKS: `Getting Started with Amazon EKS <https://docs.aws.amazon.com/eks/latest/userguide/getting-started.html>`_
- Azure AKS: `Quickstart: Create an AKS cluster using the Azure portal <https://learn.microsoft.com/en-us/azure/aks/kubernetes-walkthrough-portal>`_
- GCP GKE: `Quickstart for GKE <https://docs.cloud.google.com/kubernetes-engine/docs/concepts/kubernetes-engine-overview>`_

.. note::

   By default, minikube does not encapsulate network communication by default. Communication between ROS2 nodes across tests might happen.

After setup, the following command should show the cluster information:

.. code-block:: bash

   kubectl cluster-info

For debugging and monitoring, we recommend installing `k9s <https://k9scli.io/>`_.

Installation
------------

RoboVAST uses `Poetry <https://python-poetry.org/docs/>`_ for dependency management and packaging.

Install poetry, e.g. in ubuntu with

.. code-block:: bash

   sudo apt install python3-poetry


Then clone the RoboVAST repository

.. code-block:: bash

   git clone https://github.com/cps-test-lab/robovast.git

and install with

.. code-block:: bash

   poetry install

This will install the ``vast`` command and all its plugins.
The ``vast`` command provides a unified interface to all RoboVAST functionality.

.. code-block:: bash

   vast --help

   # enable shell completions
   vast install-completion
   source ~/.bashrc  # or source the appropriate file for your shell
