from __future__ import annotations

from statistics import median


def view_multiplier(views: int | None, baseline: float | int | None) -> float | None:
    if views is None or baseline is None or baseline <= 0:
        return None
    return round(views / baseline, 2)


def annotate_view_anomalies(
    rows: list[dict], *, baseline_values: list[int | None] | None = None
) -> list[dict]:
    """Annotate displayed rows against one consistent reference, including across pages.

    None retains the original current-row reference. An explicitly empty reference
    means no usable baseline, rather than silently substituting the current page.
    """
    source_values = baseline_values if baseline_values is not None else [row.get("views") for row in rows]
    values = [value for value in source_values if isinstance(value, int) and value >= 0]
    baseline = median(values) if values else None
    for row in rows:
        multiplier = view_multiplier(row.get("views"), baseline)
        row["view_multiplier"] = multiplier
        row["is_anomaly"] = multiplier is not None and multiplier >= 2.0 and len(values) >= 3
    return rows
