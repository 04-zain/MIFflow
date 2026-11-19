"""

Loads the initial parameter values from the JSON file for the selected processing module.

We keep classifier, unmixing, tissue-detection and other starting settings in
separate files under config/defaults. For example, "classifier.rtrees" selects
the Random Forest section of classifier.json. Files describing installed
programs and downloads are kept separately under config/software.

Each file is read on its first use in a process. We return an independent copy
of the requested section so editing a form does not change another form's
starting values. Restart the application after editing these files. Once we
accept settings in an analysis, its recorded parameter values are used when
processing or reopening that step.

"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib import resources
import json
import math
import re


# ==============================================================================
#                         Reading module configuration
# ==============================================================================

_DEFAULT_DIRECTORY = "config/defaults"
_MAX_BYTES = 512 * 1024
_SECTION_NAME = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*\Z")


class DefaultConfigurationError(ValueError):
    """

    Reports a missing configuration section or invalid initial parameter values.

    """


def _unique_object(pairs):
    """

    Reads a JSON object while checking that each field appears once.

    JSON parsers normally retain the final value when a field is repeated.
    Here we report that duplication so an edited default cannot be replaced
    by another value elsewhere in the same object.

    Args:
        pairs: Ordered key/value pairs supplied by the JSON decoder.

    Returns:
        Dictionary containing each decoded field once.

    Raises:
        ValueError: A field name is repeated.

    """

    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"The field {name!r} is repeated.")
        result[name] = value
    return result


def _finite_float(text):
    """

    Converts a JSON decimal value and requires the result to remain finite.

    A number such as 1e999 has valid JSON notation but exceeds floating-point
    storage. Checking the converted value catches that case as well as explicit
    NaN or infinity declarations passed by the decoder.

    Args:
        text: Numerical text supplied by JSON parsing.

    Returns:
        Finite Python float for the declared value.

    Raises:
        ValueError: Conversion produces NaN or infinity.

    """

    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"The numerical value {text!r} is not finite.")
    return value


def _parse(payload: bytes, location: str) -> dict:
    """

    Reads a module's named parameter sections and checks the JSON structure.

    The document contains a format identifier and a sections object. Each
    section has a nonempty name and an object of parameter values. Numerical
    ranges specific to a model are checked when its configuration is created.

    Args:
        payload: Complete JSON contents, limited to 512 KiB.
        location: File location included in any error message.

    Returns:
        Dictionary of named parameter sections from the file.

    Raises:
        DefaultConfigurationError: File size, JSON, repeated fields, numerical
            values or section structure are invalid.

    """

    try:
        if len(payload) > _MAX_BYTES:
            raise ValueError("The configuration file exceeds 512 KiB.")
        document = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_finite_float,
        )
        if not isinstance(document, dict) or set(document) != {"schema", "sections"}:
            raise ValueError("The document requires schema and sections fields.")
        if document["schema"] != "mif-authoring-defaults/v1":
            raise ValueError("The configuration format is unsupported.")
        sections = document["sections"]
        if not isinstance(sections, dict) or not sections:
            raise ValueError("The configuration must contain at least one section.")
        if any(
            not isinstance(name, str)
            or _SECTION_NAME.fullmatch(name) is None
            or not isinstance(values, dict)
            for name, values in sections.items()
        ):
            raise ValueError("Each section requires a valid name and an object of values.")
        return sections
    except (ValueError, TypeError, UnicodeError) as error:
        raise DefaultConfigurationError(f"Check {location}: {error}") from error


@lru_cache(maxsize=32)
def _document(module: str) -> dict:
    """

    Loads one processing module's configuration and reuses it for later requests.

    A file may contain several related sections: classifier.json includes
    RF, SVM, features, training and search-grid settings. Every section must
    begin with the same module name so placing a section in the wrong file
    produces a direct error at its location.

    Args:
        module: Validated first part of a section name, such as "classifier".

    Returns:
        Cached sections from config/defaults/<module>.json.

    Raises:
        DefaultConfigurationError: The file is missing, invalid or contains
            sections belonging to another module.

    """

    location = resources.files("mif_pipeline").joinpath(_DEFAULT_DIRECTORY, module + ".json")
    try:
        with location.open("rb") as stream:
            payload = stream.read(_MAX_BYTES + 1)
    except OSError as error:
        raise DefaultConfigurationError(f"Cannot read {location}: {error}") from error
    sections = _parse(payload, str(location))
    if any(name.split(".", 1)[0] != module for name in sections):
        raise DefaultConfigurationError(f"Check {location}: all sections must belong to {module}.")
    return sections


# ==============================================================================
#                         Selecting initial parameter values
# ==============================================================================


def default_section(name: str) -> dict:
    """

    Returns editable starting values for the requested processing method.

    We use the first part of the name to select its file, then read the exact
    named section. A deep copy includes nested lists and dictionaries, which
    lets callers adjust the returned settings independently.

    Args:
        name: Section name, for example "classifier.rtrees" or "tissue_detection".

    Returns:
        Independent dictionary of initial parameter values.

    Raises:
        DefaultConfigurationError: The name is invalid, the module file cannot
            be read, or the requested section is absent.

    """

    if not isinstance(name, str) or _SECTION_NAME.fullmatch(name) is None:
        raise DefaultConfigurationError("Select a valid configuration section name.")
    module = name.split(".", 1)[0]
    sections = _document(module)
    if name not in sections:
        raise DefaultConfigurationError(
            f"The section {name!r} is missing from {_DEFAULT_DIRECTORY}/{module}.json."
        )
    return deepcopy(sections[name])
