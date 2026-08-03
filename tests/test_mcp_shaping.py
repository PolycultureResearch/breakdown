"""Unit tests for MCP response shaping: rounding, compaction, deep links."""

from urllib.parse import quote

from breakdown.mcp.shaping import (
    COLD_START_HOW_TO_READ,
    RCA_HOW_TO_READ,
    WHATIF_HOW_TO_READ,
    compact_rca,
    compact_scenario,
    metric_link,
    rca_link,
    round_floats,
    whatif_how_to_read,
    whatif_link,
)


def test_round_floats():
    assert round_floats(0.123456789) == 0.1235
    assert round_floats(123456.789) == 123500.0
    assert round_floats(-0.000123456) == -0.0001235
    assert round_floats(0.0) == 0.0
    assert round_floats(7) == 7  # ints untouched
    assert round_floats(float("nan")) is None
    assert round_floats(float("inf")) is None
    assert round_floats({"a": [1.23456, {"b": 9.87654}]}) == {"a": [1.235, {"b": 9.877}]}


def test_links_match_ui_deep_link_params():
    # Param names are applyDeepLink()'s contract in static/app.js — renaming
    # them there must break this test.
    url = rca_link("revenue", "2024-01-01", "2024-02-15", "2024-02-16", "2024-04-09")
    assert url == (
        "http://127.0.0.1:9090/ui/#rca=revenue"
        "&reference_start=2024-01-01&reference_end=2024-02-15"
        "&analysis_start=2024-02-16&analysis_end=2024-04-09"
    )
    assert metric_link("order_count") == "http://127.0.0.1:9090/ui/#metric=order_count"

    scenario = {"baseline_start": "2024-03-01", "baseline_end": "2024-04-09"}
    url = whatif_link(scenario)
    assert url.startswith("http://127.0.0.1:9090/ui/#whatif=")
    assert quote('{"baseline_start":"2024-03-01"') in url


def test_links_respect_public_url_env(monkeypatch):
    monkeypatch.setenv("BREAKDOWN_PUBLIC_URL", "https://breakdown.example.com/")
    assert metric_link("revenue") == "https://breakdown.example.com/ui/#metric=revenue"
    monkeypatch.delenv("BREAKDOWN_PUBLIC_URL")
    monkeypatch.setenv("BREAKDOWN_PORT", "8123")
    assert metric_link("revenue") == "http://127.0.0.1:8123/ui/#metric=revenue"


def _rca_fixture():
    contribution = {
        "parent": "order_count",
        "estimate": -160000.0,
        "share_of_gap": 1.398,
        "ci_95": [-200800.0, -119800.0],
        "prob_same_direction": 1.0,
        "decomposition": {
            "means": {"estimate": -1.0, "ci_95": [-2.0, 0.0]},
            "comovement": {"estimate": 0.1, "ci_95": [-0.1, 0.2]},
        },
    }
    ok_node = {
        "status": "ok",
        "grain": "day",
        "effective_windows": {
            "reference": {"start": "2024-01-01", "end": "2024-02-15", "n_periods": 46},
            "analysis": {"start": "2024-02-16", "end": "2024-04-09", "n_periods": 54},
        },
        "baseline": 860600.0,
        "actual": 746200.0,
        "gap": -114400.0,
        "relative_change": -0.133,
        "attribution_method": "shapley",
        "inference_method": None,
        "fit_quality": None,
        "sign_warnings": None,
        "ci_status": "ok",
        "unexplained": 3608.0,
        "components": {
            "trend": {"estimate": -0.13, "ci_95": [-0.2, -0.05]},
            "seasonal": {"estimate": 0.0, "ci_95": [-0.01, 0.01]},
        },
        "interaction": {"estimate": -742.3, "ci_95": [-2447.0, 961.8]},
        "contributions": [contribution],
    }
    skipped_node = {
        "status": "window_shorter_than_grain",
        "grain": "month",
        "effective_windows": None,
        "baseline": None,
        "actual": None,
        "gap": None,
        "relative_change": None,
        "attribution_method": None,
        "fit_quality": None,
        "sign_warnings": None,
        "ci_status": None,
        "unexplained": None,
        "components": None,
        "interaction": None,
        "contributions": [],
    }
    return {
        "target": "revenue",
        "reference_window": {"start": "2024-01-01", "end": "2024-02-15"},
        "analysis_window": {"start": "2024-02-16", "end": "2024-04-09"},
        "nodes": {"revenue": ok_node, "monthly_costs": skipped_node},
        "ranked_causes": [
            {"metric": f"m{i}", "score": 1.0 - i / 20, "via": "revenue"} for i in range(15)
        ],
    }


def test_compact_rca():
    out = compact_rca(_rca_fixture())

    assert set(out) == {"target", "reference_window", "analysis_window", "nodes", "ranked_causes"}
    assert len(out["ranked_causes"]) == 10  # capped

    node = out["nodes"]["revenue"]
    # windows collapse to period counts; components to point estimates
    assert node["n_periods"] == {"reference": 46, "analysis": 54}
    assert node["components"] == {"trend": -0.13, "seasonal": 0.0}
    # null node fields are omitted, not serialized
    assert "fit_quality" not in node and "sign_warnings" not in node
    # contributions keep the narration channels and drop the decomposition
    contrib = node["contributions"][0]
    assert set(contrib) == {"parent", "estimate", "share_of_gap", "ci_95", "prob_same_direction"}

    # skipped nodes shrink to their status instead of a null skeleton
    assert out["nodes"]["monthly_costs"] == {
        "status": "window_shorter_than_grain",
        "grain": "month",
    }


def test_compact_rca_keeps_withheld_ci():
    fixture = _rca_fixture()
    contrib = fixture["nodes"]["revenue"]["contributions"][0]
    contrib["ci_95"] = None  # degenerate window: interval honestly withheld
    fixture["nodes"]["revenue"]["ci_status"] = "degenerate_single_period"
    out = compact_rca(fixture)
    # ci_95: null is meaningful (see how_to_read) and must survive compaction
    assert out["nodes"]["revenue"]["contributions"][0]["ci_95"] is None
    assert out["nodes"]["revenue"]["ci_status"] == "degenerate_single_period"


def test_compact_scenario():
    fixture = {
        "mode": "fitted",
        "baseline_window": {"start": "2024-03-01", "end": "2024-04-09"},
        "n_draws": 2000,
        "seed": 0,
        "sources": [
            {"id": "i:daily_sessions", "kind": "intervention", "label": "daily_sessions +15.0%"}
        ],
        "nodes": {
            "daily_sessions": {
                "status": "intervened",
                "baseline": 1446.0,
                "simulated": 1663.0,
                "delta": {"estimate": 216.9, "ci_95": [216.9, 216.9]},
                "relative_delta": 0.15,
                "prob_direction": 1.0,
                "fit_quality": None,
                "extrapolation": {
                    "flag": True,
                    "hist_min": 1.0,
                    "hist_max": 2.0,
                    "hist_mean": 1.5,
                    "hist_std": 0.2,
                },
                "contributions": [{"source": "i:daily_sessions", "estimate": 216.9}],
            },
            "average_order_value": {
                "status": "baseline",
                "baseline": 508.0,
                "simulated": 508.0,
                "delta": {"estimate": 0.0, "ci_95": [0.0, 0.0]},
                "relative_delta": 0.0,
                "prob_direction": None,
                "fit_quality": None,
                "extrapolation": {
                    "flag": False,
                    "hist_min": 1.0,
                    "hist_max": 2.0,
                    "hist_mean": 1.5,
                    "hist_std": 0.2,
                },
                "contributions": [],
            },
        },
        "warnings": [{"kind": "extrapolation", "metric": "daily_sessions", "detail": "…"}],
        "caveats": ["local slopes", "confounders", "asserted assumptions"],
    }
    out = compact_scenario(fixture)

    # mode passes through so a client can label the result
    assert out["mode"] == "fitted"
    # extrapolation stats collapse to the flag; detail lives in warnings
    assert out["nodes"]["daily_sessions"]["extrapolation"] is True
    # unaffected nodes shrink to their baseline
    assert out["nodes"]["average_order_value"] == {"status": "baseline", "baseline": 508.0}
    # caveats and warnings pass through verbatim
    assert out["warnings"] == fixture["warnings"]
    assert out["caveats"] == fixture["caveats"]


def test_how_to_read_guides():
    for guide in (RCA_HOW_TO_READ, WHATIF_HOW_TO_READ):
        assert guide.startswith("How to read")
        assert 400 < len(guide) < 2500  # substantial but token-bounded
    assert "unexplained" in RCA_HOW_TO_READ
    assert "prob_direction" in WHATIF_HOW_TO_READ


def test_compact_scenario_cold_start_keeps_belief_intervals():
    fixture = {
        "mode": "cold_start",
        "baseline_window": None,
        "n_draws": 2000,
        "seed": 0,
        "sources": [{"id": "i:sessions", "kind": "intervention", "label": "sessions +10.0%"}],
        "nodes": {
            "sessions": {
                "status": "intervened",
                "baseline": 1200.0,
                "baseline_ci_95": [830.0, 1570.0],
                "simulated": 1320.0,
                "delta": {"estimate": 120.0, "ci_95": [83.0, 157.0]},
                "relative_delta": 0.10,
                "prob_direction": 1.0,
                "fit_quality": None,
                "extrapolation": {"flag": False, "plausible_min": 0.0, "plausible_max": None},
                "contributions": [{"source": "i:sessions", "estimate": 120.0}],
            },
            "aov": {
                # point-asserted baseline: no belief interval to report
                "status": "baseline",
                "baseline": 50.0,
                "baseline_ci_95": None,
                "simulated": 50.0,
                "delta": {"estimate": 0.0, "ci_95": [0.0, 0.0]},
                "relative_delta": 0.0,
                "prob_direction": None,
                "fit_quality": None,
                "extrapolation": {"flag": False, "plausible_min": None, "plausible_max": None},
                "contributions": [],
            },
        },
        "warnings": [],
        "caveats": ["stated beliefs"],
    }
    out = compact_scenario(fixture)

    assert out["mode"] == "cold_start"
    assert out["baseline_window"] is None
    # the belief interval around an uncertain operating point survives compaction
    assert out["nodes"]["sessions"]["baseline_ci_95"] == [830.0, 1570.0]
    # a point baseline stays compact: null interval is omitted, not emitted
    assert out["nodes"]["aov"] == {"status": "baseline", "baseline": 50.0}


def test_whatif_how_to_read_modes():
    assert whatif_how_to_read("fitted") == WHATIF_HOW_TO_READ
    cold = whatif_how_to_read("cold_start")
    assert cold.startswith(WHATIF_HOW_TO_READ)
    assert COLD_START_HOW_TO_READ in cold
    assert "COLD START" in cold
    assert "never as evidence" in cold
