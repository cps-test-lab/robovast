"""robovast_manipulation — manipulation-domain PROV-O support for robovast.

Provides the canonical namespace URI/prefix and the ManipulationVariation
plugin that contributes manipulation-specific PROV-O nodes to campaign
provenance graphs.
"""

MANIPULATION_NS_URI = "https://purl.org/robovast/manipulation/"
MANIPULATION_NS_PREFIX = "manipulation"

from robovast_manipulation.manipulation_variation import ManipulationVariation  # noqa: E402

__all__ = ["MANIPULATION_NS_URI", "MANIPULATION_NS_PREFIX", "ManipulationVariation"]
