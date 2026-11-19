"""Read our canonical spatial patch vectors without assembling a second matrix.

Each completed row belongs to one immutable image/grid/panel/encoder selection.
Labels and graph neighbourhoods remain in the fitting task. Mappings live until
that task finishes, including exception and cancellation paths.
"""

from contextlib import contextmanager
from collections.abc import Iterator
import hashlib
import json
from pathlib import Path

import numpy as np


@contextmanager
def mapped_patch_rows(selection: dict[str, object], expected_rows: int) -> Iterator[list[np.ndarray]]:
    """Yield read-only row views after checking the selected completed bytes.

    We open each spatial table once and check only the requested row checksums.
    Completion files are read one at a time; only the numeric mappings remain
    open while fitting, so each image needs one retained file handle.
    The graph trainer builds its required labelled self/neighbour arrays directly
    from these views; no dense copy of the selected frozen population is needed.
    """
    if not isinstance(selection, dict) or set(selection) != {"tables", "rows"}:
        raise ValueError("Frozen features require their tables and spatial row selections")
    tables, positions = selection["tables"], selection["rows"]
    if not isinstance(tables, list) or not 1 <= len(tables) <= expected_rows:
        raise ValueError("The frozen table inventory exceeds the selected graph")
    if not isinstance(positions, list) or len(positions) != expected_rows:
        raise ValueError("Frozen rows must exactly cover the graph nodes")
    grouped = [[] for _ in tables]
    for index, position in enumerate(positions):
        if (not isinstance(position, list) or len(position) != 2
                or any(type(value) is not int for value in position)):
            raise ValueError("A frozen row needs its integer table and spatial position")
        table, row = position
        if not 0 <= table < len(tables) or row < 0:
            raise ValueError("The selected patch lies outside its frozen table")
        grouped[table].append((index, row))
    opened = []
    values = [None] * expected_rows
    try:
        for table, reference in enumerate(tables):
            if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
                raise ValueError("A frozen table requires its completed descriptor identity")
            path = Path(reference["path"])
            if not path.is_absolute() or path.stat().st_size > 1024**2:
                raise ValueError("Frozen table metadata must be a bounded absolute source")
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != reference["sha256"]:
                raise ValueError("The selected frozen table metadata changed")
            description = json.loads(content)
            count = description["grid_rows"] * description["grid_columns"]
            if description["schema_version"] != "mif-kronos-patch-table/v1" or description["feature_width"] != 768:
                raise ValueError("The frozen patch table has an incompatible feature definition")
            matrix = np.load(path.parent / "features.npy", mmap_mode="r", allow_pickle=False)
            opened.append(matrix)
            if matrix.shape != (count, 768) or matrix.dtype != np.dtype("<f4"):
                raise ValueError("Frozen numeric storage differs from the declared spatial grid")
            checked = set()
            with (path.parent / "completed.sha256").open("rb") as completed:
                for index, row in grouped[table]:
                    if row >= count:
                        raise ValueError("The selected patch lies outside its frozen table")
                    value = matrix[row]
                    if row not in checked:
                        completed.seek(row * 32)
                        expected = completed.read(32)
                        if expected != hashlib.sha256(memoryview(value)).digest():
                            raise ValueError("The selected frozen row is incomplete or changed")
                        checked.add(row)
                    values[index] = value
            grouped[table].clear()
        yield values
    finally:
        for matrix in opened:
            matrix._mmap.close()
