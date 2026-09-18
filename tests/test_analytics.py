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
