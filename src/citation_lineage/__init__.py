"""档案引用谱系 API."""
from .consolidation import (
    ConsolidationError,
    ConsolidationLimitError,
    consolidate,
)
from .disposition import DispositionError, assess_disposition
from .handoff import HandoffError, manifest_sha256, prepare_handoff
from .revise import RevisionError, revise
from .service import process
from .snapshot import MaterializationError, materialize

__all__ = [
    'process',
    'prepare_handoff',
    'HandoffError',
    'manifest_sha256',
    'revise',
    'RevisionError',
    'consolidate',
    'ConsolidationError',
    'ConsolidationLimitError',
    'materialize',
    'MaterializationError',
    'assess_disposition',
    'DispositionError',
]
