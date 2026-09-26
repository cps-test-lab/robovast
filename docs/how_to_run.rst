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

.. `vast cluster` and `vast service` both attach operator verbs lazily, from
   robovast-cluster through an entry point: `setup`, `cleanup`, `jobs-cleanup` and
   `monitor` on the first, `upgrade` and `token` on the second. sphinx-click reads a
   group's eager ``commands`` dict rather than ``list_commands()``, so a group holding
   even one eager verb renders ONLY that half and drops every lazy one **silently**: the
   section appears, the page builds clean, and the operator verbs are simply absent.

   `vast cluster` holds none of its own, so its directive below renders the whole group.
   `vast service` holds `log`, `restart`, `info`, `resources`, `cache` and `mcp-stats`,
   so its two lazy verbs get a directive each, from the module that defines them. Adding
   an eager verb to `vast cluster` would silently drop its four — give each one a
   directive here if that ever happens.

.. click:: robovast.client.cluster_cli:cluster
   :prog: vast cluster
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

.. click:: robovast.client.cli:files
   :prog: vast files
   :nested: full

.. click:: robovast.client.cli:image
   :prog: vast image
   :nested: full

.. click:: robovast.execution.share_cli:share
   :prog: vast share
   :nested: full

.. click:: robovast.results_processing.cli:results
   :prog: vast results
   :nested: full


Environment variables
---------------------

``ROBOVAST_INSECURE_SSL``
    Set to ``1`` to disable TLS certificate verification for remote fetches.
    This allows the CLI to continue when a remote host presents an invalid
    certificate. Use only with hosts you trust.
