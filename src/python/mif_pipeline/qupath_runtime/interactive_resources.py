"""Per-process viewing budgets; no persisted QuPath preference is changed."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from collections.abc import Mapping
import re
from mif_pipeline._capacity import available_memory_bytes
from mif_pipeline.pipeline_core.execution_resources import native_library_environment

# ==============================================================================
#                             Interactive resources
# ==============================================================================


def interactive_environment(
    environment: Mapping[str, str], *, available_bytes: int | None = None
) -> dict[str, str]:
    result = native_library_environment(environment)
    available = available_memory_bytes() if available_bytes is None else available_bytes
    if available is not None and available < 1536 * 1024**2:
        raise ValueError(
            "There is not enough available memory to open this image view. Wait for current processing to finish, or close unused image windows or other memory-intensive applications, then reopen the view."
        )
    current = result.get("JAVA_TOOL_OPTIONS", "").strip()
    all_options = " ".join(
        result.get(name, "") for name in ("JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS")
    )
    additions = []
    if re.search(r"(?:^|\s)(?:-Xmx|-XX:MaxHeapSize=|-XX:MaxRAMPercentage=)", all_options) is None:
        # Leave at least half of currently available RAM for native tile buffers,
        # OpenCV and continued desktop activity. This is a viewing budget only.
        heap_mib = 1024 if available is None else min(2048, (available // (2 * 1024**2) // 64) * 64)
        additions.append(f"-Xmx{heap_mib}m")
    if "-XX:ActiveProcessorCount=" not in all_options:
        additions.append("-XX:ActiveProcessorCount=2")
    result["JAVA_TOOL_OPTIONS"] = " ".join([current, *additions]).strip()
    result.setdefault("OMP_NUM_THREADS", "2")
    result.setdefault("OPENBLAS_NUM_THREADS", "2")
    return result
