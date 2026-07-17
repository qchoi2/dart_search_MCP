"""Optional permanent-index decision gate.

The application intentionally does not build or activate a permanent index until
measured usage shows both recurring pressure and a verified benefit.
"""

from .gate import IndexNeedEvidence, IndexRecommendation, evaluate_index_need

__all__ = ["IndexNeedEvidence", "IndexRecommendation", "evaluate_index_need"]
