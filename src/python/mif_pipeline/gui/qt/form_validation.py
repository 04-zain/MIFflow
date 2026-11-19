"""Collect independent editor errors before presenting or submitting a form."""
from mif_pipeline.gui.interactions import ControlKind, decode_control_value


def control_errors(controls, values):
    """Check all supplied controls using the shared decoder, without scientific mutation."""
    errors = {}
    for control in controls:
        value = values.get(control.name)
        blank = value is None or isinstance(value, str) and not value.strip()
        if blank and not control.required:
            continue
        if blank and control.kind in {ControlKind.TEXT, ControlKind.LOCAL_FILE}:
            errors[control.name] = "Choose a file." if control.kind is ControlKind.LOCAL_FILE else "Enter a value."
            continue
        if control.kind is ControlKind.CHOICE and value not in control.choices:
            errors[control.name] = "Choose one of the available options."
            continue
        try:
            decode_control_value(control, value)
        except (TypeError, ValueError):
            errors[control.name] = {
                ControlKind.INTEGER: "Enter a whole number.",
                ControlKind.NUMBER: "Enter a finite number.",
                ControlKind.CHECKBOX: "Choose whether to include this option.",
                ControlKind.LOCAL_FILE: "Enter a valid file path.",
            }.get(control.kind, "Check the entered values.")
    return errors
