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
    n = rca_mod._N_BOOT
    value, censored = rca_mod.prob_same_direction(np.linspace(1.0, 2.0, n))
    assert censored, "every replicate on one side is a saturated count, not certainty"
    assert value == pytest.approx(1.0 - 1.0 / n)

    # Not saturated: one replicate the other way is a representable proportion
    # and is published exactly, uncensored.
    mixed = np.linspace(1.0, 2.0, n)
    mixed[0] = -1.0
    value, censored = rca_mod.prob_same_direction(mixed)
    assert not censored and value == pytest.approx(1.0 - 1.0 / n)


def test_an_exact_sample_is_not_censored():
    """The one case where 1.0 is honest: no spread at all.

    A `simulate` propagation straight through an identity from a pinned
    intervention is exact arithmetic, not an estimate of a proportion. Clamping
    it would understate a sign that really is known.
    """
    value, censored = rca_mod.prob_same_direction(np.full(64, 3.0))
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
        advi_draws=300,
    )
    ceiling = 1.0 - 1.0 / rca_mod._N_BOOT
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
            f"{rca_mod._N_BOOT} replicates cannot take that value."
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
