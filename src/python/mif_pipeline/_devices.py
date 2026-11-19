"""Select an available PyTorch device for a configured numerical operation.

The selection checks the requested CPU, CUDA or Metal preference against the runtime
capabilities and the method's supported alternatives. It returns the chosen device together
with the decision needed for reporting; the estimator configuration and fitted parameters
remain with the scientific method."""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
import os
import platform

# ==============================================================================
#                                    Devices
# ==============================================================================


@dataclass(frozen=True, slots=True)
class DeviceDecision:
    method: str
    device: str
    reason: str


def runtime_capabilities(*, torch=None):
    """Check the tensor runtime on the computer executing this function.

    We perform a small matrix multiplication for each available accelerator;
    importing a CUDA build alone does not establish that its driver works.
    The report concerns this runtime, not a model's supported operations or
    the separate native engine. Cluster callers run this check in an allocation.
    """
    report = {"host": platform.node(), "platform": platform.system(), "torch": None,
              "cuda_build": None, "processors": [], "error": None}
    if torch is None:
        try:
            import torch
        except (ImportError, OSError) as error:
            report["error"] = str(error)
            return report
    report["torch"] = str(torch.__version__)
    report["cuda_build"] = torch.version.cuda
    for device in ("cpu", "cuda", "mps"):
        item = {"device": device, "available": False, "detail": ""}
        try:
            if device == "cuda" and not torch.cuda.is_available():
                item["detail"] = (
                    "CUDA libraries are installed, but no usable NVIDIA GPU was found. Check the driver and GPU allocation."
                    if report["cuda_build"] else
                    "This installation has CPU tensor libraries. Use the NVIDIA edition or install its pinned CUDA dependencies."
                )
            elif device == "mps" and not torch.backends.mps.is_available():
                item["detail"] = "Apple Metal is unavailable on this computer."
            else:
                with torch.inference_mode():
                    value = torch.ones((2, 2), dtype=torch.float32, device=device)
                    if float((value @ value).sum().cpu().item()) != 8.0:
                        raise RuntimeError("The processor arithmetic check returned an unexpected result.")
                item["available"] = True
                item["detail"] = "Matrix multiplication passed."
                if device == "cuda":
                    item["name"] = str(torch.cuda.get_device_name())
                    item["free_bytes"], item["total_bytes"] = map(int, torch.cuda.mem_get_info())
        except (RuntimeError, OSError) as error:
            item["detail"] = str(error)
        report["processors"].append(item)
    return report


def select_torch_device(
    method, *, preference=None, required_bytes=0, torch=None, precision_policy="float64"
):
    if precision_policy not in {"float64", "float32_clipped", "float32_nnls"}:
        raise ValueError("Choose float64 or 32-bit clipped least squares")
    choice = preference or os.environ.get("MIF_COMPUTE_DEVICE", "auto")
    if choice not in {"auto", "cpu", "cuda", "mps"}:
        raise ValueError("Choose automatic, CPU, NVIDIA CUDA or Apple Metal processing")
    if type(required_bytes) is not int or required_bytes < 0:
        raise ValueError("The device-memory estimate is invalid")
    if method not in {
        "unmixing",
        "kronos",
        "gnn",
        "nimbus",
    }:
        raise ValueError("The method has no registered device policy")
    if method == "nimbus" and choice == "mps":
        raise ValueError("Nimbus has not been verified with Apple Metal. Select CPU processing for Nimbus.")
    if choice == "cpu":
        return DeviceDecision(method, "cpu", "CPU selected explicitly.")
    if torch is None:
        try:
            import torch
        except ImportError:
            if choice != "auto":
                raise ValueError(
                    "Install a compatible PyTorch runtime in this processing environment to use the selected device"
                )
            return DeviceDecision(
                method,
                "cpu",
                "No compatible tensor runtime is installed in this processing environment.",
            )
    cuda = bool(torch.cuda.is_available())
    metal = bool(torch.backends.mps.is_available())
    if choice in {"auto", "cuda"} and cuda:
        free, _ = torch.cuda.mem_get_info()
        if required_bytes <= int(free * 0.75):
            return DeviceDecision(
                method, "cuda", "NVIDIA CUDA is available and the estimated working memory fits."
            )
        if choice == "cuda":
            raise ValueError(
                "The selected job exceeds currently available GPU memory; reduce its working set or choose CPU processing"
            )
    if (
        choice in {"auto", "mps"}
        and metal
        and method != "nimbus"
        and (method != "unmixing" or precision_policy in {"float32_clipped", "float32_nnls"})
    ):
        return DeviceDecision(
            method,
            "mps",
            (
                "Apple Metal is available for the selected 32-bit spectral solver."
                if method == "unmixing"
                else "Apple Metal is available for this model."
            ),
        )
    if choice == "mps" and method == "unmixing" and precision_policy == "float64":
        raise ValueError(
            "The released unmixing calculation uses 64-bit arithmetic, which the Metal backend does not support"
        )
    if choice != "auto":
        raise ValueError("The requested accelerator is unavailable in this processing environment")
    reason = (
        "Unmixing preserves 64-bit arithmetic; Apple Metal does not support that precision."
        if metal and method == "unmixing"
        else "No supported accelerator with sufficient memory is available; CPU processing is selected."
    )
    return DeviceDecision(method, "cpu", reason)
