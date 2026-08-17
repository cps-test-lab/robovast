How to run
==========

.. click:: robovast.configuration.configuration_utils.cli:configuration
   :prog: vast config
   :nested: full
   :commands: generate, variation-points, variation-types, list


.. Three directives rather than one, because sphinx-click only falls through to a group's
   lazily-attached commands when its eager ``commands`` dict is *empty* — so a single
   ``:commands: local, cluster`` on ``vast exec`` silently rendered neither (both arrive
   through entry points, from ``robovast`` and ``robovast-client`` respectively). Each is
   documented from the module that defines it instead.

.. click:: robovast.client.exec_cli:execution
   :prog: vast exec
   :nested: full
   :commands: command, stop-container

.. click:: robovast.execution.execution_utils.cli:local
   :prog: vast exec local
   :nested: full

.. click:: robovast.client.cluster_cli:cluster
   :prog: vast exec cluster
   :nested: full
   :commands: run, stop, stop-job, log, download-cleanup

.. click:: robovast.results_processing.cli:results
   :prog: vast results
   :nested: full
   :commands: postprocess, merge-campaigns, postprocess-commands


Environment variables
---------------------

``ROBOVAST_INSECURE_SSL``
    Set to ``1`` to disable TLS certificate verification for remote fetches.
    This allows the CLI to continue when a remote host presents an invalid
    certificate. Use only with hosts you trust.
