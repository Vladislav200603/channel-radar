from app.services.analytics import annotate_view_anomalies, view_multiplier


def test_view_multiplier_handles_empty_baseline() -> None:
    assert view_multiplier(100, 0) is None
    assert view_multiplier(None, 100) is None


def test_anomaly_uses_channel_median() -> None:
    rows = [{"views": 100}, {"views": 110}, {"views": 105}, {"views": 1_000}]
    annotate_view_anomalies(rows)
    assert rows[-1]["is_anomaly"] is True
    assert rows[-1]["view_multiplier"] == 9.3
    assert rows[0]["is_anomaly"] is False


def test_paginated_rows_share_the_period_reference() -> None:
    period_views = [100] * 50 + [1_000]
    first_page = [{"views": 100} for _ in range(50)]
    last_page = [{"views": 1_000}]

    annotate_view_anomalies(first_page, baseline_values=period_views)
    annotate_view_anomalies(last_page, baseline_values=period_views)

    assert all(row["view_multiplier"] == 1.0 for row in first_page)
    assert not any(row["is_anomaly"] for row in first_page)
    assert last_page[0]["view_multiplier"] == 10.0
    assert last_page[0]["is_anomaly"] is True
    # Moving a post between displayed pages does not change its signal.
    moved_post = [{"views": 1_000}, {"views": 100}]
    annotate_view_anomalies(moved_post, baseline_values=period_views)
    assert moved_post[0] == last_page[0]


def test_unavailable_period_reference_does_not_fall_back_to_page() -> None:
    rows = [{"views": 100}, {"views": 100}, {"views": 1_000}]
    annotate_view_anomalies(rows, baseline_values=[])
    assert all(row["view_multiplier"] is None for row in rows)
    assert not any(row["is_anomaly"] for row in rows)


def test_supplied_baseline_requires_valid_nonzero_observations() -> None:
    row = {"views": 1_000}
    annotate_view_anomalies([row], baseline_values=[100, None, 100])
    assert row["view_multiplier"] == 10.0
    assert row["is_anomaly"] is False  # Two observations are insufficient, regardless of page size.
    annotate_view_anomalies([row], baseline_values=[-1, None, 0, 0, 0])
    assert row["view_multiplier"] is None
    assert row["is_anomaly"] is False
