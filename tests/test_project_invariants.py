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
"""

import ast
import dataclasses
import inspect
import json
import logging
import math
from pathlib import Path

import pandas as pd
import pytest

from breakdown import data_fetch
from breakdown.api import trees as trees_mod
from breakdown.data_fetch import _align_to_spine
from breakdown.engine import model as model_mod
from breakdown.engine import simulate as simulate_mod

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


def test_every_fetcher_goes_through_the_shared_alignment_contract():
    """Rule 1, structurally: C2's invariant is that no provider has its own copy.

    `cloud` and `local` used to floor their labels and return raw rows, which is
    how a two-day partial week became a full row at ~2/7 volume. A new provider
    that hand-rolls alignment is the same defect returning.
    """
    offenders = []
    for name, obj in vars(data_fetch).items():
        if not (inspect.isclass(obj) and name.endswith("DataFetcher")):
            continue
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
        offenders.append(name)
    assert not offenders, (
        f"{offenders} implement fetch_metric without calling `_align_to_spine`. "
        "Every provider shares one date-alignment contract (roadmap C2)."
    )


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
    assert not unbounded, (
        f"{unbounded} default to an unbounded dict on TreeState. Every cache "
        "here grows with distinct user-chosen windows until the process is "
        "OOM-killed (roadmap C8, 2.18). Use BoundedCache."
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


# --- Rule 3: no engine result reaches an encoder unsanitized ------------------


def _strict(payload) -> None:
    json.dumps(payload, allow_nan=False)


def test_a_degenerate_tree_still_encodes_strictly(tmp_path, monkeypatch):
    """Rule 3, end to end through the surface that actually broke.

    One zero-denominator period used to reach Starlette's `allow_nan=False`
    encoder as an unhandled 500, and `round_floats` turned the same NaN into
    `null` for an agent (C17). Both routes are exercised on a tree carrying a
    zero denominator *and* a zero-variance parent.
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
    )
    # monkeypatch, not os.environ: the app reads these at lifespan, so leaving
    # them set points every later test in the session at a tmp_path tree that
    # no longer exists. (Found the hard way — it errored two README tests that
    # pass in isolation, which is the signature of exactly this mistake.)
    monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
    monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
    monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")

    from fastapi.testclient import TestClient

    from breakdown.api.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        state = app.state.trees["degenerate"]
        frame = state.data.frames["day"]
        # the structural zero `_align_to_spine` manufactures for a flow
        # denominator, and a parent held flat (the C4 production shape)
        frame.loc[frame.index[-3], "order_count"] = 0.0
        frame["promo"] = 0.0

        for path, params in (
            ("/rca/aov", {"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"}),
            ("/rca/revenue", {"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"}),
        ):
            r = client.post(path, params=params)
            assert r.status_code != 500, (
                f"POST {path} returned 500 on a degenerate but ordinary tree — a "
                "non-finite value reached the encoder (roadmap C17)."
            )
            _strict(r.json())

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
