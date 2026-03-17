from hydra.core.config_search_path import ConfigSearchPath
from hydra.plugins.search_path_plugin import SearchPathPlugin


class RobovastSearchPathPlugin(SearchPathPlugin):
    """Adds robovast's built-in conf directory to Hydra's search path.

    This lets user configs reference ``robovast_common`` (and other
    built-in config groups) via the defaults list without any manual
    search-path setup::

        defaults:
          - robovast_common
          - _self_
    """

    def manipulate_search_path(self, search_path: ConfigSearchPath) -> None:
        search_path.append(
            provider="robovast",
            path="pkg://robovast.conf",
        )
