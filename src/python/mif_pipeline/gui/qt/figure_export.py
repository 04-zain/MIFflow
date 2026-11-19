"""Explicit, atomic exports of small scientific Matplotlib figures."""

from io import BytesIO
from pathlib import Path

from matplotlib import rc_context
from matplotlib.figure import Figure
from PySide6.QtCore import QIODevice, QSaveFile
from PySide6.QtWidgets import QFileDialog, QMessageBox


_FORMATS = {"SVG vector image (*.svg)": "svg", "PDF document (*.pdf)": "pdf",
            "PNG image (*.png)": "png"}
_DATA_FORMATS = {"CSV table (*.csv)": "csv", "NumPy crop data (*.npz)": "npz"}


def save_figure(figure: Figure, path: Path, *, description: str | None = None) -> None:
    """Render with optional display metadata before atomically replacing a destination."""
    image_format = path.suffix.lower().lstrip(".")
    if image_format not in _FORMATS.values():
        raise ValueError("Choose a filename ending in .svg, .pdf or .png.")
    content = BytesIO()
    with rc_context({"svg.fonttype": "none", "pdf.fonttype": 42}):
        figure.savefig(content, format=image_format, dpi=300,
                       facecolor="white", bbox_inches="tight",
                       metadata=({"Subject" if image_format == "pdf" else "Description": description}
                                 if description is not None else None))
    _save_bytes(content.getvalue(), path)


def _save_bytes(data: bytes, path: Path) -> None:
    """Publish a complete encoded figure using the same atomic Qt writer."""
    output = QSaveFile(str(path))
    if not output.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError(output.errorString())
    if output.write(data) != len(data):
        message = output.errorString()
        output.cancelWriting()
        raise OSError(message)
    if not output.commit():
        raise OSError(output.errorString())


def _choose_path(parent, default_name: str, formats=None, *, include_filter=False):
    """Use Qt's save dialog, including its standard overwrite confirmation."""
    formats = _FORMATS if formats is None else formats
    dialog = QFileDialog(parent, "Export data" if set(formats.values()) <= set(_DATA_FORMATS.values()) else "Export figure")
    dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
    dialog.setFileMode(QFileDialog.FileMode.AnyFile)
    dialog.setNameFilters(list(formats))
    dialog.setDefaultSuffix(next(iter(formats.values())))
    dialog.selectFile(default_name)
    def select_format(selected):
        suffix = formats[selected]
        dialog.setDefaultSuffix(suffix)
        chosen = dialog.selectedFiles()
        if chosen:
            dialog.selectFile(str(Path(chosen[0]).with_suffix("." + suffix)))
    dialog.filterSelected.connect(select_format)
    accepted = dialog.exec() == QFileDialog.DialogCode.Accepted
    selected = dialog.selectedFiles() if accepted else ()
    name_filter = dialog.selectedNameFilter()
    dialog.deleteLater()
    if not accepted:
        return None
    path = Path(selected[0])
    return (path, name_filter) if include_filter else path


def export_figure(parent, figure: Figure, default_name: str, *, description: str | None = None) -> Path | None:
    """Save the visible figure and optional display description in the chosen format."""
    path = _choose_path(parent, default_name)
    if path is None:
        return None
    try:
        save_figure(figure, path, description=description)
    except (OSError, ValueError, RuntimeError) as exc:
        QMessageBox.warning(parent, "Figure not exported", f"Could not save {path.name}.\n\n{exc}")
        return None
    return path


def export_plot_files(parent, paths: dict[str, Path], default_name: str, *, data_only=False,
                      data_choices: dict[str, Path] | None = None) -> Path | None:
    """Save an existing review rendering without recalculating its scientific view."""
    formats = {label: fmt for label, fmt in (_DATA_FORMATS if data_only else _FORMATS).items() if fmt in paths}
    named = ({f"{title} (*{path.suffix})": path for title, path in data_choices.items()
              if path.suffix.lstrip(".") in _DATA_FORMATS.values()}
             if data_only and data_choices else {})
    if named:
        formats = {label: path.suffix.lstrip(".") for label, path in named.items()}
    if not formats:
        return None
    choice = _choose_path(parent, default_name, formats, include_filter=True) if named else _choose_path(parent, default_name, formats)
    if choice is None:
        return None
    path, selected = choice if named else (choice, None)
    try:
        image_format = path.suffix.lower().lstrip(".")
        if image_format not in formats.values() or (named and image_format != formats[selected]):
            raise ValueError("Choose one of the available export formats.")
        source = named[selected] if named else paths[image_format]
        _save_bytes(source.read_bytes(), path)
    except (OSError, ValueError, RuntimeError) as exc:
        QMessageBox.warning(parent, "Figure not exported", f"Could not save {path.name}.\n\n{exc}")
        return None
    return path
