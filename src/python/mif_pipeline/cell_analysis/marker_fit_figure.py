"""Shared fitted-component parameter presentation for review and saved exports."""


def component_parameters(parameters):
    """Return the two fitted means, variances and conditional mixture weights."""
    variances = parameters.get("variances") or tuple(
        shape * scale ** 2 for shape, scale in zip(parameters["shapes"], parameters["scales"], strict=True))
    return tuple((name, float(mean), float(variance), float(weight))
        for name, mean, variance, weight in zip(
            ("Lower-mean component", "Higher-mean component"),
            parameters["means"], variances, parameters["weights"], strict=True))


def add_component_parameters(axis, parameters):
    """Add the same compact parameter table to both export entry points."""
    axis.set_axis_off()
    rows = [[name, *(format(value, ".8g") for value in (mean, variance, weight))]
            for name, mean, variance, weight in component_parameters(parameters)]
    conditional = parameters.get("conditional_positive_fit", False)
    table = axis.table(cellText=rows,
        colLabels=("Component", "Mean", "Variance", "Weight\n(positive values)" if conditional else "Weight"),
        cellLoc="center", loc="center", colWidths=(.34, .22, .22, .22))
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.35)
    for (row, _column), cell in table.get_celld().items():
        cell.set_edgecolor("#d8e2e0")
        cell.set_facecolor("#edf5f3" if row == 0 else "white")
        if row == 0 and conditional:
            cell.set_height(cell.get_height() * 1.5)
    if conditional:
        axis.text(.5, -.12,
            f"Gamma curves include the positive fraction; {parameters.get('scope_zero_count', 0):,} exact-zero cells remain in the histogram.",
            ha="center", va="top", transform=axis.transAxes, fontsize=8)
    return table
