from __future__ import annotations

from statistics import median


def view_multiplier(views: int | None, baseline: float | int | None) -> float | None:
    if views is None or baseline is None or baseline <= 0:
        return None
    return round(views / baseline, 2)


def annotate_view_anomalies(rows: list[dict]) -> list[dict]:
    values = [row["views"] for row in rows if isinstance(row.get("views"), int) and row["views"] >= 0]
    baseline = median(values) if values else None
    for row in rows:
        multiplier = view_multiplier(row.get("views"), baseline)
        row["view_multiplier"] = multiplier
        row["is_anomaly"] = multiplier is not None and multiplier >= 2.0 and len(values) >= 3
    return rows
