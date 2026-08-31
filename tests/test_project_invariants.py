"""The four rules in AGENTS.md, enforced structurally.

Two hostile reviews found the same meta-defect twice: a policy chosen carefully
in one file and not propagated to its neighbour. Five separate findings were
each a defect the author had already fixed one file over — C15 against
`dbt_sql.py`'s refusal discipline, C17 against `slices.py`'s encoder guard, C18
against `_align_to_spine`'s own interior-gap warning, the unbounded
`slice_cache` against C8's bounded `traces`, and the uncapped `compute_shapley`
against `simulate.py`'s `_MAX_SOURCES`.

So these tests **enumerate the code and check the property**, rather than
pinning today's call sites. A test that asserted "these four caches are
bounded" would have passed on the day `slice_cache` was added and would not
have caught any of the five. The question each test asks is the one a reviewer
would: *is there a new place where this rule is not followed?*

A rule that is genuinely violated in one documented place is pinned with that
exception named, so the exception stays deliberate and a second one fails.

The final sections are not among the four rules. They are the same ratchet
applied to later findings, each a structural property of the app rather than
a statistical one. 2.16's "one `APIRouter`, included twice, so the aliases
cannot drift" went unasserted until a README curl test reported it as a
documentation problem. The `read-the-numbers` trial of 2026-08-13 then
produced two more, both again "a policy applied at one call site and not its
neighbours": a saturated statistic published as certainty
(`prob_same_direction` at exactly 1.00, while `rca.py` one line away withheld
a degenerate one), and a date string validated on two routes and not on their
four siblings.
"""

import ast
import dataclasses
import inspect
import json
import logging
import math
import os
import re
import typing
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from pydantic import AfterValidator

from breakdown import data_fetch
from breakdown.api import trees as trees_mod
from breakdown.api.main import app, router
from breakdown.data_fetch import _align_to_spine
from breakdown.engine import model as model_mod
from breakdown.engine import rca as rca_mod
from breakdown.engine import simulate as simulate_mod
from breakdown.engine import stats as stats_mod
from breakdown.parser import Parser

PACKAGE = Path(__file__).resolve().parent.parent / "breakdown"


# --- Rule 1: the provider boundary refuses rather than approximates ----------


def _spine_call(kind, rows, start="2024-01-01", end="2024-01-10"):
    df = pd.DataFrame({"date": pd.to_datetime([d for d, _ in rows]), "m": [v for _, v in rows]})
    return _align_to_spine(df, "m", "day", kind, start, end, "m")


@pytest.mark.parametrize(
    "kind,rows,gap",
    [
        # every gap position x every kind that can reach one
        ("flow", [("2024-01-05", 5.0), ("2024-01-10", 6.0)], "leading"),
        ("flow", [("2024-01-01", 5.0), ("2024-01-10", 6.0)], "interior"),
        ("stock", [("2024-01-05", 5.0), ("2024-01-10", 6.0)], "leading"),
        ("stock", [("2024-01-01", 5.0), ("2024-01-10", 6.0)], "interior"),
        ("rate", [("2024-01-05", 5.0), ("2024-01-10", 6.0)], "leading"),
        ("rate", [("2024-01-01", 5.0), ("2024-01-10", 6.0)], "interior"),
    ],
)
def test_no_gap_is_filled_without_saying_so(kind, rows, gap, caplog):
    """Rule 1. A period the source did not return is either refused or named.

    The one thing that must never happen is the C18 shape: a value invented and
    nothing said. `stock` and `rate` refuse; `flow` fills and logs. Which of the
    two a given case does is a design decision recorded in `_align_to_spine`'s
    docstring — this test only insists that it is one of them.
    """
    caplog.set_level(logging.WARNING, logger="breakdown.data_fetch")
    try:
        out = _spine_call(kind, rows)
    except (RuntimeError, ValueError):
        return  # refused: the strongest form of the rule
    invented = int(out["m"].isna().sum() == 0 and len(out) > len(rows))
    if invented:
        assert caplog.records, (
            f"{kind}/{gap}: {len(out) - len(rows)} period(s) were invented and "
            "nothing was logged — this is the C18 defect in a new place. Either "
            "refuse, or log what was fabricated."
        )


def test_the_one_silent_fill_is_the_documented_one(caplog):
    """Rule 1, its single deliberate exception, pinned so it stays single.

    A source returning *no rows at all* keeps the full zero-fill for flows: an
    all-quiet window is a legitimate flow series, and `_align_to_spine`'s
    docstring says so. That is the only silent fill in the codebase.
    """
    caplog.set_level(logging.WARNING, logger="breakdown.data_fetch")
    empty = pd.DataFrame({"date": pd.to_datetime([]), "m": []})
    out = _align_to_spine(empty, "m", "day", "flow", "2024-01-01", "2024-01-05", "m")
    assert len(out) == 5 and (out["m"] == 0.0).all()
    assert not caplog.records, "the empty-source fill is deliberate and silent; nothing else is"


def _fetcher_classes():
    """Every concrete `BaseDataFetcher` in the package, wherever it lives.

    Was `vars(data_fetch)` filtered on a `DataFetcher` name suffix, and
    `SnapshotFetcher` failed **both** filters — wrong module, wrong suffix — so
    the one fetcher that serves files written by an older release was the one
    the invariant could not see. It served pre-C2 snapshots verbatim, including
    a four-day partial week presented as a whole one on the demo a prospect is
    shown. Enumerate by base class, not by where something lives or what it is
    called.
    """
    import importlib
    import pkgutil

    import breakdown

    found = {}
    for mod in pkgutil.walk_packages(breakdown.__path__, "breakdown."):
        try:
            module = importlib.import_module(mod.name)
        except Exception:  # pragma: no cover - optional provider extras
            continue
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, data_fetch.BaseDataFetcher)
                and obj is not data_fetch.BaseDataFetcher
                and not inspect.isabstract(obj)
            ):
                found[obj.__qualname__] = (name, obj)
    return list(found.values())


def test_the_fetcher_scan_sees_the_one_that_hid_from_it():
    """Guard on the guard: `SnapshotFetcher` must be in scope."""
    names = {n for n, _ in _fetcher_classes()}
    assert "SnapshotFetcher" in names, (
        "SnapshotFetcher is not being enumerated — the alignment invariant is "
        "blind to the fetcher most likely to serve a stale-shaped frame."
    )
    assert len(names) >= 5, f"only {len(names)} fetchers found; the scan is too narrow: {names}"


def test_every_fetcher_goes_through_the_shared_alignment_contract():
    """Rule 1, structurally: C2's invariant is that no provider has its own copy.

    `cloud` and `local` used to floor their labels and return raw rows, which is
    how a two-day partial week became a full row at ~2/7 volume. A new provider
    that hand-rolls alignment is the same defect returning.
    """
    offenders = []
    for name, obj in _fetcher_classes():
        fetch = getattr(obj, "fetch_metric", None)
        if fetch is None or inspect.isabstract(obj):
            continue
        try:
            src = inspect.getsource(fetch)
        except (OSError, TypeError):  # pragma: no cover
            continue
        if "_align_to_spine" in src:
            continue
        # The mock is the one exemption, and the reason is load-bearing: it
        # *generates onto* `period_spine`, so its output cannot have a gap, a
        # partial edge period or a stray label for alignment to fix. That is
        # also exactly why the suite never saw C2 — every provider that could
        # be misaligned was one the tests did not exercise. Assert the reason
        # rather than the name, so a mock that stopped generating on the spine
        # (and started needing alignment like everyone else) fails here.
        if name == "MockDataFetcher" and "period_spine" in src:
            continue
        # A wrapper that reaches the contract through its own helper:
        # `SnapshotFetcher` re-aligns a hit in `_realign_snapshot`, because a
        # snapshot outlives the code that wrote it and nothing fingerprints the
        # *engine's* alignment rules the way `definition_sha` fingerprints the
        # metric's.
        if "_realign_snapshot" in src:
            continue
        offenders.append(name)
    assert not offenders, (
        f"{offenders} implement fetch_metric without calling `_align_to_spine`. "
        "Every provider shares one date-alignment contract (roadmap C2)."
    )


def test_every_sliced_fetcher_coerces_timezones():
    """Rule 1 on the path the C1/C2 sweep never reached (roadmap C23).

    `_sliced_long` — the shared reshape both semantic-layer providers use —
    floored its labels and never dropped a timezone, so a tz-aware sliced
    frame survived every check and then reindexed all-NaN against the tz-naive
    spine in `slices._fill_by_kind`, where the flow branch turned it into a
    panel of invented zeros: the C1 symptom, in a surface the invariant above
    is structurally blind to because it only inspects `fetch_metric`. Every
    concrete `fetch_metric_sliced` must reach `_to_naive_dates` — directly, or
    through `_sliced_long`, or by delegating to a wrapped fetcher that does.
    """
    offenders = []
    for name, obj in _fetcher_classes():
        fetch = obj.__dict__.get("fetch_metric_sliced")
        if fetch is None:
            continue  # inherits the base refusal; nothing fetches
        try:
            src = inspect.getsource(fetch)
        except (OSError, TypeError):  # pragma: no cover
            continue
        if "_to_naive_dates" in src or "_sliced_long" in src:
            continue
        # The mock's exemption, with its reason asserted like fetch_metric's:
        # its sliced frame derives its dates from its own generated series
        # (which the class produces on `period_spine`), so there is no external
        # timestamp to coerce. If it ever starts parsing dates it did not
        # generate, this stops matching and it fails here like anyone else.
        if name == "MockDataFetcher" and "period_spine" in inspect.getsource(obj):
            continue
        # A read-through wrapper serves frames its inner provider (or its own
        # write path) already coerced; `store.read_sliced` hits are re-parsed
        # from parquet, which cannot re-attach a zone the writer dropped.
        if "self.inner.fetch_metric_sliced" in src:
            continue
        offenders.append(name)
    assert not offenders, (
        f"{offenders} implement fetch_metric_sliced without the shared date "
        "coercion (`_to_naive_dates` / `_sliced_long`). A tz-aware sliced frame "
        "becomes an all-zero panel downstream (roadmap C23)."
    )

    # And the shared reshape itself must hold the coercion, or every provider
    # routing through it passes the scan while the defect stands.
    assert "_to_naive_dates" in inspect.getsource(data_fetch._sliced_long)


# --- Rule 2: every cache on TreeState is bounded ------------------------------


def test_every_cache_on_tree_state_is_bounded():
    """Rule 2, structurally: enumerate the dataclass, don't list the caches.

    `traces` is a `TraceView` onto the process-wide, byte-bounded `TraceStore`;
    the frame caches are `BoundedCache`. A plain `dict` default here is the
    `slice_cache` defect returning under a new name.
    """
    unbounded = []
    for field in dataclasses.fields(trees_mod.TreeState):
        factory = field.default_factory
        if factory is dataclasses.MISSING:
            continue
        value = factory()
        if not isinstance(value, dict):
            continue
        if field.name == "traces":
            continue  # replaced with a TraceView at load; covered below
        # `earliest` is keyed by *metric name*, so it is bounded by the tree
        # the operator wrote, not by anything a caller can vary. The rule is
        # about caches a request can grow — distinct windows, distinct
        # dimensions, distinct analysis dates. Named rather than pattern-matched
        # on the field name, so a genuinely unbounded field cannot join it by
        # being called something innocuous.
        if field.name == "earliest":
            continue
        if not isinstance(value, trees_mod.BoundedCache):
            unbounded.append(field.name)
            continue
        # The type is not the bound (roadmap C32, grill H3): this test used to
        # accept any BoundedCache, and the cache was bounded by entry *count*
        # while the thing that grows is a frame's cardinality × window. Every
        # frame cache must carry a byte budget.
        assert value.max_bytes > 0, (
            f"TreeState.{field.name} is a BoundedCache with no byte budget. "
            "An entry scales with dimension cardinality times the loaded "
            "window (~154 MB measured at 5,000 slice values × 830 days), so "
            "a count bound alone is rule 2's defect with extra steps."
        )
    assert not unbounded, (
        f"{unbounded} default to an unbounded dict on TreeState. Every cache "
        "here grows with distinct user-chosen windows until the process is "
        "OOM-killed (roadmap C8, 2.18). Use BoundedCache."
    )


def test_bounded_cache_evicts_by_bytes_not_only_count():
    """The byte budget must actually fire (roadmap C32): a cache whose entries
    are large evicts long before its entry count, and an entry bigger than the
    whole budget is never cached at all (it would evict everything to be
    evicted next)."""
    frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=1000), "v": 1.0})
    per = trees_mod.BoundedCache._nbytes(frame)
    assert per > 0, "a DataFrame must measure as more than zero bytes"

    cache = trees_mod.BoundedCache(max_entries=64, max_bytes=int(per * 3.5))
    for i in range(6):
        cache[i] = frame.copy()
    assert len(cache) == 3, "byte eviction should hold ~3 entries, count allows 64"
    assert cache.total_bytes <= cache.max_bytes
    assert set(cache) == {3, 4, 5}, "eviction must be oldest-first"

    huge = pd.DataFrame({"v": np.zeros(10)})
    big_budget = trees_mod.BoundedCache(max_entries=64, max_bytes=1)
    big_budget[0] = huge
    assert len(big_budget) == 0, "an entry over the whole budget is not cached"

    # Bookkeeping survives overwrite and clear — a drifting total_bytes is the
    # M4 failure class (a budget that silently stops firing).
    cache[3] = frame.copy()
    assert cache.total_bytes == sum(cache._sizes.values())
    cache.clear()
    assert cache.total_bytes == 0 and not cache._sizes


def test_no_async_route_calls_the_engine_inline():
    """Every engine call in an async handler goes through `asyncio.to_thread`
    (roadmap C33, grill H4): `GET /shapley` ran the O(2ⁿ) enumeration on the
    event loop — a measured 1.09s stall freezing /health, every /progress poll
    and every /ui asset — while every neighbouring route did it right. The
    guard is the property, not the four call sites of the day: a *direct*
    call to a heavy engine function inside any `async def` here fails.

    `resolve_reference_window` is allowed by name: it is date arithmetic on
    already-loaded metadata, and wrapping every trivial helper would bury the
    rule. A sync helper (like `_run_slice`) may call the engine freely — it
    only ever runs inside `to_thread`.
    """
    heavy = {
        "run_rca",
        "run_scenario",
        "shapley_attribution",
        "fit_metric",
        "slice_attribution",
        "entity_flows",
        "load_tree",
        "_fit_summary",
        "_run_slice",
    }
    source = (PACKAGE / "api" / "main.py").read_text()
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "id", None)
            if name in heavy:
                offenders.append(f"{node.name}:{call.lineno} calls {name}()")
    assert not offenders, (
        f"{offenders}: engine work invoked inline in an async handler. Route "
        "it through `async with tree.lock: await asyncio.to_thread(...)` like "
        "its neighbours, or the event loop freezes for the duration (C33)."
    )


def test_the_trace_store_is_bounded_by_size_not_only_by_count():
    """Rule 2's sharp edge: an entry scales with the loaded window.

    A cached fit measured 13.4 MB on an 830-day window, so a 256-entry cap
    reaches ~3.4 GB against the demo's 2 GB box. Counting entries cannot be
    made safe by choosing a smaller number.
    """
    store = trees_mod.TraceStore()
    assert getattr(store, "max_bytes", 0) > 0, (
        "TraceStore must carry a byte budget, not only an entry count (2.18)."
    )


def test_every_window_scaled_field_on_a_fit_is_metered_or_named():
    """Rule 2's other half: the budget only bounds what the meter can see.

    `_trace_nbytes` summed the trace's xarray groups and nothing else, which
    was right while the trace was the only per-period thing on a `FitResult`.
    It stopped being right quietly — `dates` is one value per fitted period,
    and roadmap S10's `ppc_band` is six — and each was individually small
    enough to justify not counting, which is the `slice_cache` argument
    verbatim.

    So the classification is enumerated rather than assumed: every field on
    the dataclass is either metered, bounded by the tree the operator wrote,
    or a named exception with a reason. A new field lands in none of the three
    and fails here, which is the only moment anyone will ask the question.
    """
    metered = {"trace", "dates", "ppc_band"}
    # Bounded by the tree's shape (a name, a grain, a parent list, a fixed
    # diagnostics block), not by the window a caller loaded.
    bounded_by_the_tree = {
        "target",
        "parents",
        "y_mean",
        "y_std",
        "x_stds",
        "inference_method",
        "fit_end",
        "grain",
        "diagnostics",
    }
    # `summary_json` scales with the window (one `az.summary` row per `trend`
    # latent) but is filled lazily on the first `GET /metrics/{name}`, after
    # the store already weighed this entry. Counting it would need the store
    # to re-measure on read, which is a different design; the exception is
    # named here so it stays deliberate.
    measured_too_late = {"summary_json"}

    names = {f.name for f in dataclasses.fields(model_mod.FitResult)}
    unclassified = names - metered - bounded_by_the_tree - measured_too_late
    assert not unclassified, (
        f"{sorted(unclassified)} are new fields on FitResult that this test has "
        "never seen. If a field carries one value per fitted period, "
        "`_trace_nbytes` must count it — the trace store's budget is the only "
        "thing standing between a wide window and an OOM (rule 2). If it does "
        "not, add it to `bounded_by_the_tree` here and say why."
    )

    # And `metered` is a claim, so it is tested rather than asserted: the
    # measured size has to actually move when each of those fields grows.
    class _Fit:
        trace = None
        dates = pd.DatetimeIndex([])
        ppc_band = None

    base = trees_mod._trace_nbytes(_Fit())

    long_dates = _Fit()
    long_dates.dates = pd.date_range("2024-01-01", periods=1000)
    assert trees_mod._trace_nbytes(long_dates) > base, "`dates` is not metered"

    with_band = _Fit()
    with_band.ppc_band = {"n_periods": 1000}
    assert trees_mod._trace_nbytes(with_band) > base, "`ppc_band` is not metered"


# --- Rule 3: no engine result reaches an encoder unsanitized ------------------


def _strict(payload) -> None:
    json.dumps(payload, allow_nan=False)


def test_a_degenerate_tree_still_encodes_strictly(tmp_path, monkeypatch):
    """Rule 3, end to end through every attribution path that can carry a NaN.

    One zero-denominator period used to reach Starlette's `allow_nan=False`
    encoder as an unhandled 500, and `round_floats` turned the same NaN into
    `null` for an agent (C17). The formula path was fixed then — and the
    posterior path of the same function published NaN for another year,
    because this test's one scenario contained no probabilistic node (roadmap
    C29, grill H1). So the tree now carries all three degenerate shapes:

    - a zero denominator on a derived rate (`aov`) — the C17 formula case;
    - a zero-variance parent (`promo`, a real parent of `bookings`) — the C4a
      case (an earlier edition declared `promo` standalone while its docstring
      called it a parent; the flat-parent path was never actually wired);
    - a probabilistic node (`demand`) whose rate parent is **undefined inside
      the analysis window but outside the fit window** — the fit never sees
      the NaN, and only the attribution-time refusal stands between it and
      the encoder. The fit itself is a stub through the trace-cache seam, so
      no sampler runs here.

    The property asserted is the strict-encoding half of "every float a node
    payload carries is finite or None": `json.dumps(..., allow_nan=False)`
    raises on any non-finite float, and withheld values are `None`.
    """
    tree = tmp_path / "degenerate.yml"
    tree.write_text(
        "provider:\n  type: mock\n"
        "metrics:\n"
        "  - name: order_count\n    source: mock.order_count\n    kind: flow\n"
        "  - name: revenue\n    source: mock.revenue\n    kind: flow\n"
        "  - name: promo\n    source: mock.promo\n    kind: flow\n"
        "  - name: aov\n    source: mock.aov\n    kind: rate\n"
        '    formula: "revenue / order_count"\n'
        "    parents: [revenue, order_count]\n"
        "  - name: demand\n    source: mock.demand\n    kind: flow\n"
        "    parents: [aov]\n"
        "  - name: bookings\n    source: mock.bookings\n    kind: flow\n"
        '    formula: "demand + promo"\n'
        "    parents: [demand, promo]\n"
    )
    # monkeypatch, not os.environ: the app reads these at lifespan, so leaving
    # them set points every later test in the session at a tmp_path tree that
    # no longer exists. (Found the hard way — it errored two README tests that
    # pass in isolation, which is the signature of exactly this mistake.)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")

    import arviz as az
    from fastapi.testclient import TestClient

    from breakdown.api.main import app
    from breakdown.engine.model import FitResult

    analysis = {"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"}

    with TestClient(app, raise_server_exceptions=False) as client:
        state = app.state.trees["degenerate"]
        frame = state.data.frames["day"]
        # the structural zero `_align_to_spine` manufactures for a flow
        # denominator, and a parent held flat (the C4 production shape)
        frame.loc[frame.index[-3], "order_count"] = 0.0
        frame["promo"] = 0.0
        # 1.11c's undefined rate period, placed inside the analysis window so
        # the fit (which ends at analysis_start) can never have seen it — the
        # exact H1 shape.
        frame.loc[frame.index[-3], "aov"] = float("nan")

        # A stub NUTS fit through the trace-cache seam: `run_rca` reuses it
        # via `cached_fit_is_usable` and fits nothing. The refusal under test
        # fires before any of the stub's numbers are read.
        rng = np.random.default_rng(0)
        fit_dates = pd.date_range("2024-01-01", "2024-03-26", freq="D")
        stub = FitResult(
            trace=az.from_dict(
                posterior={
                    "beta_raw": rng.normal(size=(2, 50, 1)),
                    "trend": rng.normal(size=(2, 50, len(fit_dates))),
                }
            ),
            target="demand",
            parents=["aov"],
            y_mean=0.0,
            y_std=1.0,
            x_stds=np.array([1.0]),
            dates=fit_dates,
            inference_method="nuts",
            fit_end=analysis["analysis_start"],
        )
        state.traces[("demand", analysis["analysis_start"])] = stub

        for path in ("/rca/aov", "/rca/revenue", "/rca/bookings"):
            r = client.post(path, params=analysis)
            assert r.status_code != 500, (
                f"POST {path} returned 500 on a degenerate but ordinary tree — a "
                "non-finite value reached the encoder (roadmap C17/C29)."
            )
            _strict(r.json())

        # The poisoned probabilistic node degrades by name inside a wider
        # analysis ("one bad node does not end the analysis") …
        body = client.post("/rca/bookings", params=analysis).json()
        demand = body["nodes"]["demand"]
        assert demand["status"] == "attribution_failed"
        assert "aov" in demand["status_reason"]

        # … and refuses by name when it is itself the target.
        r = client.post("/rca/demand", params=analysis)
        assert r.status_code == 422, (
            "the H1 shape must be a named refusal for the target, not a "
            f"published NaN (got {r.status_code})"
        )
        _strict(r.json())
        assert "aov" in r.json()["detail"]

        r = client.get("/metrics/aov")
        assert r.status_code == 200
        _strict(r.json())


def test_round_floats_never_emits_a_non_finite_number():
    """Rule 3 on the agent-facing side: `null` is the MCP shape of a NaN."""
    from breakdown.mcp.shaping import round_floats

    out = round_floats({"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": float("-inf")}})
    flat = [out["a"], *out["b"], out["c"]["d"]]
    assert all(v is None or (isinstance(v, float) and math.isfinite(v)) for v in flat)


# --- Rule 4: every coalition enumeration is capped ----------------------------


def _modules_enumerating_subsets():
    """Every module that imports itertools.combinations / permutations."""
    found = []
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "itertools":
                if any(a.name in ("combinations", "permutations") for a in node.names):
                    found.append(path)
                    break
    return found


def test_every_subset_enumeration_declares_a_cap():
    """Rule 4, structurally: find the enumerations, don't list them.

    Both known enumerations are O(2^n) and both run while holding the tree's
    lock. `simulate.py` capped at 10 and explained why; `compute_shapley` had no
    cap at all until 2.18, at 20s for 12 parents and 80s for 14.
    """
    modules = _modules_enumerating_subsets()
    assert modules, "expected to find the subset enumerations; did the import style change?"
    uncapped = []
    for path in modules:
        text = path.read_text()
        if "_MAX_" not in text:
            uncapped.append(path.name)
    assert not uncapped, (
        f"{uncapped} enumerate subsets with no `_MAX_*` cap declared. An O(2^n) "
        "loop under the request lock needs a documented refusal (2.18)."
    )


@pytest.mark.parametrize(
    "cap", [(model_mod, "_MAX_SHAPLEY_PARENTS"), (simulate_mod, "_MAX_SOURCES")]
)
def test_the_declared_caps_are_small_enough_to_be_caps(cap):
    """A cap of 20 would be 1,000x the work of 10 and is not a cap.

    Measured end to end through `run_rca`: 10 parents ~3.5s, 12 ~20s, 14 ~80s.
    """
    module, name = cap
    value = getattr(module, name)
    assert 2 <= value <= 12, f"{name} = {value}: 2^{value} coalitions is not a bound"


def test_compute_shapley_refuses_above_its_cap():
    """Rule 4 behaviourally, at the chokepoint a library caller cannot bypass."""
    n = model_mod._MAX_SHAPLEY_PARENTS + 1
    parents = [f"p{i}" for i in range(n)]
    formula = " + ".join(parents)
    with pytest.raises(ValueError, match="too many parents"):
        model_mod.compute_shapley(
            formula, parents, {p: 1.0 for p in parents}, {p: 2.0 for p in parents}
        )


# --- The 2.16 mount invariant: one router, included twice ---------------------

TREE_PREFIX = "/trees/{tree_id}"


def test_every_shared_route_is_mounted_bare_and_tree_prefixed():
    """2.16's load-bearing property: the aliases cannot drift, because there is
    one `APIRouter` and it is included twice.

    Enumerates `router.routes` rather than listing the ten endpoints, for the
    same reason every test above enumerates: a route added tomorrow must be
    covered on the day it is added, and a test that pinned today's ten would
    pass while an eleventh reached only one mount.

    This is asserted against `app.openapi()` — the resolved, public view of what
    the app serves — and not by walking `app.routes`. `app.routes` is not a flat
    list: since FastAPI 0.137.0 each `include_router` appends one lazy
    `_IncludedRouter` node rather than copying the routes in, so counting
    `.path` attributes there under-reports every included route as absent. That
    is exactly how this defect presented — a probe over `app.routes` showed one
    pathless object where ten routes were expected, on an app whose ten routes
    were serving 200s the whole time.
    """
    spec_paths = app.openapi()["paths"]
    served = {(template, method.upper()) for template, ops in spec_paths.items() for method in ops}

    missing = []
    for route in router.routes:
        # `include_in_schema=False` would hide a route from the schema and so
        # from this check. Nothing on this router sets it; if something ever
        # does, it must be excluded deliberately here rather than silently
        # dropping out of the invariant.
        assert getattr(route, "include_in_schema", True), (
            f"{route.path} is include_in_schema=False, so this invariant cannot "
            "see it. Either put it back in the schema or name it here."
        )
        for method in route.methods:
            for expected in (route.path, TREE_PREFIX + route.path):
                if (expected, method) not in served:
                    missing.append(f"{method} {expected}")

    assert not missing, (
        f"{sorted(missing)} are not served. Every route on the shared `router` is "
        "mounted twice — bare (the default tree) and under "
        f"`{TREE_PREFIX}` — by the two `include_router` calls at the bottom of "
        "breakdown/api/main.py. A route reaching only one mount means an alias "
        "has drifted, which is the one thing 2.16's single-router design exists "
        "to prevent."
    )


def test_the_router_is_not_consumed_by_being_included():
    """The double include must not mutate the router it includes.

    `app.include_router(router)` twice is only safe if the first call leaves
    `router.routes` alone — otherwise the second mount would see a consumed
    object and the tree-prefixed aliases would be silently short. Asserted
    directly, because it is the assumption the mounting above rests on and it is
    a property of FastAPI rather than of our code.
    """
    before = list(router.routes)
    probe = FastAPI()
    probe.include_router(router)
    probe.include_router(router, prefix=TREE_PREFIX)
    assert list(router.routes) == before, (
        "include_router mutated the router it was handed; the two mounts in "
        "breakdown/api/main.py can no longer share one router object."
    )


# --- No published number claims resolution it does not have -------------------


def test_a_saturated_direction_probability_publishes_its_ceiling():
    """A proportion over n replicates has nothing between 1 − 1/n and 1.

    `prob_same_direction` is `max((x>0).mean(), (x<0).mean())` over `_N_BOOT`
    replicates, so 1.0 is not a measurement of certainty — it is the estimator
    saturating, which happens most readily where the evidence is thinnest. It
    was published as `1.00` and rendered `P(dir) 100.0%` with no qualifier,
    one line away from the code that withholds a degenerate probability as "a
    confidence read off no information at all".
    """
    n = stats_mod.N_BOOT
    value, censored = stats_mod.prob_same_direction(np.linspace(1.0, 2.0, n))
    assert censored, "every replicate on one side is a saturated count, not certainty"
    assert value == pytest.approx(1.0 - 1.0 / n)

    # Not saturated: one replicate the other way is a representable proportion
    # and is published exactly, uncensored.
    mixed = np.linspace(1.0, 2.0, n)
    mixed[0] = -1.0
    value, censored = stats_mod.prob_same_direction(mixed)
    assert not censored and value == pytest.approx(1.0 - 1.0 / n)


def test_an_exact_sample_is_not_censored():
    """The one case where 1.0 is honest: no spread at all.

    A `simulate` propagation straight through an identity from a pinned
    intervention is exact arithmetic, not an estimate of a proportion. Clamping
    it would understate a sign that really is known.
    """
    value, censored = stats_mod.prob_same_direction(np.full(64, 3.0))
    assert value == 1.0 and not censored


def test_every_published_direction_probability_is_representable():
    """The ratchet, end to end on a real decomposition.

    Both attribution paths and every node, pinned to `_N_BOOT` rather than to
    the literal 0.998 — so raising the replicate count moves the bound with it
    instead of silently loosening this test.
    """
    from tests.synthetic import generate_mock_data, win

    yaml = (
        "metrics:\n"
        "  - name: daily_sessions\n    source: mock.daily_sessions\n"
        "  - name: order_count\n    source: mock.order_count\n"
        "    parents: [daily_sessions]\n"
        "    priors:\n      coefficient:\n"
        '        distribution: "Normal"\n        params: { mu: 0.1, sigma: 0.02 }\n'
        "  - name: average_order_value\n    source: mock.average_order_value\n"
        "  - name: revenue\n    source: mock.revenue\n"
        '    formula: "order_count * average_order_value"\n'
        "    parents: [order_count, average_order_value]\n"
    )
    dag = Parser(yaml).dag
    result = rca_mod.run_rca(
        dag,
        generate_mock_data(n_days=100),
        {},
        "revenue",
        **win(("2024-01-01", "2024-02-15"), ("2024-02-16", "2024-04-09")),
        draws=300,
    )
    ceiling = 1.0 - 1.0 / stats_mod.N_BOOT
    published = [
        (name, c["parent"], c["prob_same_direction"])
        for name, node in result["nodes"].items()
        for c in node.get("contributions") or []
    ]
    assert published, "expected contributions to check; did the fixture stop decomposing?"
    for name, parent, psd in published:
        assert psd is None or 0.5 <= psd <= ceiling, (
            f"{name} <- {parent} publishes prob_same_direction {psd}, outside "
            f"[0.5, 1 - 1/_N_BOOT = {ceiling}]. A proportion over "
            f"{stats_mod.N_BOOT} replicates cannot take that value."
        )


def test_every_direction_probability_goes_through_the_shared_estimator():
    """Structurally: find the idiom, don't list today's three call sites.

    The same one-liner was written out three times — `prob_same_direction` in
    both RCA paths, `prob_concentrated` in `slices.py`, `prob_direction` in
    `simulate.py` — which is exactly how a fix to one of them fails to reach
    the others. There is one estimator now, and a fourth copy of the idiom is
    the defect returning under a new field name.

    The idiom is the two-sided `max(...)` form, which is the one that saturates
    at 1.0. `model._sign_warnings`' one-sided `P(beta > 0)` is a different
    quantity and is never published — it feeds a 0.10 threshold — so it is not
    matched.
    """
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        lines = path.read_text().splitlines()
        # The enclosing def of every line, so the shared estimator can be
        # exempted by name rather than by line number.
        owner = {}
        for node in ast.walk(ast.parse("\n".join(lines))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    owner[ln] = node.name
        for i, line in enumerate(lines, 1):
            if "max(" in line and "> 0).mean()" in line and "< 0).mean()" in line:
                if owner.get(i) == "prob_same_direction":
                    continue
                offenders.append(f"{path.relative_to(PACKAGE.parent)}:{i}")
    assert not offenders, (
        f"{offenders} compute a direction probability inline. Use "
        "`rca.prob_same_direction` / `rca.direction_fields`, which publish the "
        "estimator's resolution ceiling instead of a saturated 1.0."
    )


# --- Every date parameter is validated as a date, at the boundary -------------


def _is_date_param(name: str) -> bool:
    return name.endswith(("_start", "_end", "_date")) or name == "date"


def _endpoint_routes():
    """Every route object that carries an `endpoint`, at any FastAPI version.

    Not `app.routes` alone. FastAPI 0.137.0 made `include_router` append one
    lazy `_IncludedRouter` node per include instead of copying routes into the
    parent, so from 0.137.0 `app.routes` holds two pathless objects where the
    shared router's ten routes used to appear — and this file already carries a
    test whose whole subject is that mistake. The dev environment is locked
    below that version, so a walk of `app.routes` passes locally and finds
    nothing on a fresh resolve.
    """
    from breakdown.api.main import app, router

    seen, out = set(), []
    for r in list(router.routes) + list(app.routes):
        if id(r) not in seen and getattr(r, "endpoint", None) is not None:
            seen.add(id(r))
            out.append(r)
    return out


def _date_params(fn) -> dict:
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
    except Exception:  # pragma: no cover - a handler we cannot introspect
        return {}
    return {n: a for n, a in hints.items() if _is_date_param(n)}


def test_every_date_taking_route_validates_its_dates():
    """Structurally: enumerate the routes, don't list today's date parameters.

    `POST /rca/{name}?analysis_start=` returned 500 — `pd.Timestamp("")` is
    `NaT`, which satisfies a `str` annotation, passes every `is None` guard and
    reaches `snap_window`, where `NaT.normalize()` is an `AttributeError`.
    `analysis_start=banana` was a correct 422, because that spelling raises.
    Two routes already ran the ISO check inline and their four siblings did
    not, so it is one annotated type and this test asks the reviewer's
    question: is there a new date parameter that skipped it?
    """
    from breakdown.api.main import _iso_date

    offenders = []
    for route in _endpoint_routes():
        fn = route.endpoint
        for pname, ann in _date_params(fn).items():
            validators = [
                m
                for m in getattr(ann, "__metadata__", ())
                if isinstance(m, AfterValidator) and m.func is _iso_date
            ]
            if not validators:
                offenders.append(f"{getattr(route, 'path', route)}:{pname}")
    assert not offenders, (
        f"{offenders} take a date and do not validate it. Annotate with "
        "`IsoDate` / `OptionalIsoDate` — a `str` annotation lets the empty "
        "string through as `NaT` and the engine fails with a 500."
    )


def test_every_date_taking_request_model_validates_its_dates():
    """The same rule on the body side: `POST /simulate` takes its window there."""
    unvalidated = []
    for name, field in simulate_mod.ScenarioRequest.model_fields.items():
        if not _is_date_param(name):
            continue
        try:
            simulate_mod.ScenarioRequest(**{name: ""})
        except Exception:
            continue
        unvalidated.append(name)
    assert not unvalidated, (
        f"ScenarioRequest.{unvalidated} accept an empty string as a date. "
        "`pd.to_datetime('')` is NaT and `NaT < NaT` is False, so it survives "
        "every ordering check and fails inside the fit."
    )


@pytest.mark.parametrize("bad", ["", "banana", "2026-13-45", "   ", "\t"])
def test_no_date_parameter_can_produce_a_500(bad, tmp_path, monkeypatch):
    """The ratchet behaviourally: every date parameter on every date-taking
    route, for every shape of bad input, answers 4xx and never 5xx.

    The routes and their date parameters are read off the app rather than
    listed, so a new one is covered the day it is added.
    """
    from fastapi.testclient import TestClient

    from breakdown.api.main import app

    tree = tmp_path / "dates.yml"
    tree.write_text(
        "provider:\n  type: mock\n"
        "metrics:\n"
        "  - name: order_count\n    source: mock.order_count\n    kind: flow\n"
        "  - name: revenue\n    source: mock.revenue\n    kind: flow\n"
    )
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")

    good = {"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"}
    checked = 0
    with TestClient(app, raise_server_exceptions=False) as client:
        for route in _endpoint_routes():
            fn = route.endpoint
            path = getattr(route, "path", "")
            if "{tree_id}" in path or not _date_params(fn):
                continue
            method = "POST" if "POST" in (getattr(route, "methods", None) or ()) else "GET"
            url = path.replace("{name}", "revenue")
            for pname in _date_params(fn):
                params = {**good, pname: bad}
                r = client.request(method, url, params=params)
                checked += 1
                assert r.status_code < 500, (
                    f"{method} {url}?{pname}={bad!r} returned {r.status_code}. "
                    "A date parameter the caller got wrong is a 422, never a 500."
                )
                assert r.status_code != 200 or pname not in _date_params(fn), (
                    f"{method} {url} accepted {pname}={bad!r}"
                )
    assert checked, "expected to find date-taking routes; did the signatures change?"


def test_the_engine_refuses_a_not_a_date_rather_than_crashing():
    """Below the API, for the callers that are not HTTP (the MCP tools).

    `pd.Timestamp('')` is `NaT`, not an exception — the whole reason the empty
    string took a different path from `banana`.
    """
    from breakdown.grains import snap_window, to_date

    for value in ("", None, float("nan"), pd.NaT):
        with pytest.raises(ValueError):
            to_date(value, "analysis_start")
        with pytest.raises(ValueError):
            snap_window(value, "2024-01-31", "week")
    assert snap_window("2024-01-01", "2024-01-31", "week") is not None


# --- An absent declaration stays absent through serialization -----------------


def test_an_undeclared_direction_serializes_as_undeclared():
    """`direction` defaulted to `up_is_good` in the parser, and `/dag`
    serializes with `model_dump()` — so "the author did not say" reached the
    browser indistinguishable from "the author said up is good", and app.js's
    own `|| "up_is_good"` fallback could never fire. `churn_arpu` rose 18.5%
    and rendered green ("improved") while carrying 27.3% of the damage.

    Checked across every tree that ships, so the property is enumerated rather
    than pinned to one metric: a metric that declares nothing serializes
    `direction: null`, and one that declares something round-trips it.
    """
    trees = (
        sorted((PACKAGE.parent / "knowledge").glob("*_tree.yml"))
        + sorted((PACKAGE / "examples").glob("*.yml"))
        + sorted((PACKAGE.parent / "demo").glob("*_tree.yml"))
    )
    assert trees, "expected to find the shipped trees"
    # White Cube's provider path is an env placeholder (see the demo fixture);
    # any value parses, and nothing here reaches a provider.
    os.environ.setdefault("WHITE_CUBE_DBT_PROJECT", "/nonexistent/white-cube-has-no-provider")
    seen_declared = seen_undeclared = 0
    for path in trees:
        parser = Parser(path.read_text())
        for name in parser.dag.nodes:
            defn = parser.dag.nodes[name]["definition"]
            dumped = defn.model_dump()
            assert dumped["direction"] == defn.direction
            if defn.direction is None:
                seen_undeclared += 1
            else:
                assert defn.direction in ("up_is_good", "down_is_good", "neutral")
                seen_declared += 1
    # Only assert the distribution when both kinds are reachable. `demo/` and
    # `knowledge/` are excluded from the sdist, so the shipped suite sees one
    # tree — the bundled example, which declares nothing — and a test that
    # demands a declared example there fails on the artifact rather than on the
    # code. The property that matters is the one above: undeclared stays
    # undeclared through serialization.
    if not seen_declared:
        return
    assert seen_undeclared and seen_declared, (
        "expected the shipped trees to contain both declared and undeclared "
        f"directions (declared={seen_declared}, undeclared={seen_undeclared})"
    )


# --- A definitional zero is not a measured one (roadmap 1.11a) ----------------


def _derived_rate_tree():
    """The smallest tree exercising both halves of 1.11: a derived rate over
    two flows, beside a fetched formula node."""
    return """
provider: {type: mock}
metrics:
  - name: sessions
    source: t.metrics.sessions
  - name: orders
    source: t.metrics.orders
  - name: conversion_rate
    kind: rate
    formula: "orders / sessions"
    parents: [orders, sessions]
  - name: revenue
    source: t.metrics.revenue
    formula: "orders * conversion_rate"
    parents: [orders, conversion_rate]
"""


def test_a_derived_nodes_zero_is_distinguishable_from_a_measured_one():
    """The defect class this project keeps finding, in its newest shape.

    `unexplained: 0` means "the decomposition reconciled with the node's own
    fetched series" for a measured node, and "nobody checked anything" for a
    derived one. Rendered identically they are indistinguishable, exactly like
    `null >= 0` painting an unanalyzed node green, an absent `direction`
    becoming a claim, and a structurally-absent component published with a
    zero-width interval.

    So: the *payload* must carry the difference, on every surface that carries
    the number at all. This checks the engine and the MCP shaping; the UI is
    checked by `test_every_surface_that_prints_unexplained_labels_which_zero`.
    """
    from breakdown.engine.rca import run_rca
    from breakdown.mcp.shaping import compact_rca

    parser = Parser(_derived_rate_tree())
    fetcher = data_fetch.MockDataFetcher(dag=parser.dag)
    frames, grains, kinds, denoms = {}, {}, {}, {}
    for m in parser.config.metrics:
        grains[m.name], kinds[m.name] = m.grain, m.kind
        if m.denominator:
            denoms[m.name] = m.denominator
        if not m.derived:
            frames[m.name] = fetcher.fetch_metric(m.name, "2024-01-01", "2024-03-31")
    # The derived node is computed, not fetched — the whole point of 1.11a.
    assert "conversion_rate" not in frames
    frames["conversion_rate"] = pd.DataFrame(
        {
            "date": frames["orders"]["date"],
            "conversion_rate": frames["orders"]["orders"].to_numpy(float)
            / frames["sessions"]["sessions"].to_numpy(float),
        }
    )
    from breakdown.grains import build_grained as _bg

    data = _bg(frames, grains, kinds, denoms)
    out = run_rca(
        parser.dag,
        data,
        {},
        "revenue",
        reference_start="2024-01-01",
        reference_end="2024-01-28",
        analysis_start="2024-02-01",
        analysis_end="2024-02-28",
    )
    derived = out["nodes"]["conversion_rate"]
    measured = out["nodes"]["revenue"]

    assert derived["unexplained"] == 0.0
    assert derived["unexplained_status"] == "definitional"
    assert measured["unexplained_status"] == "measured", (
        "a node with its own `source` was compared against its identity; its "
        "residual is a measurement whatever its value"
    )
    # And the two zeros stay distinguishable after compaction for an agent,
    # which is where a field dropped 'for token economy' would erase them.
    compact = compact_rca(out)
    assert compact["nodes"]["conversion_rate"]["unexplained_status"] == "definitional"
    assert compact["nodes"]["revenue"]["unexplained_status"] == "measured"


def _js_code(path) -> str:
    """JS source with // line comments and /* block comments */ stripped, so a
    token count means code, not commentary (grill L6). Naive about comment
    markers inside string literals — fine for counting identifiers that never
    appear in user-facing strings."""
    import re as _re

    src = path.read_text()
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
    src = _re.sub(r"^\s*//.*$", "", src, flags=_re.M)
    return src


def test_every_rca_node_field_reaches_a_render_site():
    """The invariant that would have caught grill H7 (roadmap C35): the engine
    emitted `inference_method` on every RCA node, MCP published it with a
    comment on why an agent needs it — and no UI surface ever read it, so an
    ADVI analysis rendered byte-identical to a NUTS one for a year.

    Enumerated from the payload itself: `_node_out()` with no overrides is the
    engine's own list of every field a node can carry. Each must appear in the
    frontend (app.js or disclosures.js, comments stripped) at least once, or
    be a named exception with a reason. A new engine field lands in neither
    and fails here — which is the only moment anyone will ask the question.
    """
    fields = set(rca_mod._node_out().keys())
    js = _js_code(PACKAGE / "static" / "app.js") + _js_code(
        PACKAGE / "static" / "disclosures.js"
    )
    # Named exceptions, each with the reason it is not rendered:
    unrendered = {
        # consumed to *position* the k̂ figure's ± suffix, via khatFigure's
        # arithmetic — the name appears there, so it is not in this set; kept
        # as documentation of the pattern for the next field.
    }
    missing = sorted(f for f in fields - set(unrendered) if f not in js)
    assert not missing, (
        f"RCA node fields the engine emits and no frontend surface reads: "
        f"{missing}. Render each on at least one surface, or add it to the "
        "named exceptions here with the reason a reader never needs it "
        "(roadmap C35 — a correct payload rendered incompletely is the fifth "
        "rule's defect)."
    )


def test_every_surface_that_prints_unexplained_labels_which_zero():
    """The fifth rule, which has no runner — so it is enumerated in the source.

    There is no JS test runner here (MVP-first, deliberately), and the export
    is what circulates without its author, so a label present in the live table
    and absent from the export is exactly the drift this checks for. Every
    place `app.js` writes an `unexplained` row must build its label through
    `unexplainedRow`, never from a string literal.
    """
    app_js = _js_code(PACKAGE / "static" / "app.js")
    literal_rows = [
        line
        for line in app_js.splitlines()
        if "unexplained</td>" in line and "unexplainedRow" not in line
    ]
    assert not literal_rows, (
        "an `unexplained` row is built from a string literal instead of "
        f"`unexplainedRow(node)`, so it cannot distinguish a definitional zero "
        f"from a measured one: {literal_rows}"
    )
    # Both surfaces must actually *call* it (grill L6: the old form counted
    # substrings, so the definition line — and even a comment — satisfied it).
    # Comments are stripped by `_js_code`, and the definition lives in
    # disclosures.js now, so every hit here is a genuine call expression.
    calls = [
        line for line in app_js.splitlines()
        if "unexplainedRow(" in line and not line.lstrip().startswith("function ")
    ]
    assert len(calls) >= 2, (
        f"expected the live table and the exported report each to call "
        f"`unexplainedRow`; found {len(calls)} call sites"
    )
    disclosures = _js_code(PACKAGE / "static" / "disclosures.js")
    assert "definitional" in disclosures, "the UI never mentions a definitional zero"


# --- No rate aggregate is an average of per-period ratios (roadmap 1.11c) -----


def test_no_rate_window_aggregate_is_computed_by_averaging_ratios():
    """Enumerate the package for anything that reduces a metric's window to a
    scalar, and require it to go through the one kind-aware entry point.

    `resample_up` has always refused to average a rate over *time*; the window
    aggregate is the same operation under a different name, and it did average
    them — every rate's `baseline`/`actual` in RCA and every rate's what-if
    baseline. Pinning today's two call sites would not catch the third, so this
    scans for the call instead: `window_mean` is flow/stock-only, and its only
    legitimate caller is `node_window_value`, which routes a rate to
    `grains.rate_window_value`.
    """
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in ("node_window_value", "window_mean"):
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "window_mean":
                    offenders.append(f"{path.name}:{node.name}")
    assert not offenders, (
        f"{offenders} call `window_mean` directly. It is the arithmetic mean of "
        "a window and is wrong for a `kind: rate` metric — a window's rate is "
        "Σnumerator / Σdenominator. Call `node_window_value`, which applies the "
        "node's kind."
    )


def test_a_weighted_rate_aggregate_is_not_the_average_of_the_ratios():
    """The property behind the scan: the two answers genuinely differ, and the
    weighted one is the one that reconciles with the components.

    Without this the scan above could be satisfied by a `node_window_value`
    that quietly averaged anyway.
    """
    from breakdown.grains import rate_window_value

    numerator = np.array([10.0, 90.0])
    denominator = np.array([10.0, 30.0])
    rates = numerator / denominator  # 1.0 and 3.0

    weighted = rate_window_value(rates, denominator)
    assert weighted == pytest.approx(numerator.sum() / denominator.sum())  # 2.5
    assert weighted != pytest.approx(rates.mean())  # 2.0 — the wrong answer

    # An undefined period contributes to neither sum, so it drops out rather
    # than poisoning the aggregate: `0/0` is not `0`.
    with_undefined = rate_window_value(
        np.array([1.0, 3.0, float("nan")]), np.array([10.0, 30.0, 0.0])
    )
    assert with_undefined == pytest.approx(2.5)

    # No weights at all: the disclosed fallback, over the *defined* periods.
    assert rate_window_value(np.array([1.0, 3.0, float("nan")]), None) == pytest.approx(2.0)
    # Nothing defined at all is no value, never a zero.
    assert math.isnan(rate_window_value(np.array([float("nan")]), np.array([0.0])))


def test_the_payload_says_which_of_the_two_aggregates_a_rate_reports():
    """A period mean and a component aggregate must not read alike either.

    The scan above stops anything from *computing* the wrong number. This is the
    other half, and the one the fifth rule is about: the two arithmetics produce
    one field called `actual`, and a reader given no label will assume the right
    one. Worse, the fallback has two causes — a metric that has no denominator
    (a median: this mean is the only number there is) and a tree nobody has
    declared one on — and the remedy for the second is nonsense advice for the
    first, which is what `doctor` used to give.
    """
    from breakdown.engine.windows import (
        node_window_value,
        rate_window_method,
        rate_window_method_reason,
    )
    from breakdown.grains import build_grained

    dates = pd.date_range("2024-01-01", periods=4, freq="D")
    frames = {
        "den": pd.DataFrame({"date": dates, "den": [10.0, 30.0, 10.0, 30.0]}),
        "declared": pd.DataFrame({"date": dates, "declared": [1.0, 3.0, 1.0, 3.0]}),
        "answered": pd.DataFrame({"date": dates, "answered": [1.0, 3.0, 1.0, 3.0]}),
        "silent": pd.DataFrame({"date": dates, "silent": [1.0, 3.0, 1.0, 3.0]}),
    }
    kinds = {"den": "flow", "declared": "rate", "answered": "rate", "silent": "rate"}
    data = build_grained(
        frames,
        dict.fromkeys(frames, "day"),
        kinds,
        {"declared": "den"},
        {"answered": "a median — not Σnum / Σden for any pair of series"},
    )
    start, end = dates[0], dates[-1]
    method = {n: rate_window_method(data, n, start, end) for n in frames}
    assert method == {
        "den": None,  # a flow has one aggregation and it is not in question
        "declared": "components",
        "answered": "period_mean_none_exists",
        "silent": "period_mean_undeclared",
    }
    # The two fallbacks compute the identical number and mean different things,
    # which is the entire reason they are labelled.
    assert node_window_value(data, "answered", start, end) == node_window_value(
        data, "silent", start, end
    )
    assert node_window_value(data, "declared", start, end) != node_window_value(
        data, "silent", start, end
    )
    # And the answered one carries the author's own words, not a generic label.
    assert "median" in rate_window_method_reason(data, "answered", method["answered"])
    assert rate_window_method_reason(data, "declared", "components") is None
    assert "no `denominator`" in rate_window_method_reason(data, "silent", method["silent"])


def test_an_undefined_period_is_never_filled_and_never_dropped():
    """The representation itself, at the boundary and in the frame.

    Two failure modes bracket the right answer. Filling asserts a value the
    source never gave (the C18 shape). Dropping the row silently re-dates every
    later period, because model time, lags and bootstrap blocks are all
    positional — so the frame keeps the row and carries `NaN`.
    """
    from breakdown.grains import _check_contiguous, build_grained

    rows = [("2024-01-01", 0.5), ("2024-01-03", 0.7)]
    out = _spine_call("rate", rows, start="2024-01-01", end="2024-01-03")
    assert len(out) == 3
    assert out["m"].isna().tolist() == [False, True, False]

    frame = pd.DataFrame({"date": pd.to_datetime([d for d, _ in rows]), "m": [v for _, v in rows]})
    grained = build_grained({"m": out}, {"m": "day"}, {"m": "rate"})
    kept = grained.frame("day")
    assert len(kept) == 3, "an undefined value must not remove its period from the spine"
    # And the contiguity check agrees: a NaN is not a hole.
    _check_contiguous(kept, "day", ["m"], widest=3)
    assert len(frame) == 2  # the source really did return two rows


# --- Every shipped rate has been asked what it is a rate of (roadmap 1.11) ---


def _shipped_trees():
    """Every tree this repo ships, wherever it lives.

    `demo/` and `knowledge/` are excluded from the sdist, so the suite running
    against the built artifact sees one tree — the bundled example. A test that
    *demands* the reference tree be present therefore fails on the packaging
    rather than on the code, which has happened here before (see the direction
    invariant above). So: enumerate what is there, assert the property on each,
    and gate any assertion about the *distribution* on both kinds being
    reachable.
    """
    os.environ.setdefault("WHITE_CUBE_DBT_PROJECT", "/nonexistent/white-cube-has-no-provider")
    return (
        sorted((PACKAGE.parent / "knowledge").glob("*_tree.yml"))
        + sorted((PACKAGE / "examples").glob("*.yml"))
        + sorted((PACKAGE.parent / "demo").glob("*_tree.yml"))
    )


def test_every_shipped_rate_either_declares_a_denominator_or_answers_that_it_has_none():
    """The ratchet on 1.11: the unanswered count is zero and may not drift back.

    A rate with neither is not a bug — the window value is the mean of its
    per-period ratios and the fallback is disclosed — but it is an *open
    question*, and the whole argument of 1.11 is that an open question and a
    settled one must not look alike. The trees have all been swept; this is what
    stops the 44th rate arriving with nobody having asked.

    It is deliberately not a parser rule. Making the field mandatory is a
    breaking schema change and the author's call; making it an invariant of
    *this repo's own trees* costs a stranger nothing and holds the line here.
    """
    trees = _shipped_trees()
    assert trees, "expected to find the shipped trees"
    rates = answered = declared = 0
    for path in trees:
        parser = Parser(path.read_text())
        rates += sum(1 for m in parser.config.metrics if m.kind == "rate")
        answered += len(parser.rates_denominator_none)
        declared += sum(1 for m in parser.config.metrics if m.denominator)
        assert parser.rates_denominator_unanswered == [], (
            f"{path.name} has rate(s) nobody has said anything about: "
            f"{parser.rates_denominator_unanswered}. Declare `denominator: "
            '<metric>`, or `no_denominator: "<why>"` where there genuinely is '
            "none — the two are different facts and both are readable."
        )
    assert rates and declared, "expected the shipped trees to contain declared rates"
    # The distribution assertion is gated: only the reference tree carries an
    # answered-none rate, and it is not in the sdist.
    if answered:
        assert declared > answered, (
            "expected most shipped rates to establish a denominator rather than "
            f"declare none (declared={declared}, none={answered})"
        )


def test_an_answered_none_survives_serialization_distinguishably():
    """The C21 rule applied to this field: `/dag` serializes with
    `model_dump()`, so a fact the payload does not carry is a fact no renderer
    can act on. "Nobody has said" and "asked and answered" must be two different
    payloads, not one payload plus a convention.
    """
    for path in _shipped_trees():
        parser = Parser(path.read_text())
        for name in parser.dag.nodes:
            defn = parser.dag.nodes[name]["definition"]
            dumped = defn.model_dump()
            assert dumped["no_denominator"] == defn.no_denominator
            assert dumped["denominator"] == defn.denominator
            # Never both, on any node, in any shipped tree: they are opposite
            # answers to one question.
            assert not (dumped["denominator"] and dumped["no_denominator"])


# --- A fetched identity is checked at load, not only inside a window ---------


def test_a_fetched_formula_node_has_its_identity_checked_at_load(caplog, tmp_path, monkeypatch):
    """Roadmap 1.11a's cheap addition, asserted because nothing else reaches it.

    `unexplained` already reports an identity's drift — but only for the windows
    somebody happens to analyse, so an identity that has been wrong since March
    is invisible until an RCA lands on March. The load-time check runs once over
    the whole loaded window. A derived node is skipped, and the skip is the
    point: there is nothing to check it against, which is exactly what
    `unexplained_status: "definitional"` reports downstream.
    """
    tree = tmp_path / "drift.yml"
    tree.write_text("""
provider: {type: mock}
metrics:
  - name: a
    source: t.metrics.a
  - name: b
    source: t.metrics.b
  - name: measured_sum
    source: t.metrics.measured_sum
    formula: "a + b"
    parents: [a, b]
  - name: derived_sum
    formula: "a + b"
    parents: [a, b]
""")
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-03-31")
    from fastapi.testclient import TestClient

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="breakdown.api.main"):
        with TestClient(app) as client:
            assert client.get("/health").json()["status"] == "ok"
    drift = [r.getMessage() for r in caplog.records if "identity" in r.getMessage()]
    # The mock synthesizes every metric independently, so `measured_sum` really
    # does depart from `a + b` — and the whole point is that the engine says so
    # at load rather than waiting for someone to analyse the right window.
    assert any("measured_sum" in m for m in drift), (
        f"the fetched identity was not checked at load; warnings were {drift}"
    )
    assert not any("derived_sum" in m for m in drift), (
        "a derived node was 'checked' against an identity it is defined by — "
        "there is nothing to compare, and reporting one would be the "
        "definitional zero wearing a measurement's clothes"
    )


# --- Nothing in app.js shadows the global `state` (roadmap 2.21) -------------


def test_no_local_binding_shadows_the_global_state_object():
    """The fifth rule again, and this one cost a whole RCA render.

    Roadmap 2.21 introduced `const state = r.localization || ...` inside
    `sliceResultHtml`, whose *first line* reads the global `state.slices`.
    `const` hoists into the temporal dead zone, so every RCA rendered
    "RCA failed: Cannot access 'state' before initialization" — the whole
    right-hand panel, from a name collision. No JS runner and no red suite;
    it was found by opening the browser, which is exactly the gap the fifth
    rule names, so the cheap half of it is enumerated here instead.

    `state` is the one genuinely global mutable in `app.js` (declared at the
    top, read by nearly every function), so a local of the same name is never
    what the author meant even when it happens to work.
    """
    app_js = (PACKAGE / "static" / "app.js").read_text().splitlines()
    shadows = [
        f"{i + 1}: {line.strip()}"
        for i, line in enumerate(app_js)
        # The global itself is at column 0; anything indented is a local.
        if re.match(r"\s+(const|let|var)\s+state\b", line)
    ]
    assert not shadows, (
        "a local binding named `state` shadows the global state object in "
        f"app.js; every read of the real `state` in that scope throws: {shadows}"
    )
    assert sum(1 for line in app_js if re.match(r"const state\b", line)) == 1, (
        "the global `state` object should be declared exactly once at the top of app.js"
    )


# --- Every MCP tool refuses through the SDK's anticipated-failure channel -----


def test_every_mcp_tool_surfaces_its_refusals():
    """The first rule, one boundary over, and a new SDK release found it.

    `mcp/server.py`'s refusals are the provider boundary's discipline aimed at
    a model instead of a warehouse: name the offending value, name the remedy,
    never approximate. But an MCP tool has *two* failure channels and the SDK
    picks between them by exception type — `ToolError` hands its text to the
    caller, anything else is a crash whose text stays on the server. mcp 2.0.0
    forwarded both, so six refusals raising `ValueError`/`RuntimeError` looked
    correct; mcp 2.1.0 stopped forwarding crash text and all six went opaque at
    once, leaving a model with `Error executing tool run_rca` and no way to
    recover, explain, or stop.

    `@_surface_refusals` is the one place that policy lives. Enumerate the
    tools rather than pinning today's six: a seventh added without it is a
    refusal nobody will ever read.
    """
    tree = ast.parse((PACKAGE / "mcp" / "server.py").read_text())

    def _decorators(fn):
        return {ast.unparse(d) for d in fn.decorator_list}

    tools = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(d.startswith("mcp.tool") for d in _decorators(node))
    ]
    assert len(tools) >= 6, "the MCP tool scan found nothing — has the decorator moved?"

    unguarded = [t.name for t in tools if "_surface_refusals" not in _decorators(t)]
    assert not unguarded, (
        "these MCP tools do not convert their refusals to the SDK's "
        "anticipated-failure type, so the caller sees only 'Error executing "
        f"tool <name>': {unguarded}"
    )


def test_no_mcp_guard_refuses_with_a_bare_exception():
    """The wrapper covers what the engine raises; the module's own guards
    decide *at the raise site* and say so there. A `raise ValueError` sitting
    beside a `raise ToolError` in the same file is exactly the shape of defect
    the four rules exist to catch — the right policy, one line from its
    opposite, with no stated reason."""
    tree = ast.parse((PACKAGE / "mcp" / "server.py").read_text())
    bare = [
        ast.unparse(node)[:80]
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id in {"ValueError", "RuntimeError"}
    ]
    assert not bare, (
        "an MCP guard raises a bare exception; the SDK treats it as a crash "
        f"and withholds the message from the calling model: {bare}"
    )


# --- No orchestrator hardcodes a sampler, and none reuses a fit downward -----
#
# Roadmap S2's second half. `run_rca` and `run_scenario` both passed
# `inference_method="advi"` as a literal, chosen once for speed and never
# revisited; when the choice turned out to be wrong the fix had to be made in
# two files, and making it in one would have been the meta-defect exactly.
# Both properties below are enumerated rather than pinned to today's two call
# sites, because a third orchestrator is the case that matters.

_ENGINE = PACKAGE / "engine"


def _engine_fit_metric_calls():
    """Every `fit_metric(...)` call in the engine, as (module, ast.Call)."""
    out = []
    for path in sorted(_ENGINE.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "fit_metric"
            ):
                out.append((path.name, node))
    return out


def test_no_orchestrator_hardcodes_the_sampler():
    """The sampler a caller gets is the sampler they asked for.

    `inference_method` is a promise about which sampler runs. An orchestrator
    that writes a literal there has decided on the user's behalf, silently, and
    the decision then lives in as many files as there are orchestrators — which
    is how `run_rca` and `run_scenario` came to share a wrong default for a
    release. Passing the parameter through is what makes the choice reachable
    (`POST /rca/{name}?inference_method=advi`) and reviewable in one place.
    """
    calls = _engine_fit_metric_calls()
    assert calls, "expected to find fit_metric calls in the engine; did the import style change?"
    hardcoded = [
        f"{mod}:{kw.value.lineno} inference_method={kw.value.value!r}"
        for mod, call in calls
        for kw in call.keywords
        if kw.arg == "inference_method" and isinstance(kw.value, ast.Constant)
    ]
    assert not hardcoded, (
        "an engine orchestrator passes a literal `inference_method` to fit_metric: "
        f"{hardcoded}. Thread the caller's choice through instead — a sampler picked "
        "on the user's behalf is a promise broken in whatever file it is written in "
        "(roadmap S2)."
    )


def test_every_orchestrator_that_reuses_a_cached_fit_checks_it_is_good_enough():
    """A cached approximation must not answer a request for exact sampling.

    `traces` is shared by every viewer of a process, so without this one
    colleague's deliberate `?inference_method=advi` triage run decides the
    sampler behind everybody else's default analysis of the same window — and
    the payload then names a method nobody chose. Reuse is allowed only
    *upward*, and `cached_fit_is_usable` is the single place that says so.
    """
    users = []
    for mod, _call in _engine_fit_metric_calls():
        text = (_ENGINE / mod).read_text()
        # An orchestrator is a module that both fits on demand and consults a
        # `traces` cache before doing it. A module that only fits (no cache)
        # has no reuse decision to get wrong.
        if "traces" in text and "cached_fit_is_usable" not in text:
            users.append(mod)
    assert not users, (
        f"{users} fit on demand against a `traces` cache without going through "
        "`cached_fit_is_usable`. Reuse is upward-only: a NUTS fit answers an ADVI "
        "request, an approximation does not answer a NUTS one (roadmap S2)."
    )


# --- One sampler budget, read from one place (roadmap C27) --------------------
#
# The same shape as the block above, one parameter over. `POST /analyze/{name}`
# declared `tune=500` while `run_rca` and `run_scenario` inherited
# `fit_metric`'s `tune=1000`, so one node fitted over one window returned a
# posterior drawn after a different warm-up depending on which URL the reader
# called — and nothing in either payload said which. `draws` had diverged the
# same way in the other direction (`fit_metric` 1000, everything reachable
# through the API 500). Harmless while `/analyze` was the only NUTS path and
# the analyses ran ADVI, which never reads `tune`; roadmap S2's Option C put
# every route on NUTS and made it two unequal posteriors for one question.
#
# Enumerated rather than pinned to today's call sites, because the next
# orchestrator or route is the one that matters — and because "edit the three
# numbers to match" recreates the defect the moment someone edits one.

#: Parameters whose value is a sampler budget, wherever they appear.
_SAMPLER_BUDGETS = frozenset({"draws", "tune", "chains", "vi_iterations"})

#: Where the budget is allowed to be written as a literal: the definitions.
_BUDGET_CONSTANTS = ("NUTS_DRAWS", "NUTS_TUNE", "NUTS_CHAINS", "ADVI_ITERATIONS")


def _sampler_budget_literals(path: Path):
    """Every place `path` writes a sampler budget as a bare number.

    Three syntactic forms, because the split appeared as all three: a keyword
    argument at a call site (`fit_metric(..., tune=500)`), a function parameter
    default (`def run_rca(..., draws: int = 500)`), and a FastAPI route default
    (`tune: int = Query(default=500, ...)`).
    """
    found = []
    tree = ast.parse(path.read_text())

    def note(lineno, what, value):
        found.append(f"{path.name}:{lineno} {what} = {value!r}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in _SAMPLER_BUDGETS and isinstance(kw.value, ast.Constant):
                    note(kw.value.lineno, f"{ast.unparse(node.func)}({kw.arg}=)", kw.value.value)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
            for arg, default in pairs:
                if arg.arg not in _SAMPLER_BUDGETS:
                    continue
                if isinstance(default, ast.Constant):
                    note(default.lineno, f"def {node.name}({arg.arg}=)", default.value)
                # `x: int = Query(default=500)` hides the literal one level in.
                elif isinstance(default, ast.Call):
                    for kw in default.keywords:
                        if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                            note(
                                kw.value.lineno,
                                f"def {node.name}({arg.arg}=Query(default=))",
                                kw.value.value,
                            )
    return found


def test_the_sampler_budget_is_defined_exactly_once():
    """`engine/model.py` is where the four numbers live, and it says why."""
    source = (PACKAGE / "engine" / "model.py").read_text()
    tree = ast.parse(source)
    defined = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name) and t.id in _BUDGET_CONSTANTS
    }
    missing = [c for c in _BUDGET_CONSTANTS if c not in defined]
    assert not missing, (
        f"{missing} must be module-level constants in breakdown/engine/model.py — "
        "the one place the sampler budget is written (roadmap C27)."
    )
    assert "C27" in source, (
        "the constants block should say why it exists; a number with no stated "
        "reason is the thing that drifted."
    )


def test_no_sampler_budget_is_written_as_a_literal():
    """A budget written at a call site is a policy that only applies there.

    Every orchestrator default, every fit call and every route default reads
    `NUTS_DRAWS` / `NUTS_TUNE` / `NUTS_CHAINS` / `ADVI_ITERATIONS`. Writing the
    number instead means a future reader can change one and miss the others,
    which is exactly how `/analyze` came to warm up for half as long as the
    analyses did — the right policy in one file, not propagated to its
    neighbour.

    `fit_metric`'s own signature is where the constants are *applied*, so its
    defaults are Names and pass; the constants' own assignments are not
    parameters and never reach this scan.
    """
    scanned = sorted((PACKAGE / "engine").glob("*.py")) + sorted((PACKAGE / "api").glob("*.py"))
    scanned += [PACKAGE / "mcp" / "server.py", PACKAGE / "cli.py", PACKAGE / "doctor.py"]
    offenders = [hit for path in scanned if path.exists() for hit in _sampler_budget_literals(path)]
    assert not offenders, (
        "a sampler budget is written as a literal instead of reading the engine's "
        f"constants: {offenders}. Import NUTS_DRAWS / NUTS_TUNE / NUTS_CHAINS / "
        "ADVI_ITERATIONS from breakdown.engine.model — one budget per parameter, "
        "so the route a caller arrives through cannot change the posterior they "
        "get (roadmap C27)."
    )


# --- Every fit the engine runs for a caller is seeded (roadmap S22) -----------
#
# The budget block above, one property over, and the same meta-defect: two
# orchestrators wrote `random_seed=0` at their own fit call sites and
# `POST /analyze/{name}` passed nothing, so the manual-fit route returned a
# different posterior — and a different PSIS k-hat — from two identical
# requests. `fit_metric`'s own default stays None, because a library caller
# may legitimately want an unseeded fit; what may not vary is the answer the
# *server* gives to the same question.
#
# Enumerated, not pinned: the fourth call site is the one that matters.


def _unseeded_fit_calls(path: Path):
    """Every `fit_metric(...)` in `path` that does not pass `FIT_RANDOM_SEED`.

    Two spellings, because the package uses both: a direct call, and
    `asyncio.to_thread(fit_metric, ...)`, which hides the callee in the first
    positional argument and would sail past a check that only reads `func`.
    """
    found = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Call):
            continue
        func = ast.unparse(node.func)
        if func == "fit_metric":
            call = node
        elif func.endswith("to_thread") and node.args and ast.unparse(node.args[0]) == "fit_metric":
            call = node
        else:
            continue
        seeds = [kw for kw in call.keywords if kw.arg == "random_seed"]
        if len(seeds) != 1 or ast.unparse(seeds[0].value) != "FIT_RANDOM_SEED":
            found.append(f"{path.name}:{call.lineno}")
    return found


def test_every_fit_the_engine_runs_for_a_caller_is_seeded():
    """A fit is a pure function of (DAG, data, target) — including its seed.

    `POST /analyze/{name}` passed no `random_seed` while `run_rca` and
    `run_scenario` both did, so the same request twice fitted the same node
    over the same window twice and returned two different posteriors. With
    `?inference_method=advi` it also returned two different PSIS k-hats about
    them (1.23 then 1.91 on the demo tree's `customer_churn_rate`) — a
    diagnostic that answers differently about the same fit, which is the one
    property a diagnostic may not have (roadmap S22).
    """
    scanned = sorted((PACKAGE / "engine").glob("*.py")) + sorted((PACKAGE / "api").glob("*.py"))
    scanned += [PACKAGE / "mcp" / "server.py", PACKAGE / "cli.py", PACKAGE / "doctor.py"]
    offenders = [hit for path in scanned if path.exists() for hit in _unseeded_fit_calls(path)]
    assert not offenders, (
        f"these fits do not pass FIT_RANDOM_SEED: {offenders}. Import it from "
        "breakdown.engine.model and pass `random_seed=FIT_RANDOM_SEED` — a route "
        "or orchestrator that fits on a caller's behalf must return the same "
        "posterior, and the same k-hat, for the same request (roadmap S22)."
    )


def test_no_surface_prints_a_bare_khat():
    """The fifth rule, on the number roadmap S22 gave an error to.

    k̂ has a Monte-Carlo standard error of about 0.15 near the 0.5 bar, which
    is most of the width of the band it is being read against — so a surface
    that prints `1.36` where another prints `1.36 ± 0.22` is telling two
    readers different things about the same fit, and the shorter one reads as
    exact. `khatFigure(node)` is the single place that decides; `fmtKhat` stays
    for the cases that genuinely have no node in hand (a raw number).

    Structural rather than pinned to today's five call sites, because the sixth
    surface is the one that will get this wrong.
    """
    source = (PACKAGE / "static" / "app.js").read_text() + (
        PACKAGE / "static" / "disclosures.js"
    ).read_text()
    offenders = re.findall(r"fmtKhat\(\s*\w+\.khat\b\s*\)", source)
    assert not offenders, (
        f"app.js prints a bare k̂ at {len(offenders)} site(s): {sorted(set(offenders))}. "
        "Use khatFigure(node), which carries `± khat_se` when the engine could "
        "estimate it — a k̂ shown without its own error is read as exact "
        "(roadmap S22)."
    )
    assert "function khatFigure(" in source, (
        "khatFigure is gone from app.js. If the k̂ rendering was restructured, "
        "point this test at whatever replaced it — do not let it silently stop "
        "checking that the error travels with the estimate."
    )


#: Every place a sampler budget is printed at a human, and the constant it must
#: equal. `app.js` cannot import from Python (no build step, deliberately) and
#: `docs/api-reference.md` documents the defaults as a table, so both are
#: hand-copied — a fourth and a fifth spelling unless something checks them.
_RENDERED_BUDGETS = [
    # The Draws input's initial value.
    (r'id="an-draws"[^>]*value="(\d+)"', "NUTS_DRAWS"),
    # The control note and the NUTS hint, both of which name the warm-up the
    # reader is about to pay for. This is the pair that was false.
    (r"([\d,]+)\s+(?:discarded\s+)?tuning steps", "NUTS_TUNE"),
    (r"(\d+)\s+chains", "NUTS_CHAINS"),
    (r"([\d,]+)\s+optimization steps", "ADVI_ITERATIONS"),
    (r"fixed\s+([\d,]+)\s+steps", "ADVI_ITERATIONS"),
]

#: The `POST /analyze/{name}` parameter table in docs/api-reference.md.
_DOC_BUDGETS = [
    (r"^\|\s*`draws`\s*\|\s*`(\d+)`", "NUTS_DRAWS"),
    (r"^\|\s*`tune`\s*\|\s*`(\d+)`", "NUTS_TUNE"),
    (r"^\|\s*`chains`\s*\|\s*`(\d+)`", "NUTS_CHAINS"),
]


def test_every_rendered_sampler_budget_matches_the_engine():
    """The fifth rule, with the cheap half of it enumerated.

    `app.js` told every reader of the Metric tab that their NUTS fit ran
    "after 1,000 discarded tuning steps". The route ran 500. Nothing was wrong
    with the payload; the sentence describing it was wrong, which to the reader
    is the same thing. There is no JS test runner here, so the numbers are read
    out of the file and compared against the engine's own.
    """
    source = (PACKAGE / "static" / "app.js").read_text()
    for pattern, const in _RENDERED_BUDGETS:
        expected = getattr(model_mod, const)
        found = re.findall(pattern, source)
        assert found, (
            f"app.js no longer renders a {const} anywhere (pattern {pattern!r}). "
            "If the wording changed, update the pattern; if the number stopped "
            "being shown, delete the row — do not let it silently stop checking."
        )
        wrong = [v for v in found if int(v.replace(",", "")) != expected]
        assert not wrong, (
            f"app.js shows {wrong} where the engine's {const} is {expected}. "
            "The UI is describing a fit the engine did not run (roadmap C27)."
        )


def test_the_documented_route_defaults_are_the_route_defaults():
    """`docs/api-reference.md` is what a caller reads instead of the source."""
    doc = (PACKAGE.parent / "docs" / "api-reference.md").read_text()
    for pattern, const in _DOC_BUDGETS:
        expected = getattr(model_mod, const)
        found = re.findall(pattern, doc, flags=re.MULTILINE)
        assert found, f"docs/api-reference.md no longer documents a `{const}` default row"
        wrong = [v for v in found if int(v) != expected]
        assert not wrong, (
            f"docs/api-reference.md documents {wrong} for {const}, which is {expected}. "
            "A caller who reads the docs instead of the source gets a different "
            "posterior than the one they planned for (roadmap C27)."
        )


# --- Every physical bound is two-sided, and none is read off history (C26) ----
#
# `simulate.py` decided, carefully and in a comment, that a metric which has
# never been negative should not be simulated negative — and that check had one
# side. A `member_activity_rate` simulated to 1.025 (102.5% of members active)
# came back with `extrapolation: "above the historical max 0.3162"` and no
# `non_physical` at all, in the same response that correctly called three
# negative nodes impossible. Two impossibilities, one named as such.
#
# The fix is a declared bound (`share: true`) rather than an inferred one, and
# the enumeration below is what stops the next bound from arriving with one
# end. Note what it does *not* allow: a ceiling inferred from `hist_max`. "Never
# observed above X" is a fact about the sample and belongs to `extrapolation`;
# only a claim the tree makes about the quantity can call a value impossible.


def test_every_structural_bound_is_two_sided():
    """A bound that is a fact about the metric bounds it at both ends."""
    assert simulate_mod._STRUCTURAL_BOUNDS, "the bounds table cannot be empty"
    for declaration, bounds in simulate_mod._STRUCTURAL_BOUNDS.items():
        lo, hi = bounds
        assert lo is not None and hi is not None, (
            f"`{declaration}` declares only one end of its range ({bounds}). A "
            "structural bound is a fact about what the metric is, and a fact "
            "with one side is how C26 happened: the floor fired and the "
            "ceiling did not exist. If the declaration genuinely bounds one "
            "end only, it is not structural — say why in this table and here."
        )
        assert lo < hi


def test_the_impossible_verdict_is_reached_from_exactly_one_place():
    """Every `non_physical` warning in `simulate.py` comes out of one function.

    Enumerated rather than pinned, because the defect was not a wrong check —
    it was a *second* place that would have needed the same policy and never
    got it. A new bound added anywhere else in the module fails here.
    """
    tree = ast.parse((PACKAGE / "engine" / "simulate.py").read_text())
    emitters = set()
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "kind"
                    and isinstance(value, ast.Constant)
                    and value.value == "non_physical"
                ):
                    emitters.add(func.name)
    assert emitters == {"_non_physical_warning"}, (
        f"`non_physical` is decided in {sorted(emitters)}. It must be decided in "
        "`_non_physical_warning` alone — the one place that knows a declared "
        "bound applies on both sides and in both modes, and that the historical "
        "floor is an inference with no ceiling counterpart (roadmap C26)."
    )


def test_a_declared_bound_is_flagged_on_both_sides_and_never_from_history():
    """The bound holds against a history that would happily permit the value."""

    class _Defn:
        share = True

    # A history that contains 5 and -3 does not license a share of 1.5: the
    # declaration is about the quantity, the history is about the sample.
    permissive = {"hist_min": -3.0, "hist_max": 5.0, "hist_mean": 1.0, "hist_std": 2.0}
    above = simulate_mod._non_physical_warning("r", _Defn(), 1.5, permissive)
    below = simulate_mod._non_physical_warning("r", _Defn(), -0.5, permissive)
    inside = simulate_mod._non_physical_warning("r", _Defn(), 0.5, permissive)

    assert above is not None and above["kind"] == "non_physical"
    assert below is not None and below["kind"] == "non_physical"
    assert inside is None
    # Both sentences cite the declaration, not the sample.
    for w in (above, below):
        assert "share" in w["detail"] and "historical" not in w["detail"]

    # ...and with no history at all (cold start), which is the other half of
    # "structural": a declared bound does not need data to hold.
    assert simulate_mod._non_physical_warning("r", _Defn(), 1.5, None) is not None


def test_an_undeclared_node_gets_no_structural_bound():
    """The ceiling is opt-in, because nothing weaker implies it.

    C26 was filed believing a `denominator` made a rate a share. The repo's own
    trees say otherwise — `average_order_value` declares `denominator:
    order_count` and is ~$182 an order — so a ceiling read off `denominator`
    would print "$182 per order is impossible" on the bundled example tree,
    where a +10% lever on it simulates to 203.5. This pins the
    absence.
    """
    tree = Parser("""
metrics:
  - name: order_count
    source: s.m.order_count
  - name: average_order_value
    source: s.m.aov
    kind: rate
    denominator: order_count
""").dag
    aov = tree.nodes["average_order_value"]["definition"]
    assert aov.denominator == "order_count"
    assert aov.share is None
    assert simulate_mod._structural_bounds(aov) == (None, None, None)
    assert simulate_mod._non_physical_warning("average_order_value", aov, 18.0, None) is None


def test_every_surface_that_publishes_a_bound_verdict_publishes_both():
    """The fifth rule's half of C26: the reader must be able to tell them apart.

    `non_physical` reached only the panel-wide Warnings list, so the card and
    the table row for the impossible node itself rendered clean — and the MCP
    payload published `extrapolation` per node with no companion, leaving an
    agent unable to tell "far outside what we have seen" from "cannot exist".
    Both surfaces are read here, since neither has a test runner of its own.
    """
    for path, minimum in (
        (PACKAGE / "mcp" / "shaping.py", 1),
        (PACKAGE / "static" / "app.js", 2),  # the outcome card and the table row
    ):
        # Comments stripped (grill L6): a substring count over raw source is
        # satisfiable by the very comment explaining the count.
        source = _js_code(path) if path.suffix == ".js" else path.read_text()
        assert source.count("non_physical") >= minimum, (
            f"{path.name} renders or publishes fewer than {minimum} references to "
            "`non_physical`. Every surface that carries the per-node "
            "`extrapolation` verdict carries the stronger one beside it "
            "(roadmap C26); if the wording changed, update this test, and if "
            "the flag stopped being shown, put it back."
        )


def test_a_declared_share_is_checked_against_its_own_data_at_load(caplog, tmp_path, monkeypatch):
    """The declaration that makes a value impossible is itself checkable.

    `share: true` is an author's claim, and it is the claim that turns a
    what-if into a refusal — so a wrong one is a confident refusal of a
    perfectly possible scenario, which is the failure this project exists to
    avoid. Nothing else can catch it: the parser sees no data and the what-if
    engine sees one window. This runs the check the other way, once, at load.
    """
    tree = tmp_path / "share.yml"
    tree.write_text("""
provider: {type: mock}
metrics:
  - name: sessions
    source: t.metrics.sessions
  - name: honest_rate
    source: t.metrics.honest_rate
    kind: rate
    denominator: sessions
    share: true
  - name: retention
    source: t.metrics.retention
    kind: rate
    denominator: sessions
    share: true
""")
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-03-31")
    from fastapi.testclient import TestClient

    from breakdown.grains import GrainedData

    real_series = GrainedData.series

    def series(self, name):
        out = real_series(self, name)
        if name == "retention":
            # Net dollar retention's shape: a "rate" that is supposed to pass
            # 1. The mis-declaration is the interesting case, not the fix.
            out = out.copy()
            out[name] = out[name].to_numpy(dtype=float) + 1.0
        return out

    monkeypatch.setattr(GrainedData, "series", series)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="breakdown.api.main"):
        with TestClient(app) as client:
            assert client.get("/health").json()["status"] == "ok"
    said = [r.getMessage() for r in caplog.records if "`share: true`" in r.getMessage()]
    assert any("retention" in m for m in said), (
        "a `share: true` node whose own history leaves [0, 1] was accepted in "
        f"silence; warnings were {said}. The engine is about to call a "
        "simulated 1.2 impossible for a metric it has already recorded at 1.2."
    )
    assert not any("honest_rate" in m for m in said), (
        "a share whose data agrees with its declaration was warned about"
    )


def test_every_place_that_explains_a_suspect_fit_knows_the_model_can_be_the_cause():
    """Roadmap S3 added a *third* way to reach `fit_quality: "suspect"`, and the
    two surfaces that explain the verdict to a reader had to learn about it.

    Before S3, `suspect` meant the sampler struggled (NUTS: R̂ / divergences /
    ESS) or the approximation was far from the posterior (ADVI: the ELBO, or
    k̂). Both explanations in `app.js` enumerated exactly those causes, in
    prose, unconditionally. A `severe` posterior predictive check now also sets
    `suspect` — and on a NUTS fit it is the *only* thing that can — so an
    unconditional enumeration names a cause that did not happen, to a reader
    with no payload to check it against. That is the fifth rule's failure
    (a correct payload rendered dishonestly), not a cosmetic one.

    It is also exactly the meta-defect the four rules were written about: the
    fix landed in `renderPosterior`'s explanation first and the export's
    `caveatBlock` — the neighbouring surface, same policy, same file — kept the
    old sentence for one working session. Roadmap C37 then collapsed the five
    drifting copies into `fitQualityNote` (disclosures.js), so the enumeration
    now expects exactly two survivors: the shared vocabulary, and the Metric
    tab's richer diagnostics-side version (which can also see k̂ figures). A
    *third* prose explanation appearing anywhere is a copy escaping the
    vocabulary and fails here.
    """
    src = (PACKAGE / "static" / "app.js").read_text() + (
        PACKAGE / "static" / "disclosures.js"
    ).read_text()

    # Every passage that explains the verdict names the sampler-side causes in
    # prose. Find them by that enumeration rather than by a marker comment,
    # which a new author would not know to copy.
    sites = [m.start() for m in re.finditer(r"divergence[s]? (?:or|count)", src)]
    assert len(sites) == 2, (
        f"expected exactly two suspect explanations — fitQualityNote and the "
        f"metric card's diagnostics version; found {len(sites)}. More means a "
        "copy has escaped the shared vocabulary (roadmap C37); fewer means an "
        "explanation stopped naming its causes."
    )

    for start in sites:
        # The branch this sentence sits in, generously bounded: the sentence
        # plus the ~1.5k characters around it, which covers the conditional
        # that selects it in both current sites.
        window = src[max(0, start - 1500) : start + 500]
        assert "ppc_status" in window, (
            'an explanation of `fit_quality: "suspect"` at offset '
            f"{start} enumerates the sampler-side causes without branching on "
            "`ppc_status`. Since roadmap S3 a severe posterior predictive check "
            "also sets `suspect` — and on a NUTS fit it is the only thing that "
            "can — so this passage will tell a reader the sampler failed when "
            "the model did. Add the `severe` branch (see `caveatBlock` and "
            "`renderPosterior` in app.js)."
        )


def _numeric_runs(payload, path="") -> list:
    """Every list of plain numbers reachable in `payload`, as `(path, length)`.

    A list of dicts is not one of these: `time_series`, `ranked_causes` and
    `contributions` are all per-period or per-node *records*, which is a
    different thing from a raw series.
    """
    found = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            found += _numeric_runs(v, f"{path}.{k}")
    elif isinstance(payload, list):
        if payload and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in payload
        ):
            found.append((path, len(payload)))
        else:
            for i, v in enumerate(payload):
                found += _numeric_runs(v, f"{path}[{i}]")
    return found


def test_no_per_node_payload_carries_a_series(tmp_path, monkeypatch):
    """Roadmap S10's placement decision, as a property rather than a location.

    S3's `ppc` block is copied onto *every* RCA node and shaped into every MCP
    payload, and S10 needed a per-period array of six series. Putting it there
    would have been the obvious move and would have cost ~88 kB per node — on
    a 106-metric analysis, nine megabytes of decomposition handed to an agent
    that cannot read a chart. So the band lives on `FitResult` and reaches one
    route, and this is the property that makes that structural: nothing on a
    per-node payload is a series.

    Enumerating rather than pinning `ppc_band` by name, because the next
    author to want a per-period array on a node will not call it that. A
    handful of numbers (a few quantile levels, a pair of bounds) is fine and is
    what the cap allows; a window's worth is not.
    """
    from fastapi.testclient import TestClient

    from breakdown.mcp.shaping import compact_rca

    # 32 is comfortably above every legitimate fixed-length array on a node
    # (five quantile levels, two interval bounds) and far below any window a
    # tree is worth fitting on — `MIN_FIT_PERIODS` alone is larger.
    cap = 32

    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")
    with TestClient(app) as client:
        assert client.post("/analyze/order_count?draws=150").status_code == 200

        payloads = {
            "GET /metrics/{name}.diagnostics": client.get("/metrics/order_count").json()[
                "diagnostics"
            ],
        }
        rca = client.post(
            "/rca/revenue", params={"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"}
        ).json()
        for name, node in rca["nodes"].items():
            payloads[f"POST /rca/revenue nodes.{name}"] = node
        for name, node in (compact_rca(rca).get("nodes") or {}).items():
            payloads[f"compact_rca nodes.{name}"] = node

        offenders = [
            (where, path, n)
            for where, payload in payloads.items()
            for path, n in _numeric_runs(payload)
            if n > cap
        ]

    assert not offenders, (
        f"{offenders} — a per-node payload carries a series of numbers. These "
        "payloads are emitted once per node (an RCA over the reference tree has "
        "106 of them) and shaped into an agent's context verbatim, so anything "
        "that scales with the fitted window belongs on the fit and behind its "
        "own route, the way roadmap S10's `ppc_band` does."
    )
