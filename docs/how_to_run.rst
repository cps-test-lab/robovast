How to run
==========

.. click:: robovast.configuration.configuration_utils.cli:configuration
   :prog: vast configuration
   :nested: full
   :commands: generate, variation-points, variation-types, list


.. click:: robovast.execution.execution_utils.cli:execution
   :prog: vast execution
   :nested: full
   :commands: local, cluster

.. click:: robovast.analysis.result_analyzer.cli:data
   :prog: vast data
   :nested: full
   :commands: postprocess, merge-results, postprocess-commands

.. click:: robovast.analysis.result_analyzer.cli:evaluation
   :prog: vast evaluation
   :nested: full
   :commands: gui


