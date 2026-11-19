"""Pure, immutable projections consumed by desktop presentation adapters."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from .plan import BatchPlanView, build_batch_plan_view

__all__ = ["BatchPlanView", "build_batch_plan_view"]
