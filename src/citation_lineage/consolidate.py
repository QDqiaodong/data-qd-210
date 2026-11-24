"""Compatibility module for the ``consolidate`` API.

The implementation lives in :mod:`citation_lineage.consolidation`; this module
lets callers import the function from either spelling.
"""

from .consolidation import (
    ConsolidationError,
    ConsolidationLimitError,
    consolidate,
)

__all__ = ["consolidate", "ConsolidationError", "ConsolidationLimitError"]
