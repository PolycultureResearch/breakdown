"""A run holds what it needs, refuses before it pays, and withholds what it
cannot compute (grill 2026-10-05: M1, M2, L3, L4, L6).

Most of this file never touches a sampler: the fit call is replaced by a
stand-in that returns a `FitResult` of the right shape, because every defect
here is in what `run_rca` / `run_scenario` do *around* a fit — where they keep
it, when they ask for it, what they catch when it fails, and what they publish
from it. Two `slow` tests at the end run the reviewer's own reproductions
against the real sampler and the real bounded store.
"""

import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from breakdown.engine import rca as rca_mod
from breakdown.engine import simulate as sim_mod
from breakdown.engine.model import FitResult, intervention_record
from breakdown.engine.rca import plan_rca_fits, run_rca, sampling_failures
from breakdown.engine.simulate import Intervention, ScenarioRequest, run_scenario
from breakdown.grains import ensure_grained, fit_grain, next_start
from breakdown.parser import Parser

N = 130
DATES = pd.date_range("2024-01-01", periods=N)
REF = {"reference_start": "2024-03-01", "reference_end": "2024-03-31"}
AN = {"analysis_start": "2024-04-01", "analysis_end": "2024-05-09"}

CHAIN = """
metrics:
  - name: x
    source: a.b.x
  - name: y
    source: a.b.y
    parents: [x]
{y_extra}  - name: z
    source: a.b.z
    parents: [y]
"""


def chain_dag(y_extra=""):
    return Parser(CHAIN.format(y_extra=y_extra)).dag


def chain_data(seed=7):
    rng = np.random.default_rng(seed)
    x = 100 + rng.normal(0, 4, N)
    y = 0.5 * x + rng.normal(0, 1, N)
    z = 2 * y + rng.normal(0, 1, N)
    # A real movement in the analysis window, so there is a gap to attribute.
    x[DATES >= AN["analysis_start"]] += 10
    y[DATES >= AN["analysis_start"]] += 5
    z[DATES >= AN["analysis_start"]] += 10
    return pd.DataFrame({"date": DATES, "x": x, "y": y, "z": z})


class Forgetful(dict):
    """The bounded store at its worst: every write evicts everything, the new
    entry included. `TraceStore` keeps the newest entry; another tree writing
    between two of this run's statements does not."""

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.clear()


def stand_in_fit(calls=None, *, fail=None, poison=None):
    """A replacement for the engine's one fit call.

    Returns a `FitResult` whose posterior has the variables the attribution
    reads (`beta_raw`, `trend`, `alpha`) over the same dates `fit_metric`
    would train on. `fail` maps node -> exception to raise; `poison` maps
    node -> posterior variable to fill with NaN.
    """

    def fit(dag, data, node, *args, fit_end=None, **kwargs):
        # `fit_rca_node` takes fit_end positionally, `fit_metric` by keyword.
        if args:
            fit_end = args[0]
        if calls is not None:
            calls.append(node)
        if fail and node in fail:
            raise fail[node]
        parents = list(dag.predecessors(node))
        grain = fit_grain(dag, node)
        frame = ensure_grained(data).fit_frame(node, parents, grain)
        dates = pd.DatetimeIndex(frame["date"])
        if fit_end is not None:
            dates = dates[[next_start(d, grain) <= pd.Timestamp(fit_end) for d in dates]]
        defn = dag.nodes[node]["definition"]
        if defn.fit_start is not None:
            dates = dates[dates >= pd.Timestamp(defn.fit_start)]
        # ...and the leading max-lag rows a lagged regression cannot use.
        dates = dates[max((defn.lags or {}).values(), default=0) :]
        rng = np.random.default_rng(len(node))
        draws = 200
        posterior = {
            "beta_raw": (("chain", "draw", "p"), rng.normal(0.5, 0.05, (1, draws, len(parents)))),
            "trend": (("chain", "draw", "t"), rng.normal(0, 0.01, (1, draws, len(dates)))),
            "alpha": (("chain", "draw"), np.zeros((1, draws))),
        }
        # Declared interventions ride their own coefficient axis (roadmap S24).
        records = [intervention_record(iv, grain) for iv in defn.interventions]
        if records:
            posterior["beta_intervention_raw"] = (
                ("chain", "draw", "iv"),
                rng.normal(5.0, 0.5, (1, draws, len(records))),
            )
        if poison and node in poison:
            dims, values = posterior[poison[node]]
            posterior[poison[node]] = (dims, np.full(values.shape, np.nan))
        return FitResult(
            trace=SimpleNamespace(posterior=xr.Dataset(posterior)),
            target=node,
            parents=parents,
            y_mean=0.0,
            y_std=1.0,
            x_stds=None,
            dates=dates,
            inference_method="nuts",
            fit_end=fit_end,
            grain=grain,
            interventions=records,
        )

    return fit


@pytest.fixture
def fits(monkeypatch):
    """Route both engines' fit calls to the stand-in; yields the call log."""
    calls = []

    def install(**kw):
        fit = stand_in_fit(calls, **kw)
        monkeypatch.setattr(rca_mod, "fit_rca_node", fit)
        monkeypatch.setattr(sim_mod, "fit_metric", fit)
        return calls

    install()
    return SimpleNamespace(calls=calls, install=install)


def _parallel_sampling_error():
    from pymc.sampling.parallel import ParallelSamplingError

    return ParallelSamplingError("Chain 2 failed.", 2)


# ---------------------------------------------------------------------------
# M1: `traces` is a write-through cache, not the run's working memory
# ---------------------------------------------------------------------------


def test_rca_does_not_read_its_own_fits_back_from_the_cache(fits):
    """The store evicting this run's fits between the fit loop and the
    attribution loop was a KeyError — an unhandled 500."""
    traces = Forgetful()
    res = run_rca(chain_dag(), chain_data(), traces, "z", **REF, **AN)

    assert fits.calls == ["y", "z"]  # one fit each, alternatives included
    assert res["nodes"]["y"]["status"] == "ok" and res["nodes"]["z"]["status"] == "ok"
    assert res["nodes"]["z"]["contributions"][0]["parent"] == "y"
    assert res["reference_sensitivity"]["status"] != "unavailable"
    assert not traces  # nothing was retained, and nothing needed to be
    json.dumps(res, allow_nan=False)


def test_rca_holds_a_cached_fit_from_the_plan_onward(fits):
    """A fit that was in the cache when the plan was made is read from the
    plan, so a later eviction cannot take it out from under the run."""
    dag, data = chain_dag(), chain_data()
    warm = {}
    run_rca(dag, data, warm, "z", **REF, **AN, reference_sensitivity=False)
    fits.calls.clear()

    plan = plan_rca_fits(dag, data, warm, "z", **REF, **AN)
    assert plan.to_fit == [] and set(plan.fits) == {"y", "z"}
    assert plan.fits["y"] is warm[("y", AN["analysis_start"])]

    class EvictsOnFirstRead(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            self.pop(key, None)
            return value

    res = run_rca(dag, data, EvictsOnFirstRead(warm), "z", **REF, **AN)
    assert fits.calls == []
    assert res["nodes"]["z"]["status"] == "ok"


def test_a_scenario_does_not_read_its_own_fits_back_from_the_cache(fits):
    scenario = ScenarioRequest(
        baseline_start="2024-03-01",
        baseline_end="2024-03-31",
        interventions=[Intervention(metric="x", mode="delta", value=10.0)],
    )
    res = run_scenario(chain_dag(), chain_data(), Forgetful(), scenario)

    assert fits.calls == ["y", "z"]
    assert res["nodes"]["z"]["status"] == "affected"
    assert res["nodes"]["z"]["fit_quality"] is None  # the stand-in carries no diagnostics
    assert res["nodes"]["z"]["delta"]["estimate"] == pytest.approx(10 * 0.5 * 0.5, rel=0.2)


# ---------------------------------------------------------------------------
# L4: the S23 alternatives attribute over the run's fits and never fit
# ---------------------------------------------------------------------------


def test_the_alternatives_never_refit_a_node_whose_fit_failed(fits):
    """Each alternative re-entered `run_rca` with the caller's cache, found no
    fit for the failed node, and tried again: three attempts for one node."""
    fits.install(fail={"y": ValueError("Column 'y' has zero variance")})
    res = run_rca(chain_dag(), chain_data(), {}, "z", **REF, **AN)

    assert fits.calls == ["y", "z"]
    assert res["nodes"]["y"]["status"] == "fit_failed"
    rs = res["reference_sensitivity"]
    assert [a["status"] for a in rs["alternatives"]] == ["ok", "ok"]


def test_an_attribution_that_may_not_fit_reports_the_missing_fit(fits):
    """`allow_fitting=False` with nothing cached: no fit is attempted, and
    each node that needed one says so — with the earlier run's reason when
    the caller has it."""
    res = run_rca(
        chain_dag(),
        chain_data(),
        {},
        "z",
        **REF,
        **AN,
        reference_sensitivity=False,
        allow_fitting=False,
        fit_failures={"y": "Column 'y' has zero variance"},
    )
    assert fits.calls == []
    assert res["nodes"]["y"]["status"] == "fit_failed"
    assert res["nodes"]["y"]["status_reason"] == "Column 'y' has zero variance"
    assert res["nodes"]["z"]["status"] == "fit_failed"
    assert "does not fit" in res["nodes"]["z"]["status_reason"]
    assert res["nodes"]["z"]["gap"] is not None  # the measured movement stands


def test_the_alternatives_plan_with_the_requested_sampler(fits, monkeypatch):
    """The per-node `inference_method` used to overwrite the call's own, so
    the alternatives were planned with whatever the last node in scope was
    fitted with."""
    seen = []
    real = rca_mod.plan_rca_fits

    def spy(*args, **kwargs):
        seen.append(kwargs["inference_method"])
        return real(*args, **kwargs)

    monkeypatch.setattr(rca_mod, "plan_rca_fits", spy)
    run_rca(chain_dag(), chain_data(), {}, "z", **REF, **AN, inference_method="advi")
    assert seen == ["advi", "advi", "advi"]


# ---------------------------------------------------------------------------
# M2: a reference before a node's fitted period is settled before any fit
# ---------------------------------------------------------------------------

FIT_START = "    fit_start: 2024-03-10\n"


def test_an_ancestors_fit_start_degrades_that_node_and_nothing_else(fits):
    """`y` is fitted from 2024-03-10; the caller's reference starts on the
    1st. That raised from inside the attribution loop, after every fit."""
    res = run_rca(chain_dag(FIT_START), chain_data(), {}, "z", **REF, **AN)

    y = res["nodes"]["y"]
    assert y["status"] == "reference_before_fit_window"
    assert "fit_start: 2024-03-10" in y["status_reason"]
    assert "on or after 2024-03-10" in y["status_reason"]
    # Its movement is measured from the data and stands; only the split is missing.
    assert y["gap"] is not None and y["contributions"] == []
    assert fits.calls == ["z"]  # never fitted: there was nothing it could say
    assert res["nodes"]["z"]["status"] == "ok"
    assert res["ranked_causes"][0]["metric"] == "y"
    json.dumps(res, allow_nan=False)


def test_the_targets_fit_start_is_refused_before_any_fit(fits):
    dag = Parser(
        """
metrics:
  - name: x
    source: a.b.x
  - name: y
    source: a.b.y
    parents: [x]
  - name: z
    source: a.b.z
    parents: [y]
    fit_start: 2024-03-10
"""
    ).dag
    with pytest.raises(ValueError, match="first day 'z' is fitted on \\(2024-03-10\\)") as e:
        run_rca(dag, chain_data(), {}, "z", **REF, **AN)
    assert "Nothing was fitted" in str(e.value)
    assert fits.calls == []


def test_the_lag_trim_counts_toward_the_first_fitted_period(fits):
    """`fit_start` cuts first and the lag trims after it, so the first fitted
    day is `fit_start + lag` — which is what the reference is checked against
    and what the default reference is floored at."""
    dag = chain_dag("    fit_start: 2024-03-10\n    lags: {x: 3}\n")
    data = chain_data()

    plan = plan_rca_fits(
        dag, data, {}, "z", reference_start="2024-03-11", reference_end="2024-03-31", **AN
    )
    assert "2024-03-13" in plan.reference_before_fit["y"]
    assert "longest parent lag (3 day(s))" in plan.reference_before_fit["y"]

    # The default reference is one the engine then accepts.
    res = run_rca(dag, data, {}, "z", **AN, reference_sensitivity=False)
    assert res["reference_window"]["start"] == "2024-03-13"
    assert res["nodes"]["y"]["status"] == "ok"
    assert res["nodes"]["y"]["fit_window"]["start"] == "2024-03-13"


# ---------------------------------------------------------------------------
# L3: a chain dying in a multi-process run is a fit failure like any other
# ---------------------------------------------------------------------------


def test_parallel_sampling_error_is_among_the_fit_failures():
    error = _parallel_sampling_error()
    assert not isinstance(error, (ValueError, RuntimeError))  # why it escaped
    assert isinstance(error, sampling_failures())
    assert {ValueError, RuntimeError} <= set(sampling_failures())


def test_a_failed_chain_degrades_the_node_in_rca(fits):
    fits.install(fail={"y": _parallel_sampling_error()})
    res = run_rca(chain_dag(), chain_data(), {}, "z", **REF, **AN, reference_sensitivity=False)
    assert res["nodes"]["y"]["status"] == "fit_failed"
    assert res["nodes"]["y"]["status_reason"] == "Chain 2 failed."
    assert res["nodes"]["z"]["status"] == "ok"


@pytest.mark.parametrize("error", ["parallel", "runtime"])
def test_a_failed_sample_refuses_the_scenario_in_its_own_words(fits, error):
    """A scenario cannot degrade one node, so it refuses — as a ValueError the
    route turns into a 422, and without calling a sampler crash a constant
    series."""
    raised = (
        _parallel_sampling_error() if error == "parallel" else RuntimeError("Bad initial energy")
    )
    fits.install(fail={"y": raised})
    scenario = ScenarioRequest(
        baseline_start="2024-03-01",
        baseline_end="2024-03-31",
        interventions=[Intervention(metric="x", mode="delta", value=10.0)],
    )
    with pytest.raises(ValueError, match="Cannot simulate this scenario: 'y'") as e:
        run_scenario(chain_dag(), chain_data(), {}, scenario)
    assert "could not be sampled" in str(e.value)
    assert type(raised).__name__ in str(e.value)
    assert "constant series" not in str(e.value)


# ---------------------------------------------------------------------------
# L6: a non-finite posterior is withheld by name, never emitted or zeroed
# ---------------------------------------------------------------------------


def test_a_non_finite_coefficient_posterior_is_withheld(fits):
    fits.install(poison={"z": "beta_raw"})
    res = run_rca(chain_dag(), chain_data(), {}, "z", **REF, **AN, reference_sensitivity=False)
    json.dumps(res, allow_nan=False)

    z = res["nodes"]["z"]
    assert z["status"] == "ok" and z["ci_status"] == "nonfinite_posterior"
    (contribution,) = z["contributions"]
    assert contribution["estimate"] is None and contribution["ci_95"] is None
    assert contribution["share_of_gap"] is None and contribution["prob_same_direction"] is None
    # No residual is computed around a missing term.
    assert z["unexplained"] is None and z["unexplained_status"] is None
    assert z["gap"] is not None
    # A withheld share carries no evidence: `y` is reached and weighs nothing.
    ranked = {r["metric"]: r for r in res["ranked_causes"]}
    assert ranked["y"] == {"metric": "y", "score": 0.0, "via": "z"}
    assert res["nodes"]["y"]["ci_status"] == "ok"


def test_a_non_finite_trend_is_withheld_instead_of_raising(fits):
    """`sample_summary` already answered `estimate: None` for the trend; the
    sum over the components then raised TypeError — a 500."""
    fits.install(poison={"z": "trend"})
    res = run_rca(chain_dag(), chain_data(), {}, "z", **REF, **AN, reference_sensitivity=False)
    json.dumps(res, allow_nan=False)

    z = res["nodes"]["z"]
    assert z["components"]["trend"] == {"estimate": None, "ci_95": None}
    assert z["ci_status"] == "nonfinite_posterior"
    assert z["unexplained"] is None and z["unexplained_status"] is None
    # The coefficient posterior was finite, and its contribution still stands.
    assert z["contributions"][0]["estimate"] is not None


def test_a_withheld_intervention_withholds_the_residual_with_it(fits):
    """The intervention entry already said `nonfinite_posterior`, and the
    node's `unexplained` was then computed as if the term were zero — the
    step's whole effect, published as a measured residual with `ci_status: ok`."""
    step = "    interventions:\n      - {name: flip, date: 2024-03-15, kind: step}\n"
    res = run_rca(chain_dag(step), chain_data(), {}, "z", **REF, **AN, reference_sensitivity=False)
    y = res["nodes"]["y"]
    assert y["interventions"][0]["ci_status"] == "ok" and y["ci_status"] == "ok"
    assert y["unexplained"] is not None and y["unexplained_status"] == "measured"

    fits.install(poison={"y": "beta_intervention_raw"})
    res = run_rca(chain_dag(step), chain_data(), {}, "z", **REF, **AN, reference_sensitivity=False)
    json.dumps(res, allow_nan=False)
    y = res["nodes"]["y"]
    (flip,) = y["interventions"]
    assert flip["estimate"] is None and flip["ci_status"] == "nonfinite_posterior"
    assert y["ci_status"] == "nonfinite_posterior"
    assert y["unexplained"] is None and y["unexplained_status"] is None
    assert y["contributions"][0]["estimate"] is not None


# ---------------------------------------------------------------------------
# The reviewer's reproductions, against the real sampler and the real store
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_rca_survives_a_store_too_small_for_its_own_fits():
    from breakdown.api.trees import TraceStore

    # A byte budget that holds one fit of this size, not two.
    store = TraceStore(max_bytes=300_000)
    traces = store.view("t")
    res = run_rca(chain_dag(), chain_data(), traces, "z", **AN, draws=100)

    assert list(traces) == [("z", AN["analysis_start"])]  # `y` was evicted
    assert res["nodes"]["y"]["status"] == "ok" and res["nodes"]["z"]["status"] == "ok"
    assert res["nodes"]["y"]["fit_window"]["end"] == "2024-03-31"
    json.dumps(res, allow_nan=False)


@pytest.mark.slow
def test_an_ancestors_fit_start_costs_no_fit_and_ends_nothing():
    traces = {}
    res = run_rca(chain_dag(FIT_START), chain_data(), traces, "z", **REF, **AN, draws=100)

    assert list(traces) == [("z", AN["analysis_start"])]
    assert res["nodes"]["y"]["status"] == "reference_before_fit_window"
    assert res["nodes"]["z"]["status"] == "ok"
    assert res["nodes"]["z"]["fit_window"]["start"] == "2024-01-01"
    json.dumps(res, allow_nan=False)
