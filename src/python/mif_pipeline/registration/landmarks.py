"""

Checks the two saved stages of manual image registration.

The first request supplies the starting pose and image-based affine settings.
After reviewing the cluster's returned affine, the second request identifies
that exact fit and supplies the optional local landmark pairs. Their original
full-resolution coordinates are retained for physical-coordinate TPS fitting.

"""

from __future__ import annotations

from collections.abc import Mapping
import math
from pathlib import PurePosixPath
import re

from .automatic import AutomaticAlignmentSettings


def _point_pairs(moving, fixed, *, moving_size, fixed_size, minimum, label):
    """

    Checks the original-image point pairs used for local spline fitting.

    Each position contains two finite coordinates in its original image. We
    check those coordinates against that image's width and height, then require
    equal numbers of moving and fixed points. Repeated positions cannot add an
    independent constraint and are rejected within either coordinate list.

    Args:
        moving, fixed: Ordered coordinate lists for the paired and mIF images.
        moving_size, fixed_size: Original image widths and heights in pixels.
        minimum: Minimum number of complete pairs required by this fitting step.
        label: Global or local step name used when explaining an invalid entry.

    Returns:
        Two independent coordinate lists in their original pair order.

    Raises:
        ValueError: Points are incomplete, repeated, nonfinite or outside their
            image, or fewer than the required number of pairs are supplied.

    """

    checked = []
    for points, size in ((moving, moving_size), (fixed, fixed_size)):
        if not isinstance(points, (list, tuple)) or not minimum <= len(points) <= 256:
            raise ValueError(f"Provide {minimum} to 256 {label} landmark pairs.")
        rows = []
        for point in points:
            if (
                not isinstance(point, (list, tuple))
                or len(point) != 2
                or any(
                    type(value) not in (int, float)
                    or not math.isfinite(value)
                    or not 0 <= value <= limit
                    for value, limit in zip(point, size)
                )
            ):
                raise ValueError(f"A {label} landmark is outside its image or has invalid coordinates.")
            rows.append(list(point))
        if len(set(map(tuple, rows))) != len(rows):
            raise ValueError(f"Place each {label} landmark at a different location.")
        checked.append(rows)
    if len(checked[0]) != len(checked[1]):
        raise ValueError(f"Each {label} landmark needs a matching point in the other image.")
    return checked[0], checked[1]


def validate_manual_alignment(value, *, fixed_size, moving_size):
    """

    Checks a pose/affine request or a final choice tied to a returned global fit.

    Global input contains finite physical shifts, rotation, uniform scale and
    the complete image-fitting settings. Final input identifies the reviewed
    affine by its session ID and transform checksum. Refinement requires six
    to 256 original-image pairs; retaining the affine requires empty point lists.
    We return independent normalized values for exact request comparison.

    Args:
        value: Current manual-alignment/v1 global or final request dictionary.
        fixed_size: Positive integer width and height of the mIF image in pixels.
        moving_size: Positive integer width and height of the paired image in pixels.

    Returns:
        Complete dictionary ready for the selected native fitting stage.

    Raises:
        ValueError: The stage, exact field set, image dimensions, pose, settings,
            fit reference or local point/displacement values are invalid.

    """

    for size in (fixed_size, moving_size):
        if not isinstance(size, (list, tuple)) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
            raise ValueError("Both registration images need their original width and height.")
    if not isinstance(value, Mapping) or value.get("schema") != "manual-alignment/v1":
        raise ValueError("Save the current manual alignment request from the registration editor.")
    stage = value.get("stage")
    if stage == "global":
        fields = {"schema", "stage", "starting_pose", "use_starting_pose", "global_settings", "tools_directory"}
        if set(value) != fields:
            raise ValueError("Save the complete starting pose and image-fitting settings.")
        pose = value["starting_pose"]
        pose_fields = {"x_um", "y_um", "rotation_degrees", "scale"}
        if (
            not isinstance(pose, Mapping) or set(pose) != pose_fields
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in pose.values())
            or not .01 < pose["scale"] <= 100
            or type(value["use_starting_pose"]) is not bool
        ):
            raise ValueError("Enter finite starting shifts/rotation and a uniform scale greater than 0.01 and at most 100.")
        settings = value["global_settings"]
        setting_fields = {"fixed_channel", "moving_channel", "max_dimension", "iterations", "threads", "orientation_search"}
        if not isinstance(settings, Mapping) or set(settings) != setting_fields:
            raise ValueError("Save all six global affine fitting settings.")
        checked = AutomaticAlignmentSettings.from_mapping({**settings, "local_refinement": False})
        tools = value["tools_directory"]
        if tools is not None and (
            not isinstance(tools, str) or not tools or tools != tools.strip()
            or not PurePosixPath(tools).is_absolute() or ".." in tools.split("/")
            or any(ord(character) < 32 for character in tools)
        ):
            raise ValueError("Use an absolute cluster path for Elastix, or leave it empty to use cluster Software setup.")
        return {
            "schema": "manual-alignment/v1", "stage": "global",
            "starting_pose": {key: float(pose[key]) for key in ("x_um", "y_um", "rotation_degrees", "scale")},
            "use_starting_pose": value["use_starting_pose"],
            "global_settings": {key: getattr(checked, key) for key in sorted(setting_fields)},
            "tools_directory": tools,
        }

    fields = {
        "schema", "stage", "global_fit_id", "global_transform_sha256", "local_refinement",
        "local_moving_points", "local_fixed_points", "maximum_displacement_um",
    }
    if stage != "final" or set(value) != fields:
        raise ValueError("Save the current global fitting request or the final local-refinement choice.")
    if any(not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key])
           for key in ("global_fit_id", "global_transform_sha256")):
        raise ValueError("Review the returned global affine before saving the final alignment.")
    if type(value["local_refinement"]) is not bool:
        raise ValueError("Select whether the global affine needs local landmark refinement.")
    limit = value["maximum_displacement_um"]
    if type(limit) not in (int, float) or not math.isfinite(limit) or not 0 < limit <= 1e6:
        raise ValueError("Enter a positive maximum local displacement of at most 1,000,000 µm.")

    if value["local_refinement"]:
        local_moving, local_fixed = _point_pairs(
            value["local_moving_points"], value["local_fixed_points"],
            moving_size=moving_size, fixed_size=fixed_size, minimum=6, label="local",
        )
    else:
        if value["local_moving_points"] not in ([], ()) or value["local_fixed_points"] not in ([], ()):
            raise ValueError("Global-only registration must save empty local landmark lists.")
        local_moving, local_fixed = [], []
    return {
        "schema": "manual-alignment/v1", "stage": "final",
        "global_fit_id": value["global_fit_id"],
        "global_transform_sha256": value["global_transform_sha256"],
        "local_refinement": value["local_refinement"],
        "local_moving_points": local_moving, "local_fixed_points": local_fixed,
        "maximum_displacement_um": float(limit),
    }
