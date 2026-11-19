"""

Reads the installed software descriptions used by setup and prerequisite checks.

We keep one JSON file per supported program under config/software. Each file
records the selected release and the downloads or installed files that we check
for that program. Method parameters, such as the B-spline grid spacing or cell
segmentation threshold, are read separately from config/defaults.

The program-specific setup code checks the contents of its description before
using its paths and checksums. This module reads the installed JSON with fixed
size and syntax checks and returns an independent copy to each caller. Setup
then selects a local installation and checks its files and required program
execution before reporting that component as ready.

"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib import resources
import json
import math

_PROGRAMS = frozenset({"bioformats", "biolinkbert", "cellpose", "elastix", "instanseg", "java", "nimbus", "pytorch", "qupath", "stardist", "warpy"})
_MAX_DESCRIPTION_BYTES = 256 * 1024


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """

    Builds one JSON object while requiring each field name to occur once.

    Args:
        pairs: Ordered field/value pairs supplied by the JSON decoder.

    Returns:
        Dictionary containing the original fields and values.

    Raises:
        ValueError: A field repeats and would replace an earlier value.
    """

    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"The software description repeats the field {name!r}.")
        result[name] = value
    return result


def _finite_float(text: str) -> float:
    """

    Decodes a JSON decimal and rejects values that exceed finite floating-point storage.

    Args:
        text: Numerical text supplied by the JSON decoder, including any exponent.

    Returns:
        Finite floating-point value.

    Raises:
        ValueError: The number overflows to infinity or is otherwise nonfinite.
    """

    value = float(text)
    if not math.isfinite(value):
        raise ValueError("The software description contains a nonfinite number.")
    return value


def _reject_constant(text: str) -> None:
    """

    Rejects NaN and infinity spellings that are outside the saved JSON format.

    Args:
        text: Constant encountered by the JSON decoder.

    Raises:
        ValueError: The description contains an unsupported numerical constant.
    """

    raise ValueError(f"The software description contains the unsupported value {text!r}.")


@lru_cache(maxsize=len(_PROGRAMS))
def _read_description(program: str) -> dict[str, object]:
    """

    Reads and caches the installed definition for one named program.

    We read at most 256 KiB plus one byte so an oversized file is rejected before
    JSON decoding. Repeated fields and nonfinite values are rejected during that
    decoding. The outer value must be a dictionary; its individual version,
    download and required-file fields are checked by the corresponding program.

    Args:
        program: Supported program name checked by read_software_description().

    Returns:
        Cached dictionary for the installed description. The public reader
        returns a separate copy, keeping this cached value unchanged.

    Raises:
        ValueError: The file cannot be read, decoded or represented within the
            supported JSON and size limits.
    """

    path = resources.files("mif_pipeline").joinpath("config", "software", program + ".json")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_DESCRIPTION_BYTES + 1)
        if len(raw) > _MAX_DESCRIPTION_BYTES:
            raise ValueError("The software description exceeds 256 KiB.")
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
        if type(value) is not dict:
            raise ValueError("The software description must contain named fields.")
        if value.get("schema") != "mif-software-installation/v1" or value.get("program") != program:
            raise ValueError("The software description does not identify the requested program and format.")
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        raise ValueError(f"Cannot read the installed {program} software description: {error}") from error
    return value


def read_software_description(program: str) -> dict[str, object]:
    """

    Returns the installed release and file definitions for the selected program.

    We resolve the supported program name within config/software and return a
    deep copy of its cached JSON. Each caller can edit its returned values while
    the cached definition remains available for subsequent setup checks. Restarting
    the application reloads changes made to the installed program descriptions.

    Args:
        program: One of bioformats, biolinkbert, elastix, instanseg, java, nimbus,
            pytorch, qupath or warpy.

    Returns:
        Independent dictionary containing that program's installed definition.

    Raises:
        ValueError: The program is unsupported or its JSON file is unreadable/invalid.
    """

    if not isinstance(program, str) or program not in _PROGRAMS:
        raise ValueError("Select a supported software description by its program name.")
    return deepcopy(_read_description(program))
