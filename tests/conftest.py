"""Suite-wide isolation.

The facet registry reads a system facets file once, at import
(crib.facets). Point it at nothing before crib is imported, so a facets file on
the developer's machine can never add facets to the suite; tests that declare
facets run crib in a subprocess with their own file.
"""

from __future__ import annotations

import os

os.environ["CRIB_FACETS_FILE"] = os.path.join(os.path.dirname(__file__), "no-facets-file")
