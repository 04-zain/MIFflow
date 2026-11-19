"""Save image display choices separately from analysis settings and results.

Qt and QuPath exchange channel colours, visible channels and intensity limits
through this small file. Image URIs identify the pixels being displayed; these
preferences never participate in scientific result validation or recomputation.
"""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Mapping


def _read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_image_appearance(path, image_uri: str) -> dict | None:
    """Return the saved appearance for this exact image, when available."""
    if path is None:
        return None
    value = _read(path).get(image_uri.rstrip("/"))
    return dict(value) if isinstance(value, Mapping) else None


def save_image_appearance(path, image_uri: str, descriptor: Mapping) -> None:
    """Replace this image's display choices while retaining other images."""
    if path is None:
        return
    path = Path(path)
    data = _read(path)
    key=image_uri.rstrip("/")
    previous=data.get(key,{})
    value=dict(descriptor)
    for field in ('colors','limits'):
        retained=previous.get(field,{}) if isinstance(previous,Mapping) else {}
        value[field]={**(retained if isinstance(retained,Mapping) else {}),**value.get(field,{})}
    data[key] = value
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(path)
