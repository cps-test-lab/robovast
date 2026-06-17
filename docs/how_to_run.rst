How to run
==========

.. click:: robovast.configuration.configuration_utils.cli:configuration
   :prog: vast config
   :nested: full
   :commands: generate, variation-points, variation-types, list


.. click:: robovast.execution.execution_utils.cli:execution
   :prog: vast exec
   :nested: full
   :commands: local, cluster

.. click:: robovast.results_processing.cli:results
   :prog: vast results
   :nested: full
   :commands: postprocess, merge-campaigns, postprocess-commands

.. click:: robovast.evaluation.result_analyzer.cli:evaluation
   :prog: vast eval
   :nested: full
   :commands: gui


Environment variables
---------------------

``ROBOVAST_INSECURE_SSL``
    Set to ``1`` to disable TLS certificate verification for remote fetches.
    This allows the CLI to continue when a remote host presents an invalid
    certificate. Use only with hosts you trust.
