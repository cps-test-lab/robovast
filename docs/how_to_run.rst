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

.. click:: robovast.analysis.result_analyzer.cli:results
   :prog: vast results
   :nested: full
   :commands: postprocess, merge-results, postprocess-commands

.. click:: robovast.analysis.result_analyzer.cli:evaluation
   :prog: vast eval
   :nested: full
   :commands: gui


