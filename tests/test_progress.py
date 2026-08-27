"""Progress reporting for the long engine calls.

The point of these is that progress is *advisory*: it must describe the run
accurately when asked for, and must be incapable of changing or breaking the
run when it isn't — or when the consumer is broken.
"""

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from breakdown.api.main import app
from breakdown.engine.progress import report
from breakdown.engine.rca import run_rca
from breakdown.parser import Parser
from tests.synthetic import generate_mock_data, win

YAML = """
metrics:
  - name: daily_sessions
    source: dbt.metric.daily_sessions
  - name: order_count
    source: dbt.metric.order_count
    parents: [daily_sessions]
    priors:
      coefficient:
        distribution: "Normal"
        params: { mu: 0.1, sigma: 0.02 }
  - name: average_order_value
    source: dbt.metric.average_order_value
  - name: revenue
    source: dbt.metric.revenue
    formula: "order_count * average_order_value"
    parents: [order_count, average_order_value]
"""

REF = ("2024-01-01", "2024-02-15")
AN = ("2024-02-16", "2024-04-09")


def make_tree():
    return Parser(YAML).dag, generate_mock_data(n_days=100)


def test_report_is_a_noop_without_a_callback():
    report(None, stage="fitting")  # must not raise


def test_report_swallows_a_broken_consumer():
    """A progress consumer that raises must not be able to fail an analysis."""

    def boom(_update):
        raise RuntimeError("consumer exploded")

    report(boom, stage="fitting")  # must not raise


def test_rca_reports_real_stages_and_a_real_denominator():
    dag, data = make_tree()
    updates = []
    run_rca(dag, data, {}, "revenue", **win(REF, AN), draws=300, progress=updates.append)

    stages = [u["stage"] for u in updates]
    assert "fitting" in stages
    assert stages[-1] == "attributing"

    # order_count is the only probabilistic node in scope, so the denominator
    # is 1 — and it is the engine's real work list, not an estimate.
    fits = [u for u in updates if u["stage"] == "fitting"]
    assert [(u["metric"], u["current"], u["total"]) for u in fits] == [("order_count", 1, 1)]


def test_progress_does_not_change_the_answer():
    """The callback is passed explicitly and read by nothing, so a run with
    progress must be identical to one without."""
    dag, data = make_tree()
    quiet = run_rca(dag, data, {}, "revenue", **win(REF, AN), draws=300)
    noisy = run_rca(dag, data, {}, "revenue", **win(REF, AN), draws=300, progress=lambda _u: None)
    assert quiet == noisy


def test_progress_endpoint_reports_unknown_ids_as_a_non_error():
    """A finished run and a never-started one are the same answer to a poller,
    and neither is worth handling as an error on the client."""
    with TestClient(app) as client:
        resp = client.get("/progress/never-started")
        assert resp.status_code == 200
        assert resp.json() == {"stage": None}


def test_progress_entry_is_cleaned_up_after_the_run():
    with TestClient(app) as client:
        resp = client.post(
            "/rca/revenue",
            params={
                "analysis_start": AN[0],
                "analysis_end": AN[1],
                "reference_start": REF[0],
                "reference_end": REF[1],
                "run_id": "cleanup-probe",
            },
        )
        assert resp.status_code == 200
        assert "cleanup-probe" not in app.state.progress
        assert client.get("/progress/cleanup-probe").json() == {"stage": None}


def test_run_id_does_not_change_the_response():
    with TestClient(app) as client:
        params = {
            "analysis_start": AN[0],
            "analysis_end": AN[1],
            "reference_start": REF[0],
            "reference_end": REF[1],
        }
        without = client.post("/rca/revenue", params=params).json()
        with_id = client.post("/rca/revenue", params={**params, "run_id": "probe"}).json()
        assert without == with_id
