"""Read-only dependency-readiness hints for the current task graph."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations

# ==============================================================================
#                            Batch workflow readiness
# ==============================================================================


def pending_response_node_ids(plan, head) -> frozenset[str]:
    # Include newly materialized tasks, rather than only the previous scheduler's
    # next-work list. These dependency hints never authorize a launch; ordinary
    # admission also reopens exact inputs, process state and resource capacity.
    from .batch_scheduler import BatchScheduler

    return frozenset(
        node.node_id for node in plan.nodes
        if node.node_id not in head.invalidated_node_ids
        and BatchScheduler._ordinary_status_ready(node, head)
        and BatchScheduler._dependencies_ready(node, head)
    )
