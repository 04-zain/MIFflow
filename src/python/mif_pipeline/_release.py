"""

Identifies the application release and message formats used by numerical workers.

A worker is a separate Python process that performs calculations requested by the
application. Before exchanging numerical tasks, the two processes compare their
release and protocol identifiers. This catches installations that expect different
calculations or message fields.

Update the release identifier when numerical behavior changes, and the relevant
protocol identifier when message fields or framing change. The runtime executable
is checked before launch; the running worker reports its identifiers during startup.
"""

from __future__ import annotations

# ==============================================================================
#                           Runtime compatibility
# ==============================================================================

RELEASE_ID = "mif-pipeline-prototype-7"
WORKER_PROTOCOL = "mif-numerical-offload/v2"
KRONOS_PROTOCOL = "mif-kronos-worker/v3"

