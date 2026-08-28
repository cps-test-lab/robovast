How to run
==========

.. click:: robovast.client.cli:workspace
   :prog: vast workspace
   :nested: full

.. click:: robovast.client.campaign_cli:campaign
   :prog: vast campaign
   :nested: full

.. click:: robovast.configuration.configuration_utils.cli:configuration
   :prog: vast config
   :nested: full
   :commands: generate, variation-points, variation-types, list

.. click:: robovast.client.container_cli:container
   :prog: vast container
   :nested: full

.. `vast cluster` and `vast service` each hold BOTH eagerly-defined verbs and lazily
   attached ones — `store-cleanup` and `restart` are in the group's own module, `setup` and
   `upgrade` arrive from robovast-cluster through an entry point. sphinx-click reads the
   eager ``commands`` dict rather than ``list_commands()``, so a group holding even one
   eager verb renders ONLY that half and drops every lazy one **silently**: the section
   appears, the page builds clean, and six operator verbs are simply absent. Verified, not
   assumed — the first draft of this file lost them exactly that way.

   So each operator verb is documented from the module that defines it, as its own
   directive. Tedious, and the alternative is worse: an undocumented verb nobody notices.

.. click:: robovast.client.cluster_cli:cluster
   :prog: vast cluster
   :nested: full

.. click:: robovast.execution.cluster_execution.cli:setup
   :prog: vast cluster setup
   :nested: full

.. click:: robovast.execution.cluster_execution.cli:cleanup
   :prog: vast cluster cleanup
   :nested: full

.. click:: robovast.execution.cluster_execution.cli:run_cleanup
   :prog: vast cluster jobs-cleanup
   :nested: full

.. click:: robovast.execution.cluster_execution.cli:monitor
   :prog: vast cluster monitor
   :nested: full

.. click:: robovast.client.service_cli:service
   :prog: vast service
   :nested: full

.. click:: robovast.execution.cluster_execution.cli:upgrade
   :prog: vast service upgrade
   :nested: full

.. click:: robovast.execution.cluster_execution.cli:cluster_token
   :prog: vast service token
   :nested: full

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
