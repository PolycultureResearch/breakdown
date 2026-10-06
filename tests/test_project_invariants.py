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

**"Check the property" turned out to need saying twice.** The first edition of
several tests here enumerated the code and then checked the *spelling* of a
guard: rule 4 was `"_MAX_" in text` and passed for `model.py` on a comment;
the render-site test was a substring match that `status` and `gap` could
never fail; rule 2 looked only at fields whose default was a dict; rule 3
drove three routes and left the rest (grills 2026-08-29 M6 and 2026-10-05
M11). The
standard each test is now held to: it reads the AST or the running app, never
a substring of the source; it enumerates what it checks from the code, so a
new route, field, tool, cache or enumeration is asked the question the day it
is added; and every exception is a named entry with a reason and, where it
can drift, a count. Each was shown to fail on the defect it exists for by
introducing that defect in a scratch copy.

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


# --- Violations the 2026-10-05 grill found, being fixed on sibling branches ---
#
# The third review (grill 2026-10-05, M11) found that most tests in this file
# checked the *spelling* of a guard rather than the property — rule 4 passed
# for `model.py` on a comment that named another module's constant. They were
# rewritten to check the property, and the rewritten tests correctly flag
# defects that review had already catalogued and that are being fixed in
# parallel. Each such violation is listed here, by the key the test that finds
# it reports, against the finding that owns it.
#
# This is not an exemption list. `_settle` below is strict in both directions:
# a violation that is not listed fails its test, and an entry whose violation
# is gone fails it too — so the fix for H7 cannot merge without deleting H7's
# lines, and this dict cannot quietly outlive the defects it names. When it is
# empty, delete it and `_settle`'s second half with it.
_PENDING_GRILL_1005: typing.Dict[str, str] = {
    # H7: the three analysis tools hand the engine to `asyncio.to_thread`
    # directly, so an orphaned run on a tree holds no guard an MCP caller
    # would meet (mcp/server.py:459, 528, 582).
    "mcp-guard:run_rca": "H7 — MCP run_rca calls the engine outside `_guarded`",
    "mcp-guard:slice_metric": "H7 — MCP slice_metric calls the engine outside `_guarded`",
    "mcp-guard:run_whatif": "H7 — MCP run_whatif calls the engine outside `_guarded`",
    # M6: what HTTP answers with a 422 and a message, MCP answers with
    # "Error executing tool" and nothing else.
    "mcp-refusals:RuntimeError": "M6 — HTTP maps RuntimeError to 422 (C38); `_REFUSALS` omits it",
    # H6: the third door on `sql`/`bind`, and the load error on every
    # degraded response.
    "redaction-definition:GET /metrics/{name}": (
        "H6 — returns `metric.model_dump()` with `sql`/`bind` that `/dag` redacts"
    ),
    "redaction-load-error:GET /trees": "H6 — the index card carries the raw `load_error`",
    "redaction-load-error:POST /trees/{tree_id}/load": "H6 — returns the same card",
    "redaction-load-error:GET /meta": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:GET /dag": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:GET /series": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:GET /metrics/{name}": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:GET /metrics/{name}/query": (
        "H6 — 503 detail carries the raw `load_error` (readiness is checked before the token)"
    ),
    "redaction-load-error:GET /metrics/{name}/ppc": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:POST /analyze/{name}": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:GET /shapley/{name}": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:POST /rca/{name}": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:POST /rca/{name}/slices": "H6 — 503 detail carries the raw `load_error`",
    "redaction-load-error:POST /simulate": "H6 — 503 detail carries the raw `load_error`",
    # M8: emitted on `/meta`, read by no surface of the UI.
    "render-site:/meta.sparse_fills": "M8 — declared zero-fills have no reader in the UI",
    "render-site:/meta.short_series": "M8 — no reader in the UI",
    "render-site:/meta.data_from": "M8 — no reader in the UI",
    # M11's catalogue, and unowned: the frontend package adds readers for the
    # three above only. The row prints `estimate` and words the multiplier,
    # but the figure itself reaches an agent and not a browser.
    "render-site:rca.intervention.window_delta": (
        "M8/M11 — the indicator's window delta is emitted per intervention and never rendered"
    ),
    # H3: `_align_to_spine` leaves the empty-result disclosure to "the
    # provider that knows the result was empty", and this one does not make it.
    "empty-result:DbtDataFetcher": (
        "H3 — `fetch_metric` zero-fills an empty result without a word (dbt_provider.py:412-445)"
    ),
}


def _settle(scope: str, violations: typing.Mapping[str, str], remedy: str) -> None:
    """Fail on any violation in `scope` that is not pending, and on any pending
    entry in `scope` that is no longer a violation.

    `violations` maps an item's key (a route, a tool, a field) to what is wrong
    with it. Keys are namespaced `scope:key` in `_PENDING_GRILL_1005`.
    """
    found = {f"{scope}:{key}": why for key, why in violations.items()}
    pending = {k for k in _PENDING_GRILL_1005 if k.startswith(f"{scope}:")}
    new = {k: why for k, why in found.items() if k not in pending}
    assert not new, f"{remedy}\n" + "\n".join(f"  {k} — {why}" for k, why in sorted(new.items()))
    fixed = sorted(pending - set(found))
    assert not fixed, (
        f"{fixed} are listed in `_PENDING_GRILL_1005` and no longer violate "
        "anything: the fix has landed. Delete those entries — the list exists "
        "to be emptied, and an entry that outlives its defect is an exemption "
        "nobody decided to grant."
    )


def _functions(tree: ast.AST):
    """Every outermost function in a module, with its qualified name.

    Nested defs and lambdas belong to the function that contains them: a guard
    in `run_scenario` covers the closure it defines, and a fill inside a local
    helper is the enclosing function's fill.
    """
    out = []

    def visit(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{prefix}{child.name}", child))
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _docstring_nodes(tree: ast.AST) -> set:
    """`id()` of every docstring constant, so a scan over string literals reads
    code and not prose. Comments never reach the AST at all, which is the
    point of scanning it: `"_MAX_" in text` was satisfied by one."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


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
        # ...and both at once, with two separate interior runs: the shape a
        # fill that covers one run and not the next leaves a NaN behind in.
        ("flow", [("2024-01-03", 5.0), ("2024-01-06", 6.0), ("2024-01-10", 7.0)], "both"),
        ("stock", [("2024-01-01", 5.0), ("2024-01-04", 6.0), ("2024-01-10", 7.0)], "two runs"),
        ("rate", [("2024-01-03", 5.0), ("2024-01-06", 6.0), ("2024-01-10", 7.0)], "both"),
    ],
)
def test_no_gap_is_filled_without_saying_so(kind, rows, gap, caplog):
    """Rule 1. A period the source did not return is either refused or named.

    The one thing that must never happen is the C18 shape: a value invented and
    nothing said. `stock` and `rate` refuse; `flow` fills and logs. Which of the
    two a given case does is a design decision recorded in `_align_to_spine`'s
    docstring — this test only insists that it is one of them.

    Compared **by position** (grill 2026-10-05 M11). The earlier form counted a
    fill as invented only when no NaN remained anywhere in the output, so a
    fill of the leading run that left one interior period undefined — or the
    reverse — invented values and passed. An invented value is a finite number
    on a date the source returned no row for, wherever else a NaN survives.
    """
    caplog.set_level(logging.WARNING, logger="breakdown.data_fetch")
    try:
        out = _spine_call(kind, rows)
    except (RuntimeError, ValueError):
        return  # refused: the strongest form of the rule
    returned = {pd.Timestamp(d) for d, _ in rows}
    invented = [
        str(pd.Timestamp(d).date())
        for d, v in zip(out["date"], out["m"])
        if pd.notna(v) and pd.Timestamp(d) not in returned
    ]
    if invented:
        said = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
        assert said, (
            f"{kind}/{gap}: {len(invented)} period(s) were invented ({invented}) and "
            "nothing was logged — this is the C18 defect in a new place. Either "
            "refuse, or log what was fabricated."
        )
        # The warning has to be about *this* fill: it names the metric and how
        # many periods, so a count that covers one run and not the other is
        # also a silent fill of the remainder.
        assert "'m'" in said, f"{kind}/{gap}: the fill warning does not name the metric: {said}"
        counts = [int(n) for n in re.findall(r"\b(\d+) (?:interior|leading|trailing)", said)]
        assert sum(counts) >= len(invented), (
            f"{kind}/{gap}: {len(invented)} period(s) were invented and the "
            f"warnings account for {counts}: {said}"
        )


def test_the_empty_source_fill_is_said_and_counted(caplog):
    """Rule 1 has no silent exception left (grill 2026-10-05 H3).

    A source returning *no rows at all* keeps the full zero-fill for flows: an
    all-quiet window is a legitimate flow series. Until H3 that fill was the
    one deliberately silent one, on the reasoning that the provider which knew
    the result was empty would say so — and three of four providers did not,
    so a window that missed the data loaded as zeros with `/health: ok`. It is
    now warned about here, where every provider passes, and the row count
    travels on the frame so the load can refuse a tree in which *no* metric
    returned anything.
    """
    caplog.set_level(logging.WARNING, logger="breakdown.data_fetch")
    empty = pd.DataFrame({"date": pd.to_datetime([]), "m": []})
    out = _align_to_spine(empty, "m", "day", "flow", "2024-01-01", "2024-01-05", "m")
    assert len(out) == 5 and (out["m"] == 0.0).all()
    assert out.attrs["source_rows"] == 0
    assert [r for r in caplog.records if "returned no rows" in r.getMessage()], (
        "a whole window was filled from an empty result and nothing was logged"
    )


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


def _calls_made_by(fn) -> set:
    """The names a function *calls*, read off its AST: `f(...)` as `f`,
    `self.inner.g(...)` as `self.inner.g`. A name in a comment or a docstring
    is not a call, which is the difference between this and `name in
    inspect.getsource(fn)` — the form these scans had, and one a comment
    satisfies (grill 2026-10-05 M11)."""
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError):  # pragma: no cover - a builtin or C method
        return set()
    return {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}


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
        called = _calls_made_by(fetch)
        if "_align_to_spine" in called:
            continue
        # The mock is the one exemption, and the reason is load-bearing: it
        # *generates onto* `period_spine`, so its output cannot have a gap, a
        # partial edge period or a stray label for alignment to fix. That is
        # also exactly why the suite never saw C2 — every provider that could
        # be misaligned was one the tests did not exercise. Assert the reason
        # rather than the name, so a mock that stopped generating on the spine
        # (and started needing alignment like everyone else) fails here.
        if name == "MockDataFetcher" and "period_spine" in called:
            continue
        # A wrapper that reaches the contract through its own helper:
        # `SnapshotFetcher` re-aligns a hit in `_realign_snapshot`, because a
        # snapshot outlives the code that wrote it and nothing fingerprints the
        # *engine's* alignment rules the way `definition_sha` fingerprints the
        # metric's.
        if "_realign_snapshot" in called:
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
        called = _calls_made_by(fetch)
        if called & {"_to_naive_dates", "_sliced_long"}:
            continue
        # The mock's exemption, with its reason asserted like fetch_metric's:
        # its sliced frame derives its dates from its own generated series
        # (which the class produces on `period_spine`), so there is no external
        # timestamp to coerce. If it ever starts parsing dates it did not
        # generate, this stops matching and it fails here like anyone else.
        if name == "MockDataFetcher" and any(
            "period_spine" in _calls_made_by(member)
            for member in vars(obj).values()
            if inspect.isfunction(member)
        ):
            continue
        # A read-through wrapper serves frames its inner provider (or its own
        # write path) already coerced; `store.read_sliced` hits are re-parsed
        # from parquet, which cannot re-attach a zone the writer dropped.
        if "self.inner.fetch_metric_sliced" in called:
            continue
        offenders.append(name)
    assert not offenders, (
        f"{offenders} implement fetch_metric_sliced without the shared date "
        "coercion (`_to_naive_dates` / `_sliced_long`). A tz-aware sliced frame "
        "becomes an all-zero panel downstream (roadmap C23)."
    )

    # And the shared reshape itself must hold the coercion, or every provider
    # routing through it passes the scan while the defect stands.
    assert "_to_naive_dates" in _calls_made_by(data_fetch._sliced_long)


#: The modules a fetched value passes through before the engine sees it.
_BOUNDARY_MODULES = (
    "data_fetch.py",
    "dbt_provider.py",
    "dbt_sql.py",
    "dbt_bridge.py",
    "dbt_manifest.py",
    "loading.py",
    "grains.py",
    "snapshots.py",
)

#: Calls that put a value where the source had none, however they are reached
#: (`s.fillna(0)`, `np.nan_to_num(x)`, `df.reindex(..., fill_value=0)`).
_FILL_CALLS = frozenset(
    {"fillna", "ffill", "bfill", "pad", "backfill", "interpolate", "nan_to_num", "combine_first"}
)
_FILL_KEYWORDS = frozenset({"fill_value"})

#: The same thing in SQL the package generates. `dbt_sql.py` builds statements
#: as text, so its fills are string literals and no call at all.
_SQL_FILL = re.compile(r"\b(COALESCE|IFNULL|NVL|ZEROIFNULL)\s*\(", re.IGNORECASE)

_ANNOUNCING_LOG_METHODS = frozenset({"warning", "error", "exception", "critical"})


def _announces(func: ast.AST) -> bool:
    """Whether a function can say what it did: it raises, or it logs at
    WARNING or above. `logger.info` is not an announcement — nobody reads INFO
    to find out a number was invented."""
    for node in ast.walk(func):
        if isinstance(node, ast.Raise):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _ANNOUNCING_LOG_METHODS
        ):
            return True
    return False


def _fill_sites(path: Path):
    """Every fill or coalesce in `path`, as `(function, what, lineno, announces)`.

    Read off the AST: a call by name, a `fill_value=` keyword, or a COALESCE
    in a string literal that is not a docstring. Comments are invisible to
    this, which is what the substring form of this check was not.
    """
    tree = ast.parse(path.read_text())
    docstrings = _docstring_nodes(tree)
    sites = []
    for qualname, func in _functions(tree):
        announces = _announces(func)
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None)
                )
                if name in _FILL_CALLS:
                    sites.append((qualname, name, node.lineno, announces))
                for kw in node.keywords:
                    if kw.arg in _FILL_KEYWORDS:
                        sites.append((qualname, kw.arg, node.lineno, announces))
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                for match in _SQL_FILL.finditer(node.value):
                    sites.append((qualname, match.group(1).upper(), node.lineno, announces))
    return sites


#: Every fill at the boundary, by function, with how many that function holds.
#: `(module, function, call) -> (count, standing, why)`, where the standing is
#:
#: - `"announced"` — the fill does supply a metric value, and the function
#:   says so. Checked two ways: the function can announce at all (it raises or
#:   logs a warning, read off the AST), and
#:   `test_no_gap_is_filled_without_saying_so` drives it and reads the log.
#: - `"no value"` — the fill supplies something that is not a metric value
#:   (a label, a marker, a mask), with the reason.
#:
#: A count, not a flag: a second fill in the same function is a new decision
#: and fails here. That matters more than it looks — most functions at this
#: boundary raise for *something*, so "sits in a function that can announce"
#: alone would wave through a `fillna(0)` added to any of them.
_BOUNDARY_FILLS = {
    # The shared contract's two fills: flow -> 0.0, stock -> the previous
    # value. Leading and interior runs are warned about by count and date; the
    # rate branch fills nothing. (`sparse: true` logs its declared fills at
    # INFO and records them on `/meta` — a declaration, not an invention.)
    ("data_fetch.py", "_align_to_spine", "fillna"): (1, "announced", "flow gaps -> 0.0, warned"),
    ("data_fetch.py", "_align_to_spine", "ffill"): (1, "announced", "stock gaps, warned"),
    # The roll-up marker column the SQL fold adds (`1` on the `__other__`
    # row): a NULL marker means "an ordinary slice", and the cast to int needs
    # a value there. It selects rows; it is never a number anyone reads.
    ("dbt_provider.py", "DbtDataFetcher.fetch_metric_sliced", "fillna"): (
        1,
        "no value",
        "the roll-up marker column, not a metric value",
    ),
    # A NULL *slice label* becomes the literal `__null__` so the pinned
    # `values:` filter and the engine's own label for a null slice agree, and
    # the folded-values count of an empty partition is 0 rather than NULL.
    # Labels and a count of labels; the measure is untouched.
    ("dbt_sql.py", "_rolled_up", "COALESCE"): (
        2,
        "no value",
        "a NULL dimension label and a count of folded labels, not a measure",
    ),
    # Entity flows: an entity present in one window and absent from the other
    # is reported under the `__null__` slice on the side it is missing from.
    ("dbt_sql.py", "build_entity_flow_query", "COALESCE"): (
        2,
        "no value",
        "a NULL slice label on the absent side of a FULL OUTER JOIN, not a measure",
    ),
    # A mask, compared and discarded: "which periods have a denominator of
    # exactly zero". Filling 1.0 keeps an undefined denominator *out* of that
    # set; nothing is written back to a series.
    ("loading.py", "report_undefined_periods", "fillna"): (
        1,
        "no value",
        "builds a boolean mask; the filled series is never stored",
    ),
}


def test_every_fill_at_the_provider_boundary_is_announced_or_named():
    """Rule 1, structurally, over the whole boundary and not one function.

    The check this replaces asked whether `fetch_metric`'s source contained the
    substring `_align_to_spine`, which a comment satisfies, and it looked at
    nothing else — a `fillna(0)` in `loading.py`, a `COALESCE(SUM(x), 0)` in
    the generated SQL or a `nan_to_num` in a new provider was invisible to it
    (grill 2026-10-05 M11). C15 and C18 were both a fill at this layer sitting
    beside a refusal at this layer.

    So: enumerate every fill or coalesce in every boundary module, by AST —
    calls, `fill_value=` keywords, and COALESCE in the SQL `dbt_sql.py` writes
    as text — and require each to be classified in `_BOUNDARY_FILLS`. A fill
    that supplies a metric value must be in a function that can say so; one
    that does not says what it fills instead. A new fill is in neither and
    fails here, which is the only moment anyone will ask which it is.
    """
    seen, announcing = {}, {}
    for module in _BOUNDARY_MODULES:
        for qualname, what, _lineno, announces in _fill_sites(PACKAGE / module):
            key = (module, qualname, what)
            seen.setdefault(key, []).append(_lineno)
            announcing[key] = announces
    assert len(seen) >= 4, f"the fill scan found only {sorted(seen)}; did it break?"

    unclassified = [
        f"breakdown/{module}:{lines} `{what}` in {qualname}()"
        for (module, qualname, what), lines in sorted(seen.items())
        if (module, qualname, what) not in _BOUNDARY_FILLS
    ]
    assert not unclassified, (
        f"{unclassified}: a fill or coalesce at the provider boundary that this "
        "test has never seen. If it puts a value where the source returned "
        "none, that is C15 and C18 exactly: refuse, or log what was invented "
        "(at WARNING), and add it to `_BOUNDARY_FILLS` as `announced`. If it "
        "fills something that is not a metric value, add it as `no value` and "
        "say what it fills."
    )
    drift = {
        key: (len(seen.get(key, [])), expected)
        for key, (expected, _standing, _why) in _BOUNDARY_FILLS.items()
        if len(seen.get(key, [])) != expected
    }
    assert not drift, (
        f"{drift}: (found, expected) fills in a classified function. More means "
        "a new fill joined an old one's classification without being looked "
        "at; fewer means the entry outlived its code — update or delete it."
    )
    mute = sorted(
        key
        for key, (_n, standing, _why) in _BOUNDARY_FILLS.items()
        if standing == "announced" and not announcing[key]
    )
    assert not mute, (
        f"{mute} are classified as announced fills, in functions that neither "
        "raise nor log a warning."
    )


def _empty_result_disclosed(cls) -> bool:
    """Whether `cls.fetch_metric`'s own path says so when the source returns
    nothing: a raise or a warning under a test of emptiness, in `fetch_metric`
    or in a method of the class it calls.

    "A test of emptiness" is an `if` reading `.empty` or `len(...)`, or a bare
    `if not <name>:`, or an `except` for pandas' `EmptyDataError` — the four
    ways the package asks the question today.
    """
    methods = {}
    for klass in cls.__mro__:
        if klass.__module__.startswith("breakdown."):
            for name, member in vars(klass).items():
                if inspect.isfunction(member):
                    methods.setdefault(name, member)

    def parsed(fn):
        import textwrap

        return ast.parse(textwrap.dedent(inspect.getsource(fn)))

    def asks_if_empty(test: ast.AST) -> bool:
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            if isinstance(test.operand, ast.Name):
                return True
        for node in ast.walk(test):
            if isinstance(node, ast.Attribute) and node.attr == "empty":
                return True
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "len":
                return True
        return False

    def discloses(tree: ast.AST) -> bool:
        for node in ast.walk(tree):
            guarded = None
            if isinstance(node, ast.If) and asks_if_empty(node.test):
                guarded = node.body
            elif isinstance(node, ast.ExceptHandler) and "EmptyDataError" in ast.unparse(
                node.type or ast.Constant(value="")
            ):
                guarded = node.body
            if guarded and any(_announces(stmt) for stmt in guarded):
                return True
        return False

    tree = parsed(methods["fetch_metric"])
    if discloses(tree):
        return True
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in methods
            and discloses(parsed(methods[node.func.attr]))
        ):
            return True
    return False


def test_every_fetcher_says_so_when_the_source_returns_nothing(caplog):
    """Rule 1 on the one fill `_align_to_spine` makes without a word.

    A flow whose source returned no rows at all is zero-filled silently — the
    test above pins that, and `_align_to_spine`'s docstring gives the reason:
    an all-quiet window is a legitimate flow series, and *"the provider that
    knows the result was empty says so itself"*. That sentence is a policy
    delegated to every provider, and one provider implemented it.
    `LocalDataFetcher` warns; `DbtDataFetcher` does not, so a `serve` whose
    default window misses the CSVs loads a tree of zeros behind a green
    `/health` (grill 2026-10-05 H3).

    Enumerated over the fetcher classes. A fetcher that hands a frame to
    `_align_to_spine` must disclose an empty one on the way: a warning or a
    raise under a test of emptiness, in `fetch_metric` or a method it calls.
    If `_align_to_spine` is ever changed to warn on the empty case itself,
    every provider is covered at once and this passes without looking further.
    """
    caplog.set_level(logging.WARNING, logger="breakdown.data_fetch")
    empty = pd.DataFrame({"date": pd.to_datetime([]), "m": []})
    _align_to_spine(empty, "m", "day", "flow", "2024-01-01", "2024-01-05", "m")
    if caplog.records:
        return  # the shared contract discloses it for everyone

    silent = {}
    for name, obj in _fetcher_classes():
        fetch = getattr(obj, "fetch_metric", None)
        if fetch is None:
            continue
        try:
            src = inspect.getsource(fetch)
        except (OSError, TypeError):  # pragma: no cover
            continue
        calls_spine = any(
            isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_align_to_spine"
            for node in ast.walk(ast.parse(__import__("textwrap").dedent(src)))
        )
        # Only a fetcher that feeds the shared fill can reach the silent one.
        # The mock generates onto the spine and the snapshot wrapper re-aligns
        # a frame its inner provider already disclosed.
        if calls_spine and not _empty_result_disclosed(obj):
            silent[name] = (
                "hands whatever the source returned to `_align_to_spine` and says "
                "nothing when that is no rows at all"
            )
    _settle(
        "empty-result",
        silent,
        "A provider zero-fills an empty result silently. `_align_to_spine` "
        "delegates that disclosure to the provider (see its docstring); a "
        "window that misses the data must not load as a healthy tree of zeros:",
    )


# --- Rule 2: every cache on TreeState is bounded ------------------------------


#: A type that can hold more than one thing, as it is spelled in an annotation.
_CONTAINER_ANNOTATION = re.compile(
    r"\b(dict|list|set|frozenset|tuple|deque|defaultdict|OrderedDict|Counter|"
    r"Mapping|MutableMapping|Sequence|MutableSequence|Iterable|BoundedCache|TraceView)\b",
    re.IGNORECASE,
)

#: Every field on `TreeState`, by what bounds it. A field in none of these
#: fails `test_every_field_on_tree_state_is_classified`, which is the only
#: moment anyone will ask whether a request can grow it.
_TREE_STATE_BOUNDED_BY_BYTES = {
    # A `TraceView` onto the process-wide `TraceStore` once the tree is
    # registered; the plain-dict default exists only for a `TreeState` built
    # outside the app. Asserted at runtime below, on a served tree.
    "traces",
    "slice_cache",
    "flow_cache",
}
_TREE_STATE_SIZED_BY_THE_TREE = {
    # The parsed YAML and its `tree:` block: as large as the file the operator
    # wrote, fixed at boot.
    "parser": "the parsed tree definition, fixed at boot",
    "meta": "the tree's own `tree:` block",
    # Keyed by *metric name*: one entry per metric, written by the discovery
    # task, never by a request.
    "earliest": "one entry per metric",
    # Roadmap 3.10's status record, replaced whole on each warm: one default
    # window per metric and one entry per planned fit, derived from the tree.
    "warm": "one window per metric and one entry per planned fit, replaced whole",
}
_TREE_STATE_NAMED = {
    # The loaded series: every metric over the window the operator passed to
    # `serve`. It scales with that window — which is the point of it — and is
    # written once, by `load_tree`; no request widens it.
    "data": "the loaded window, chosen by the operator and written once at load",
    # Not a container itself; what hangs off it is swept class by class in
    # `test_every_container_on_a_fetcher_is_classified`.
    "fetcher": "its instance state is classified by the fetcher sweep below",
}
_TREE_STATE_SCALARS = {
    "id",
    "path",
    "load_error",
    "load_error_kind",
    "loaded",
    "loading",
    "engine_guard",
    "lock",
    "earliest_task",
    "warm_task",
}


def test_every_field_on_tree_state_is_classified():
    """Rule 2, structurally: classify the dataclass, don't recognise a cache.

    The check this replaces looked at fields whose *default was a dict* and at
    nothing else (grill 2026-10-05 M11). A lazily filled `Optional[dict] =
    None`, a `list`, a `set`, a `deque` — every way to add a cache except the
    one `slice_cache` happened to use — was skipped, so the test protected
    against the last defect's spelling.

    So every field is classified, the way `FitResult`'s are below: bounded by
    bytes, sized by the tree the operator wrote, a named exception with its
    reason, or a scalar that cannot hold a collection at all. A new field is
    in none of the four and fails here. And each class is a claim, so each is
    checked: a byte-bounded field carries a byte budget, and a "scalar" may
    not be annotated as a container.
    """
    fields = {f.name: f for f in dataclasses.fields(trees_mod.TreeState)}
    classes = [
        _TREE_STATE_BOUNDED_BY_BYTES,
        set(_TREE_STATE_SIZED_BY_THE_TREE),
        set(_TREE_STATE_NAMED),
        _TREE_STATE_SCALARS,
    ]
    classified = set().union(*classes)
    assert sum(len(c) for c in classes) == len(classified), (
        "a TreeState field is classified twice; each has exactly one bound"
    )
    unclassified = sorted(set(fields) - classified)
    assert not unclassified, (
        f"{unclassified} are fields on TreeState this test has never seen. "
        "Per-tree state lives as long as the process and is reachable from "
        "every request: if a caller's choice of window, dimension or date can "
        "add to it, it must be a `BoundedCache` with a byte budget (rule 2 — "
        "`slice_cache` sat unbounded beside the bounded `traces` until 2.18). "
        "If it cannot grow, classify it here and say what bounds it."
    )
    stale = sorted(classified - set(fields))
    assert not stale, f"{stale} are classified here and no longer exist on TreeState"

    for name in sorted(_TREE_STATE_SCALARS):
        field = fields[name]
        annotation = field.type if isinstance(field.type, str) else str(field.type)
        assert not _CONTAINER_ANNOTATION.search(annotation), (
            f"TreeState.{name} is classified as a scalar and annotated "
            f"`{annotation}`, which can hold a collection. A cache that starts "
            "as `None` and is filled on first use is still a cache."
        )
        default = (
            field.default_factory()
            if field.default_factory is not dataclasses.MISSING
            else field.default
        )
        assert not isinstance(default, (dict, list, set, frozenset, tuple)), (
            f"TreeState.{name} is classified as a scalar and defaults to a {type(default).__name__}"
        )

    for name in sorted(_TREE_STATE_BOUNDED_BY_BYTES - {"traces"}):
        value = fields[name].default_factory()
        assert isinstance(value, trees_mod.BoundedCache), (
            f"TreeState.{name} is classified as byte-bounded and is a "
            f"{type(value).__name__}. Every cache here grows with distinct "
            "user-chosen windows until the process is OOM-killed (roadmap C8, "
            "2.18). Use BoundedCache."
        )
        # The type is not the bound (roadmap C32, grill H3): this test used to
        # accept any BoundedCache, and the cache was bounded by entry *count*
        # while the thing that grows is a frame's cardinality × window. Every
        # frame cache must carry a byte budget.
        assert value.max_bytes > 0, (
            f"TreeState.{name} is a BoundedCache with no byte budget. "
            "An entry scales with dimension cardinality times the loaded "
            "window (~154 MB measured at 5,000 slice values × 830 days), so "
            "a count bound alone is rule 2's defect with extra steps."
        )


def test_no_per_tree_state_is_attached_outside_the_dataclass():
    """The other way to add a cache without adding a field.

    A dataclass without `slots` accepts `tree.recent_windows = {}` from any
    module, and the classification above would never see it. Enumerate every
    assignment to an attribute of a tree in the service layer and require the
    attribute to be a declared field.
    """
    fields = {f.name for f in dataclasses.fields(trees_mod.TreeState)}
    receivers = {"tree", "tree_state", "t"}
    offenders = []
    for path in sorted((PACKAGE / "api").glob("*.py")) + sorted((PACKAGE / "mcp").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in receivers
                    and target.attr not in fields
                ):
                    offenders.append(f"{path.name}:{node.lineno} {ast.unparse(target)}")
    assert not offenders, (
        f"{offenders} set an attribute on a tree that `TreeState` does not "
        "declare. Per-tree state goes on the dataclass, where "
        "`test_every_field_on_tree_state_is_classified` asks what bounds it."
    )


#: Container-valued instance state on the provider classes — everything that
#: hangs off `TreeState.fetcher` — by what bounds it.
_FETCHER_STATE = {
    # One generated tree of series per (start, end) it is *asked to generate*.
    # The only caller that chooses a window is the load; slice fetches go
    # through `_covering_series`, which serves any sub-window from the entry
    # the load made and adds none. That reason is asserted, not assumed:
    # `test_the_mock_cache_does_not_grow_with_sliced_windows`.
    "MockDataFetcher._cache": "one entry per loaded window; slice fetches reuse it",
    # One `BindingSpec` per metric, copied from the tree at construction.
    "DbtDataFetcher.bindings": "one binding per metric",
    # The last statement per metric and kind of query (`m`, `m::dim`,
    # `m::dim::flows`, `m::grain`, `m::filter`), overwritten in place. The
    # dimension is a declared one, never a caller's string.
    "DbtDataFetcher.last_sql": "one statement per metric × declared dimension × query kind",
    # metric -> definition sha, memoized.
    "SnapshotFetcher._sha_cache": "one sha per metric",
    # A mirror of `manifest.json` and the files already warned about: one
    # record per snapshot file. Series snapshots are one per metric at the
    # loaded window; sliced ones are widened to `slice_span` before storing,
    # so one per (metric, dimension). It grows as the directory on disk does.
    "SnapshotStore._manifest_cache": "one record per snapshot file on disk",
    "SnapshotStore._warned": "at most one entry per snapshot file on disk",
}

_CONTAINER_FACTORIES = frozenset(
    {"dict", "list", "set", "deque", "defaultdict", "OrderedDict", "Counter", "BoundedCache"}
)


def _container_attributes(path: Path):
    """`Class.attr` for every instance attribute in `path` that is a container.

    By how it is assigned anywhere in the class (`self.x = {}`, `self.x =
    dict(...)`, a comprehension) or annotated (`self.x: Optional[dict] =
    None`) — the lazily-filled spelling included, since that is the one a
    default-value check cannot see.
    """
    found = {}
    tree = ast.parse(path.read_text())
    for klass in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for node in ast.walk(klass):
            if isinstance(node, ast.Assign):
                targets, value, annotation = node.targets, node.value, None
            elif isinstance(node, ast.AnnAssign):
                targets, value, annotation = [node.target], node.value, node.annotation
            else:
                continue
            is_container = isinstance(
                value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)
            ) or (
                isinstance(value, ast.Call)
                and (getattr(value.func, "id", None) or getattr(value.func, "attr", None))
                in _CONTAINER_FACTORIES
            )
            if annotation is not None and _CONTAINER_ANNOTATION.search(ast.unparse(annotation)):
                is_container = True
            if not is_container:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    found[f"{klass.name}.{target.attr}"] = node.lineno
    return found


def test_every_container_on_a_fetcher_is_classified():
    """Rule 2, one object over: `TreeState.fetcher` lives as long as the tree.

    The TreeState test could not see past the field — `fetcher: Any = None` is
    a scalar to it — so a cache on the provider was outside the invariant
    altogether, and `MockDataFetcher._cache`, keyed by `(start, end)`, is
    exactly the shape rule 2 was written about (grill 2026-10-05 M11).

    Enumerated by module rather than by base class, so the things a fetcher
    *holds* are swept too: `SnapshotStore` is not a fetcher and keeps two
    containers of its own.
    """
    modules = {inspect.getsourcefile(obj) for _name, obj in _fetcher_classes()}
    found = {}
    for module in sorted(modules):
        found.update(_container_attributes(Path(module)))
    assert found, "expected to find container state on the fetchers; did the scan break?"
    unclassified = sorted(f"{k} (line {v})" for k, v in found.items() if k not in _FETCHER_STATE)
    assert not unclassified, (
        f"{unclassified}: container state on a provider class that this test "
        "has never seen. The fetcher lives as long as its tree, so anything a "
        "request's window, dimension or date can add to must be bounded by "
        "bytes (rule 2). If it is sized by the tree, add it to "
        "`_FETCHER_STATE` and say what bounds it."
    )
    stale = sorted(set(_FETCHER_STATE) - set(found))
    assert not stale, f"{stale} are classified here and no longer exist on a provider class"


def test_the_mock_cache_does_not_grow_with_sliced_windows():
    """The reason behind `MockDataFetcher._cache`'s exemption, asserted.

    The cache is keyed by `(start, end)`, which a slice request chooses. It is
    exempt only because a sliced fetch inside the loaded window is served from
    the entry the load made. If that stops being true the key becomes a
    caller's, and the exemption is the `slice_cache` defect.
    """
    dag = Parser(
        "provider: {type: mock}\n"
        "metrics:\n"
        "  - name: sessions\n    source: mock.sessions\n"
        "    dimensions:\n      region: customer__region\n"
    ).dag
    fetcher = data_fetch.MockDataFetcher(dag=dag)
    fetcher.fetch_metric("sessions", "2024-01-01", "2024-04-09")
    assert len(fetcher._cache) == 1
    for day in range(1, 20):
        fetcher.fetch_metric_sliced(
            "sessions", "customer__region", f"2024-02-{day:02d}", f"2024-03-{day:02d}"
        )
    assert len(fetcher._cache) == 1, (
        f"19 sliced fetches inside the loaded window left {len(fetcher._cache)} "
        "entries in MockDataFetcher._cache. It is keyed by a window the caller "
        "chooses and is no longer bounded by the load (rule 2)."
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
        # Issue #113: at most one `{parent, reason}` record per parent.
        "dropped_parents",
        # Roadmap S24: one record per declared intervention at most, and the
        # node's own declared regime start — all three sized by the tree.
        "interventions",
        "dropped_interventions",
        "fit_start",
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


#: Stand-ins for a statement and a relation an operator would not want read
#: off an open route. The mock provider ignores both; they are here so the
#: redaction test below can look for them in every response.
_SENTINEL_SQL = "SENTINEL_SQL_fct_orders_private"
_SENTINEL_BIND = "SENTINEL_BIND_analytics_private"
_SENTINEL_QUERY = "SENTINEL_QUERY_generated_statement"

#: One tree carrying every degenerate shape a route can be handed, so that
#: each route is driven against all of them rather than against whichever one
#: its author thought of:
#:
#: - a zero denominator on a derived rate (`aov`) — the C17 formula case;
#: - a zero-variance parent (`promo`, a real parent of `bookings` and of
#:   `signups`) — the C4a case (an earlier edition declared `promo`
#:   standalone while its docstring called it a parent; the flat-parent path
#:   was never actually wired);
#: - a probabilistic node (`demand`) whose rate parent is **undefined inside
#:   the analysis window but outside the fit window** — the fit never sees the
#:   NaN, and only the attribution-time refusal stands between it and the
#:   encoder (roadmap C29, grill H1);
#: - an `inf` in a fetched flow (`revenue`) — roadmap C40;
#: - a fitted node (`signups`) with a lagged parent, a dropped parent, a
#:   declared intervention whose **posterior is non-finite**, and one the fit
#:   left out — the S24 shapes, which arrived after the three above and were
#:   in no degenerate case at all;
#: - a sliced flow and a sliced rate over the same poisoned series.
_DEGENERATE_TREE = f"""\
provider:
  type: mock
metrics:
  - name: sessions
    source: mock.sessions
    kind: flow
    sql: "SELECT d AS date, v AS value FROM {_SENTINEL_SQL}"
    dimensions:
      region: customer__region
  - name: order_count
    source: mock.order_count
    kind: flow
    bind:
      relation: {_SENTINEL_BIND}
      grain_key: order_id
      time_column: ordered_at
      agg: count
      measure: order_id
    dimensions:
      region: customer__region
  - name: revenue
    source: mock.revenue
    kind: flow
  - name: promo
    source: mock.promo
    kind: flow
  - name: aov
    source: mock.aov
    kind: rate
    formula: "revenue / order_count"
    parents: [revenue, order_count]
    dimensions:
      region:
        source: customer__region
        weight: order_count
  - name: demand
    source: mock.demand
    kind: flow
    parents: [aov]
  - name: signups
    source: mock.signups
    kind: flow
    parents: [sessions, promo]
    lags: {{sessions: 1}}
    interventions:
      - {{name: flip, date: 2024-04-01, kind: step}}
      - {{name: relaunch, date: 2024-04-08, kind: step}}
  - name: bookings
    source: mock.bookings
    kind: flow
    formula: "demand + promo + signups"
    parents: [demand, promo, signups]
"""

_ANALYSIS = {"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"}
_BASELINE = {"baseline_start": "2024-03-27", "baseline_end": "2024-04-09"}


def _pct(metric: str) -> dict:
    return {**_BASELINE, "interventions": [{"metric": metric, "mode": "pct", "value": 0.1}]}


#: Every JSON route the app serves, with the requests that drive it against
#: the degenerate tree. `(method, path template) -> [request, ...]`, where a
#: request is `{"path": {...}, "params": {...}, "json": {...}}`.
#:
#: Keyed by the route so that the enumeration below can ask the reviewer's
#: question: is there a route nobody has pointed at a degenerate tree? The
#: test this replaces drove three (grill 2026-10-05 M11) — `/rca/{name}`,
#: `/metrics/{name}` and `/series` — while `/rca/{name}/slices`, `/simulate`,
#: `/shapley/{name}`, `/analyze/{name}` and `/metrics/{name}/ppc` each return
#: engine numbers and were never touched.
_ROUTE_PROBES = {
    ("GET", "/"): [{}],
    ("GET", "/health"): [{}],
    ("GET", "/manifest"): [{}],
    ("GET", "/trees"): [{}],
    ("POST", "/trees/{tree_id}/load"): [{"path": {"tree_id": "degenerate"}}],
    ("GET", "/progress/{run_id}"): [{"path": {"run_id": "never-started"}}],
    ("GET", "/meta"): [{}],
    ("GET", "/dag"): [{}],
    ("GET", "/series"): [{}],
    ("GET", "/metrics/{name}/query"): [
        {"path": {"name": "sessions"}},
        {"path": {"name": "order_count"}, "params": {"dimension": "region"}},
    ],
    ("GET", "/metrics/{name}"): [
        {"path": {"name": name}}
        for name in ("aov", "revenue", "sessions", "order_count", "demand", "signups")
    ],
    ("GET", "/metrics/{name}/ppc"): [{"path": {"name": name}} for name in ("aov", "signups")],
    ("POST", "/analyze/{name}"): [{"path": {"name": "signups"}}],
    ("GET", "/shapley/{name}"): [
        {"path": {"name": name}, "params": _ANALYSIS} for name in ("aov", "bookings", "signups")
    ],
    ("POST", "/rca/{name}"): [
        {"path": {"name": name}, "params": _ANALYSIS}
        for name in ("aov", "revenue", "bookings", "demand", "signups")
    ],
    ("POST", "/rca/{name}/slices"): [
        *(
            {"path": {"name": name}, "params": {**_ANALYSIS, "dimension": "region"}}
            for name in ("sessions", "order_count", "aov")
        ),
        # A rate blend over a window the poisoned periods are outside of, so
        # the `slice_blend` shape is produced as well as refused.
        {
            "path": {"name": "aov"},
            "params": {
                "analysis_start": "2024-03-10",
                "analysis_end": "2024-03-23",
                "dimension": "region",
            },
        },
    ],
    ("POST", "/simulate"): [
        {"json": _pct(metric)} for metric in ("sessions", "revenue", "order_count", "aov")
    ],
}

#: Routes deliberately not driven against the degenerate tree, each with why
#: no engine result can reach its encoder.
_ROUTES_NOT_PROBED = {
    # FastAPI's own: the schema and the two pages that render it. Built from
    # route signatures, never from a tree.
    ("GET", "/openapi.json"): "the OpenAPI schema, built from signatures",
    ("GET", "/docs"): "Swagger UI (HTML)",
    ("GET", "/docs/oauth2-redirect"): "Swagger UI's OAuth helper (HTML)",
    ("GET", "/redoc"): "ReDoc (HTML)",
}

#: The MCP tools, driven the same way: `tool -> [arguments, ...]`.
_MCP_PROBES = {
    "list_trees": [{}],
    "get_tree": [{}],
    "explain_metric": [{"name": name} for name in ("aov", "revenue", "demand", "signups")],
    "run_rca": [{"target": name, **_ANALYSIS} for name in ("aov", "bookings", "demand", "signups")],
    "slice_metric": [
        {
            "name": name,
            "dimension": "region",
            "reference_start": "2024-01-31",
            "reference_end": "2024-03-26",
            **_ANALYSIS,
        }
        for name in ("sessions", "aov")
    ],
    "run_whatif": [_pct("sessions"), _pct("revenue")],
}


def _json_routes():
    """`(method, path)` for every route the app answers, aliases folded.

    Every route on the shared `router` is also served under
    `/trees/{tree_id}` by the same handler (the mount invariant below asserts
    that), so the bare path stands for both.
    """
    aliases = {TREE_PREFIX + r.path for r in router.routes}
    found = set()
    for route in _endpoint_routes():
        path = getattr(route, "path", None)
        if path is None or path in aliases:
            continue
        for method in sorted((getattr(route, "methods", None) or ()) - {"HEAD", "OPTIONS"}):
            found.add((method, path))
    return found


def _degenerate_stub_fit(target, parents, *, interventions=(), nonfinite_intervention=False, **kw):
    """A NUTS-labelled `FitResult` for the trace-cache seam: `run_rca` and
    `run_scenario` reuse it via `cached_fit_is_usable` and fit nothing."""
    import arviz as az

    from breakdown.engine.model import FitResult

    rng = np.random.default_rng(0)
    fit_dates = pd.date_range("2024-01-01", "2024-03-26", freq="D")
    posterior = {
        "alpha": rng.normal(size=(2, 50)),
        "beta_raw": rng.normal(size=(2, 50, len(parents))),
        "trend": rng.normal(size=(2, 50, len(fit_dates))),
    }
    if interventions:
        draws = rng.normal(size=(2, 50, len(interventions)))
        if nonfinite_intervention:
            draws[0, 0, 0] = float("nan")
        posterior["beta_intervention_raw"] = draws
    return FitResult(
        trace=az.from_dict(posterior=posterior),
        target=target,
        parents=list(parents),
        y_mean=0.0,
        y_std=1.0,
        x_stds=np.ones(len(parents)),
        dates=fit_dates,
        inference_method="nuts",
        fit_end=_ANALYSIS["analysis_start"],
        interventions=list(interventions),
        **kw,
    )


def _degenerate_fits() -> dict:
    """The two fitted nodes' stubs, keyed by metric."""
    flip = {
        "name": "flip",
        "date": "2024-04-01",
        "kind": "step",
        "until": None,
        "learn_from": "history",
    }
    return {
        "demand": _degenerate_stub_fit("demand", ["aov"]),
        # `promo` is held flat below, so a real fit would have dropped it
        # (issue #113); `relaunch` falls in the analysis window, which RCA's
        # fit never sees, so a real fit would have left it out (S24). And the
        # one intervention it did size has a NaN among its draws: rule 3's
        # newest shape, withheld by name or published as a number that is not
        # one.
        "signups": _degenerate_stub_fit(
            "signups",
            ["sessions"],
            interventions=[flip],
            nonfinite_intervention=True,
            dropped_parents=[{"parent": "promo", "reason": "held one value over the fit window"}],
            dropped_interventions=[
                {
                    "intervention": "relaunch",
                    "date": "2024-04-08",
                    "kind": "step",
                    "reason": "no instance inside the fit window",
                }
            ],
        ),
    }


class _DegenerateApp:
    """The app serving `_DEGENERATE_TREE`, poisoned, with no sampler behind it.

    A context manager rather than a fixture so two tests can boot it under
    different environments (with and without `BREAKDOWN_API_TOKEN`) and so its
    `TestClient` is closed before any other test opens the same `app`.
    """

    def __init__(self, tmp_path, monkeypatch, env=None):
        self.tmp_path, self.monkeypatch, self.env = tmp_path, monkeypatch, env or {}

    def __enter__(self):
        from fastapi.testclient import TestClient

        from breakdown.api import main as main_mod

        tree = self.tmp_path / "degenerate.yml"
        tree.write_text(_DEGENERATE_TREE)
        # monkeypatch, not os.environ: the app reads these at lifespan, so
        # leaving them set points every later test in the session at a
        # tmp_path tree that no longer exists. (Found the hard way — it
        # errored two README tests that pass in isolation, which is the
        # signature of exactly this mistake.)
        self.monkeypatch.setenv("BREAKDOWN_TREE", str(tree))
        self.monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
        self.monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")
        for key in ("BREAKDOWN_API_TOKEN", "BREAKDOWN_REQUIRE_AUTH", "BREAKDOWN_WARM"):
            self.monkeypatch.delenv(key, raising=False)
        for key, value in self.env.items():
            self.monkeypatch.setenv(key, value)

        # This file stays in the fast loop: nothing here may sample. The two
        # orchestrators find their fits in the cache; if either ever reaches
        # for the sampler the request fails loudly instead of quietly taking
        # a minute. `/analyze` is the one route whose whole job is to fit, so
        # it is handed the stub it would have produced.
        def no_sampling(*args, **kwargs):
            raise AssertionError("tests/test_project_invariants.py must not run a sampler")

        fits = _degenerate_fits()
        self.monkeypatch.setattr(rca_mod, "fit_metric", no_sampling)
        self.monkeypatch.setattr(simulate_mod, "fit_metric", no_sampling)
        self.monkeypatch.setattr(main_mod, "fit_metric", lambda dag, data, name, **kw: fits[name])

        # base_url matters for /mcp: the transport's DNS-rebinding protection
        # only admits localhost hosts.
        self._client = TestClient(
            app, base_url="http://127.0.0.1:9090", raise_server_exceptions=False
        )
        self.client = self._client.__enter__()
        self.state = app.state.trees["degenerate"]
        frame = self.state.data.frames["day"]
        # the structural zero `_align_to_spine` manufactures for a flow
        # denominator, and a parent held flat (the C4 production shape)
        frame.loc[frame.index[-3], "order_count"] = 0.0
        frame["promo"] = 0.0
        # 1.11c's undefined rate period, placed inside the analysis window so
        # the fit (which ends at analysis_start) can never have seen it — the
        # exact H1 shape.
        frame.loc[frame.index[-3], "aov"] = float("nan")
        # And an inf (roadmap C40, grill M3): `math.isnan(inf)` is False, so
        # ±inf sailed through three route sanitizers into the strict encoder
        # while their neighbours checked isfinite. DuckDB returns inf for
        # float division by zero, so the vector is a provider or a snapshot.
        frame.loc[frame.index[-4], "revenue"] = float("inf")
        # Under both keys the orchestrators look fits up by: RCA's
        # `analysis_start`, and what-if's `None` for a baseline that runs to
        # the end of the data.
        for name, fit in fits.items():
            self.state.traces[(name, _ANALYSIS["analysis_start"])] = fit
            self.state.traces[(name, None)] = fit
        return self

    def __exit__(self, *exc):
        return self._client.__exit__(*exc)

    def request(self, method, template, probe, headers=None):
        return self.client.request(
            method,
            template.format(**probe.get("path", {})),
            params=probe.get("params"),
            json=probe.get("json"),
            headers=headers,
        )

    def call_tool(self, name, arguments, headers=None):
        response = self.client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["result"]


@pytest.fixture(scope="module")
def degenerate_responses(tmp_path_factory):
    """Every probe in `_ROUTE_PROBES` and `_MCP_PROBES`, run once.

    `{"http": {(method, path): [(probe, status, body)]}, "mcp": {tool:
    [(arguments, result)]}}`. Recorded and the app closed again before any
    test reads it, so the tests that boot the app themselves are not sharing
    it with a module-scoped client.
    """
    monkeypatch = pytest.MonkeyPatch()
    recorded = {"http": {}, "mcp": {}}
    try:
        with _DegenerateApp(tmp_path_factory.mktemp("degenerate"), monkeypatch) as served:
            recorded["traces_type"] = type(served.state.traces)
            recorded["trace_budget"] = getattr(
                getattr(served.state.traces, "_store", None), "max_bytes", 0
            )
            for (method, template), probes in _ROUTE_PROBES.items():
                for probe in probes:
                    r = served.request(method, template, probe)
                    try:
                        body = r.json()
                    except ValueError:
                        body = None
                    recorded["http"].setdefault((method, template), []).append(
                        (probe, r.status_code, body)
                    )
            for tool, calls in _MCP_PROBES.items():
                for arguments in calls:
                    recorded["mcp"].setdefault(tool, []).append(
                        (arguments, served.call_tool(tool, arguments))
                    )
    finally:
        monkeypatch.undo()
    return recorded


@pytest.fixture(scope="module")
def fitted_example():
    """The bundled example tree with one real (short) NUTS fit behind it.

    The degenerate tree's fits are stubs, and a stub carries no `diagnostics`
    block and no PPC band — those exist only on a fit the engine made. This is
    the one place this file samples, so it is done once and shared:
    `{"metric": GET /metrics/order_count, "ppc": GET .../ppc, "rca": POST
    /rca/revenue}`.
    """
    from fastapi.testclient import TestClient

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.delenv("BREAKDOWN_TREE", raising=False)
        monkeypatch.setenv("BREAKDOWN_START_DATE", "2024-01-01")
        monkeypatch.setenv("BREAKDOWN_END_DATE", "2024-04-09")
        with TestClient(app) as client:
            assert client.post("/analyze/order_count?draws=150").status_code == 200
            return {
                "metric": client.get("/metrics/order_count").json(),
                "ppc": client.get("/metrics/order_count/ppc").json(),
                "rca": client.post(
                    "/rca/revenue",
                    params={"analysis_start": "2024-03-27", "analysis_end": "2024-04-09"},
                ).json(),
            }
    finally:
        monkeypatch.undo()


def test_every_json_route_is_driven_against_the_degenerate_tree():
    """Rule 3, structurally: a route cannot be added unexamined.

    Enumerated from the app (`_endpoint_routes`, which survives FastAPI's lazy
    includes), so the question is asked the day a route is added rather than
    the day a NaN reaches it: every route either has a probe that drives it
    against the degenerate tree, or is named with the reason no engine result
    can reach its encoder.
    """
    routes = _json_routes()
    assert len(routes) >= 15, f"only {len(routes)} routes found; the enumeration is too narrow"
    unexamined = sorted(routes - set(_ROUTE_PROBES) - set(_ROUTES_NOT_PROBED))
    assert not unexamined, (
        f"{unexamined}: routes that are never driven against a degenerate tree. "
        "Starlette encodes with `allow_nan=False`, so one non-finite float in a "
        "response is an unhandled 500 (rule 3, roadmap C17). Add a request to "
        "`_ROUTE_PROBES`, or name the route in `_ROUTES_NOT_PROBED` and say why "
        "no engine result can reach it."
    )
    stale = sorted((set(_ROUTE_PROBES) | set(_ROUTES_NOT_PROBED)) - routes)
    assert not stale, f"{stale} are listed here and are not routes of the app any more"
    both = sorted(set(_ROUTE_PROBES) & set(_ROUTES_NOT_PROBED))
    assert not both, f"{both} are both probed and exempt"


def test_every_mcp_tool_is_driven_against_the_degenerate_tree():
    """The same enumeration on the agent's side of the app."""
    tree = ast.parse((PACKAGE / "mcp" / "server.py").read_text())
    tools = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(ast.unparse(d).startswith("mcp.tool") for d in node.decorator_list)
    }
    assert len(tools) >= 6, "the MCP tool scan found nothing — has the decorator moved?"
    assert tools == set(_MCP_PROBES), (
        f"{sorted(tools ^ set(_MCP_PROBES))}: the MCP tools and `_MCP_PROBES` "
        "disagree. `round_floats` turns a NaN into `null` for an agent — a "
        "decomposition of nothing — so every tool is driven against the "
        "degenerate tree like every route (rule 3)."
    )


def _strict(payload) -> None:
    json.dumps(payload, allow_nan=False)


def test_a_degenerate_tree_still_encodes_strictly(degenerate_responses):
    """Rule 3, end to end through every route that can carry a NaN.

    One zero-denominator period used to reach Starlette's `allow_nan=False`
    encoder as an unhandled 500, and `round_floats` turned the same NaN into
    `null` for an agent (C17). The formula path was fixed then — and the
    posterior path of the same function published NaN for another year,
    because this test's one scenario contained no probabilistic node (roadmap
    C29, grill H1). Then it drove three routes and five more were never
    touched (grill 2026-10-05 M11). So the tree carries every degenerate shape
    (`_DEGENERATE_TREE` lists them) and every route is driven against it.

    The property asserted is the strict-encoding half of "every float a
    payload carries is finite or None": no route answers 500, and
    `json.dumps(..., allow_nan=False)` raises on any non-finite float.
    """
    crashed, unexplained = [], []
    for (method, template), results in degenerate_responses["http"].items():
        for probe, status, body in results:
            where = f"{method} {template} {probe}"
            if status >= 500 or body is None:
                crashed.append(f"{where} -> {status}")
                continue
            _strict(body)
            # A refusal names what was refused (rule 1's discipline, one
            # boundary over): never a bare status.
            if status >= 400 and not body.get("detail"):
                unexplained.append(f"{where} -> {status}")
    assert not crashed, (
        f"{crashed}: a 5xx on a degenerate but ordinary tree — a non-finite "
        "value reached the encoder, or something the engine raised about the "
        "caller's own request was not mapped to a 4xx (roadmap C17/C29/C40)."
    )
    assert not unexplained, f"{unexplained} refused without saying why"


def _degenerate_response(recorded, method, template, name):
    for probe, status, body in recorded["http"][(method, template)]:
        if probe.get("path", {}).get("name") == name:
            return status, body
    raise AssertionError(f"no probe for {method} {template} {name}")


def test_each_degenerate_shape_is_refused_or_withheld_by_name(degenerate_responses):
    """What the right answer *is* for each shape the degenerate tree carries —
    the strict-encoding test above only proves none of them was a 500."""

    def one(method, template, name):
        return _degenerate_response(degenerate_responses, method, template, name)

    # The poisoned probabilistic node degrades by name inside a wider
    # analysis ("one bad node does not end the analysis") …
    status, body = one("POST", "/rca/{name}", "bookings")
    assert status == 200
    demand = body["nodes"]["demand"]
    assert demand["status"] == "attribution_failed"
    assert "aov" in demand["status_reason"]

    # … and refuses by name when it is itself the target.
    status, body = one("POST", "/rca/{name}", "demand")
    assert status == 422, (
        f"the H1 shape must be a named refusal for the target, not a published NaN (got {status})"
    )
    assert "aov" in body["detail"]

    # The inf poked into `revenue` comes back withheld (`null`), not 500.
    status, body = one("GET", "/metrics/{name}", "revenue")
    assert status == 200, "GET /metrics/revenue choked on an inf (C40)"
    assert any(row["revenue"] is None for row in body["time_series"])
    status, body = one("GET", "/metrics/{name}", "aov")
    assert status == 200

    # The intervention whose posterior carried a NaN is withheld by name, on
    # the node that was otherwise attributed.
    status, body = one("POST", "/rca/{name}", "signups")
    assert status == 200
    (flip,) = body["nodes"]["signups"]["interventions"]
    assert flip["estimate"] is None and flip["ci_95"] is None
    assert flip["ci_status"] == "nonfinite_posterior"


def _withheld_without_a_reason(node: dict) -> list:
    """Every `null` on one RCA node (full or MCP-compacted) that stands where
    a number was withheld and has no named status beside it.

    A node that is not `ok` owes a `status_reason`. A node that is `ok` owes
    its three headline numbers. And an estimate that is `null` — a
    contribution's, a component's, a declared intervention's — owes a status
    that is not `ok`: on the entry itself where it has one, else on the node.
    """
    problems = []
    if node.get("status") != "ok":
        if not node.get("status_reason"):
            problems.append(f"status {node.get('status')!r} with no status_reason")
        return problems
    for field in ("baseline", "actual", "gap"):
        if node.get(field) is None:
            problems.append(f"status ok with `{field}: null`")
    node_flagged = node.get("ci_status") not in (None, "ok")
    for c in node.get("contributions") or []:
        if c.get("estimate") is None and not node_flagged:
            problems.append(f"contribution {c.get('parent')} has no estimate; ci_status is ok")
    for key, c in (node.get("components") or {}).items():
        if c.get("estimate") is None and not node_flagged:
            problems.append(f"component {key} has no estimate; ci_status is ok")
    for iv in node.get("interventions") or []:
        if iv.get("estimate") is None and iv.get("ci_status") in (None, "ok"):
            problems.append(f"intervention {iv.get('name')} has no estimate and no ci_status")
    return problems


def test_a_withheld_number_always_travels_with_a_named_status(degenerate_responses):
    """Rule 3's second sentence: *withheld with a named `ci_status`/`status`,
    never emitted and never quietly zeroed* — on both surfaces.

    Strict encoding proves no NaN was emitted. It cannot tell a `null` that
    says why from a `null` that is `round_floats` mopping up after a NaN,
    which is what an agent was handed before C17. So every RCA node the
    degenerate tree produces is read twice — as HTTP returns it and as
    `compact_rca` shapes it for MCP — and every `null` where a number belongs
    must have its reason next to it.
    """
    problems = []
    for probe, status, body in degenerate_responses["http"][("POST", "/rca/{name}")]:
        if status != 200:
            continue
        for name, node in body["nodes"].items():
            for problem in _withheld_without_a_reason(node):
                problems.append(f"POST /rca/{probe['path']['name']} nodes.{name}: {problem}")
    analysed = 0
    for arguments, result in degenerate_responses["mcp"]["run_rca"]:
        if result["isError"]:
            continue
        analysed += 1
        for name, node in result["structuredContent"]["result"]["nodes"].items():
            for problem in _withheld_without_a_reason(node):
                problems.append(f"MCP run_rca({arguments['target']}) nodes.{name}: {problem}")
    assert analysed, "expected at least one MCP run_rca to answer on the degenerate tree"
    assert not problems, (
        f"{problems}: a number was withheld and nothing says why. An agent "
        "handed `null` narrates it as zero or as missing data; a named status "
        "is what makes it a refusal (rule 3)."
    )


def test_every_mcp_tool_answers_or_refuses_by_name(degenerate_responses):
    """Rule 3 on the agent-facing side, tool by tool.

    Each tool's shaped output over the degenerate tree is either a result that
    encodes strictly, or an *anticipated* failure carrying the engine's own
    sentence. The third possibility — `Error executing tool <name>` and
    nothing else — is a crash, and a model handed one cannot recover, explain
    or stop.
    """
    for tool, calls in degenerate_responses["mcp"].items():
        for arguments, result in calls:
            where = f"MCP {tool}({arguments})"
            if result["isError"]:
                # The SDK prefixes every failure with `Error executing tool
                # <name>`; an anticipated one (`ToolError`) carries the
                # message after it, a crash carries nothing (mcp >= 2.1.0).
                text = result["content"][0]["text"]
                said = text.removeprefix(f"Error executing tool {tool}").strip(" :")
                assert said, (
                    f"{where} crashed instead of refusing: {text!r}. Whatever the "
                    "engine raised never reached the caller (see `_surface_refusals`)."
                )
                continue
            _strict(result["structuredContent"])

    # The withheld intervention survives compaction with its status.
    for arguments, result in degenerate_responses["mcp"]["run_rca"]:
        if arguments["target"] == "signups":
            (flip,) = result["structuredContent"]["result"]["nodes"]["signups"]["interventions"]
            assert flip["estimate"] is None and flip["ci_status"] == "nonfinite_posterior"


def test_a_served_tree_caches_fits_in_the_byte_bounded_store(degenerate_responses):
    """Rule 2's one field whose default is not its bound.

    `TreeState.traces` defaults to a plain dict and is replaced by a
    `TraceView` onto the process-wide `TraceStore` when the app registers the
    tree. The classification test takes that on trust; this is the check.
    """
    assert degenerate_responses["traces_type"] is trees_mod.TraceView, (
        f"a served tree's `traces` is a {degenerate_responses['traces_type'].__name__}, "
        "not a view onto the byte-bounded TraceStore (rule 2, roadmap C8)."
    )
    assert degenerate_responses["trace_budget"] > 0


def test_round_floats_never_emits_a_non_finite_number():
    """Rule 3 on the agent-facing side: `null` is the MCP shape of a NaN."""
    from breakdown.mcp.shaping import round_floats

    out = round_floats({"a": float("nan"), "b": [float("inf"), 1.5], "c": {"d": float("-inf")}})
    flat = [out["a"], *out["b"], out["c"]["d"]]
    assert all(v is None or (isinstance(v, float) and math.isfinite(v)) for v in flat)


# --- Rule 4: every coalition enumeration is capped ----------------------------


#: itertools callables that walk a combinatorial space.
_ENUMERATORS = frozenset(
    {"combinations", "permutations", "combinations_with_replacement", "product"}
)

#: A subset size written as a literal this small is polynomial, not a
#: coalition enumeration: `combinations(range(k), 2)` is the k(k-1)/2 pairs
#: `model._collinearity` reads off a correlation matrix. Anything larger, or a
#: size that is a variable (`for r in range(n): combinations(others, r)`), is
#: the O(2^n) walk rule 4 is about.
_POLYNOMIAL_SUBSET_SIZE = 3


def _itertools_names(tree: ast.AST):
    """How this module can spell an itertools enumerator: the local names
    bound by `from itertools import combinations [as c]`, and the names the
    module itself is bound to (`import itertools`, `import itertools as it`).
    """
    direct, modules = {}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "itertools":
            for alias in node.names:
                direct[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "itertools":
                    modules.add(alias.asname or "itertools")
    return direct, modules


def _is_exponent(node: ast.AST) -> bool:
    """`1 << n` or `2 ** n` with a non-literal `n`: the size of a power set."""
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.left, ast.Constant)
        and not isinstance(node.right, ast.Constant)
        and (
            (isinstance(node.op, ast.LShift) and node.left.value == 1)
            or (isinstance(node.op, ast.Pow) and node.left.value == 2)
        )
    )


def _subset_enumerations(func: ast.AST, direct, modules):
    """Every exponential enumeration inside one function, as `(what, lineno)`.

    Four spellings, because the import-statement form this replaces saw one:
    a bare imported name, `itertools.<name>` under any alias,
    `chain.from_iterable(combinations(...))` (the inner call is found like any
    other), and a bitmask walk — `range(1 << n)` or `range(2 ** n)`.
    """
    found = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name) and node.func.id in direct:
            name = direct[node.func.id]
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in modules
        ):
            name = node.func.attr
        if name in _ENUMERATORS:
            if name == "product":
                # `product(a, b)` is a fixed-arity grid. `product(x, repeat=n)`
                # is |x|^n and is the enumeration.
                repeat = next((kw.value for kw in node.keywords if kw.arg == "repeat"), None)
                size = repeat
                if repeat is None:
                    continue
            else:
                size = node.args[1] if len(node.args) > 1 else None
                size = size or next((kw.value for kw in node.keywords if kw.arg == "r"), None)
                # `permutations(x)` with no size is n!, the worst of them.
            if (
                isinstance(size, ast.Constant)
                and isinstance(size.value, int)
                and size.value <= _POLYNOMIAL_SUBSET_SIZE
            ):
                continue
            found.append((f"{name}(...)", node.lineno))
        elif getattr(node.func, "id", None) == "range" and any(
            _is_exponent(sub) for arg in node.args for sub in ast.walk(arg)
        ):
            found.append(("range(2^n)", node.lineno))
    return found


def _module_int_constants(tree: ast.Module) -> dict:
    """ALL_CAPS module-level names bound to an int literal, or imported from
    another `breakdown` module (where the same definition is required of them
    by `_resolve_cap`)."""
    constants = {}
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if (
                isinstance(target, ast.Name)
                and target.id.lstrip("_").isupper()
                and isinstance(value, ast.Constant)
                and isinstance(value.value, int)
                and not isinstance(value.value, bool)
            ):
                constants[target.id] = value.value
    return constants


def _cap_guards(func: ast.AST, constants: dict):
    """The caps this function refuses above, as `{name: value}`.

    A guard is an `if` that compares **a size** against **a named module-level
    integer constant** and whose body **raises**. All three parts are the
    property: a size, because the bound has to be on the thing enumerated; a
    named constant, because a bare `10` cannot be found, documented or tested
    for being small enough; a raise, because sampling above the cap would be a
    different number (rule 4's last sentence).

    "A size" is `len(...)` directly, or a local the function assigned from an
    expression containing `len(...)` (`n = len(parent_names)`).
    """
    sized_locals = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and any(
            isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "len"
            for sub in ast.walk(node.value)
        ):
            sized_locals |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    def is_size(expr: ast.AST) -> bool:
        return any(
            (isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "len")
            or (isinstance(sub, ast.Name) and sub.id in sized_locals)
            for sub in ast.walk(expr)
        )

    caps = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.If) or not any(isinstance(s, ast.Raise) for s in node.body):
            continue
        for compare in [n for n in ast.walk(node.test) if isinstance(n, ast.Compare)]:
            if not all(isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE)) for op in compare.ops):
                continue
            sides = [compare.left, *compare.comparators]
            named = [s.id for s in sides if isinstance(s, ast.Name) and s.id in constants]
            if named and any(
                is_size(s) for s in sides if not isinstance(s, ast.Name) or s.id not in constants
            ):
                caps.update({name: constants[name] for name in named})
    return caps


def _enumeration_report():
    """For every function in the package that enumerates subsets:
    `{(module, function): {"sites": [...], "caps": {name: value}}}`.

    A function's caps are the guards in its own body, plus those of any
    function in the same module it calls *before* its first enumeration —
    one hop, which is what `run_scenario` does (`_validate_scenario` holds the
    refusal and is its first statement). No further: a guard two calls away is
    a guard a refactor moves without anyone noticing.
    """
    report = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        direct, modules = _itertools_names(tree)
        constants = _module_int_constants(tree)
        functions = dict(_functions(tree))
        for qualname, func in functions.items():
            sites = _subset_enumerations(func, direct, modules)
            if not sites:
                continue
            first = min(lineno for _what, lineno in sites)
            caps = _cap_guards(func, constants)
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in functions
                    and node.lineno < first
                ):
                    caps.update(_cap_guards(functions[node.func.id], constants))
            module = str(path.relative_to(PACKAGE))
            report[(module, qualname)] = {"sites": sites, "caps": caps}
    return report


def test_every_subset_enumeration_is_refused_above_a_named_cap():
    """Rule 4, structurally: find the enumerations, then find their refusal.

    Both known enumerations are O(2^n) and both run while holding the tree's
    lock. `simulate.py` capped at 10 and explained why; `compute_shapley` had no
    cap at all until 2.18, at 20s for 12 parents and 80s for 14.

    The check this replaces was `"_MAX_" not in text` over the whole file
    (grill 2026-10-05 M11, and M6 of the grill before it). It passed for
    `model.py` on a *comment* naming `simulate.py`'s constant — the real cap,
    `MAX_SHAPLEY_PARENTS`, has no leading underscore — and it would have
    passed an uncapped loop added to `rca.py` on `_MAX_SHOWN_DATES`, which
    bounds a log line. Its finder saw one import spelling.

    So, on the AST: every call that walks a combinatorial space, in whatever
    spelling, must sit in a function that compares a size against a named
    module-level integer and raises (see `_cap_guards` for why each of those
    three is part of the property). And a named cap must be small enough to
    be one: 2^20 is 1,000x the work of 2^10.
    """
    report = _enumeration_report()
    assert report, "expected to find the subset enumerations; did the finder break?"
    uncapped = [
        f"breakdown/{module}:{lineno} {what} in {function}()"
        for (module, function), entry in sorted(report.items())
        if not entry["caps"]
        for what, lineno in entry["sites"]
    ]
    assert not uncapped, (
        f"{uncapped}: a subset enumeration with no refusal above a named cap in "
        "the function that runs it. An O(2^n) loop under the tree's lock needs "
        "`if len(...) > SOME_MAX: raise ValueError(<remedy>)` against a "
        "module-level constant (roadmap 2.18) — and not a sampled "
        "approximation above it, which is a different number."
    )
    # Measured end to end through `run_rca`: 10 parents ~3.5s, 12 ~20s, 14 ~80s.
    loose = {
        name: value
        for entry in report.values()
        for name, value in entry["caps"].items()
        if not 2 <= value <= 12
    }
    assert not loose, f"{loose}: 2^n coalitions at that n is not a bound"


def _refuses_at(cap: int, call) -> str:
    with pytest.raises(ValueError) as refusal:
        call(cap + 1)
    return str(refusal.value)


def _shapley_at(n: int):
    parents = [f"p{i}" for i in range(n)]
    return model_mod.compute_shapley(
        " + ".join(parents), parents, {p: 1.0 for p in parents}, {p: 2.0 for p in parents}
    )


def _scenario_at(n: int):
    dag = Parser(
        "metrics:\n" + "".join(f"  - name: m{i}\n    source: s.m{i}\n" for i in range(n))
    ).dag
    scenario = simulate_mod.ScenarioRequest(
        interventions=[
            simulate_mod.Intervention(metric=f"m{i}", mode="pct", value=0.1) for i in range(n)
        ]
    )
    return simulate_mod.run_scenario(dag, None, {}, scenario)


#: How to call each enumerating function with `n` players, so its refusal can
#: be exercised. Keyed like `_enumeration_report`: a new enumerator is absent
#: from here and fails the test below until someone shows it refusing.
_ENUMERATOR_PROBES = {
    ("engine/model.py", "compute_shapley"): _shapley_at,
    ("engine/simulate.py", "run_scenario"): _scenario_at,
}


def test_every_enumerator_refuses_at_one_above_its_cap():
    """Rule 4 behaviourally, at the chokepoint a library caller cannot bypass.

    The structural test finds a guard; this calls through it. Each enumerating
    function is run at one player above its own cap and must refuse — with a
    message that names the limit, so the caller knows what to get under, and
    says what it was counting.

    The probes are keyed by the functions the AST scan finds, so a third
    enumerator cannot be added without being shown to refuse.
    """
    report = _enumeration_report()
    unprobed = sorted(set(report) - set(_ENUMERATOR_PROBES))
    assert not unprobed, (
        f"{unprobed} enumerate subsets and have no entry in `_ENUMERATOR_PROBES`. "
        "Add a call that runs each with n players, so its refusal is exercised "
        "and not only located."
    )
    stale = sorted(set(_ENUMERATOR_PROBES) - set(report))
    assert not stale, f"{stale} are probed here and no longer enumerate anything"

    for key, call in _ENUMERATOR_PROBES.items():
        caps = report[key]["caps"]
        assert caps, f"{key} has no cap to probe; see the structural test"
        cap = min(caps.values())
        # At the cap itself the function must *not* refuse on size — otherwise
        # the constant is not the bound it is documented as.
        try:
            call(cap)
        except ValueError as e:
            assert "too many" not in str(e), f"{key} refuses at its own cap ({cap}): {e}"
        except Exception:
            pass  # anything else is the probe's fixture, not the cap
        message = _refuses_at(cap, call)
        assert "too many" in message and str(cap) in message, (
            f"{key} refused {cap + 1} players without naming its limit of "
            f"{cap}: {message!r}. A refusal that does not say what to get under "
            "leaves the caller guessing (rule 4: refuse above the cap *with a "
            "remedy*)."
        )


def test_compute_shapley_names_the_remedy_for_too_many_parents():
    """The remedy for a wide formula node is not obvious, so it is spelled out:
    split into intermediate sums, which keeps every attribution exact."""
    message = _refuses_at(model_mod.MAX_SHAPLEY_PARENTS, _shapley_at)
    assert "intermediate" in message and "exact" in message


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


def _ui_code() -> str:
    return _js_code(PACKAGE / "static" / "app.js") + _js_code(PACKAGE / "static" / "disclosures.js")


def _ui_reads(js: str, field: str) -> bool:
    """Whether the frontend reads a property called `field` anywhere.

    A property read: `x.field`, `x?.field`, `x["field"]`, or a destructuring
    `const { field } = x` — and not an assignment to it. The check this
    replaces was `field in js`, a substring test (grill 2026-10-05 M11):
    `status`, `gap` and `grain` could never fail it, a field named in a
    tooltip string passed it, and an object literal's own key passed it.

    What this still cannot tell is *whose* property is read — `w.status` on a
    what-if node satisfies `status` on an RCA node. There is no JS parser here
    (no build step, deliberately), so that is covered from the other side: the
    nested records are enumerated too, and a list nobody renders has entry
    fields nobody reads. `interventions` matched what-if's `w.interventions`
    for the whole of S24's development; `window_delta`, `claim` and `until`
    on its entries could not have.
    """
    f = re.escape(field)
    return bool(
        re.search(rf"\.{f}\b(?!\s*=(?!=))", js)
        or re.search(rf"\[\s*[\"']{f}[\"']\s*\]", js)
        or re.search(rf"(?:const|let|var)\s*\{{[^{{}}]*\b{f}\b[^{{}}]*\}}\s*=", js)
    )


def _records(found) -> list:
    return [r for r in found if isinstance(r, dict)]


def _rca_nodes(body):
    return list(body["nodes"].values())


#: The payloads the UI renders, as families of records whose keys are the
#: fields a reader may or may not get to see. `route -> {family: extractor}`,
#: where the extractor pulls every record of that family out of one 200 body.
#: Nested entries are families of their own, because that is where M8's class
#: of defect lives: a top-level key can be read while nothing reads what is
#: inside it.
_RENDERED_PAYLOADS = {
    ("GET", "/health"): {"/health": lambda b: [b]},
    ("GET", "/trees"): {"/trees": lambda b: [b], "tree card": lambda b: b["trees"]},
    ("POST", "/trees/{tree_id}/load"): {"tree card": lambda b: [b]},
    ("GET", "/meta"): {"/meta": lambda b: [b]},
    ("GET", "/series"): {"/series entry": lambda b: list(b["metrics"].values())},
    ("GET", "/metrics/{name}/query"): {"/metrics/{name}/query": lambda b: [b]},
    # Not `diagnostics`, and not the PPC `band`: both are built only by a real
    # fit, and the stubs behind the degenerate tree carry neither. They are
    # read off the one fit this file does run — `_FITTED_PAYLOADS` below.
    ("GET", "/metrics/{name}"): {
        "/metrics/{name}": lambda b: [b],
        "/metrics/{name}.fit_window": lambda b: [b["fit_window"]],
    },
    ("GET", "/metrics/{name}/ppc"): {"/metrics/{name}/ppc": lambda b: [b]},
    ("POST", "/analyze/{name}"): {"/analyze/{name}": lambda b: [b]},
    ("POST", "/rca/{name}"): {
        "rca": lambda b: [b],
        "rca.node": _rca_nodes,
        "rca.contribution": lambda b: [c for n in _rca_nodes(b) for c in n["contributions"] or []],
        "rca.intervention": lambda b: [c for n in _rca_nodes(b) for c in n["interventions"] or []],
        "rca.dropped_parent": lambda b: [
            c for n in _rca_nodes(b) for c in n["dropped_parents"] or []
        ],
        "rca.dropped_intervention": lambda b: [
            c for n in _rca_nodes(b) for c in n["dropped_interventions"] or []
        ],
        "rca.component": lambda b: [
            c for n in _rca_nodes(b) for c in (n["components"] or {}).values()
        ],
        "rca.fit_window": lambda b: [n["fit_window"] for n in _rca_nodes(b)],
        "rca.effective_window": lambda b: [
            w for n in _rca_nodes(b) for w in (n["effective_windows"] or {}).values()
        ],
        "rca.ranked_cause": lambda b: b["ranked_causes"],
        "rca.reference_sensitivity": lambda b: [b.get("reference_sensitivity")],
        "rca.alternative": lambda b: (
            (b.get("reference_sensitivity") or {}).get("alternatives") or []
        ),
    },
    ("POST", "/rca/{name}/slices"): {
        "slice": lambda b: [b],
        "slice.slice": lambda b: b["slices"],
        "slice.reconciliation": lambda b: [b["reconciliation"]],
    },
    ("POST", "/simulate"): {
        "simulate": lambda b: [b],
        "simulate.node": lambda b: list(b["nodes"].values()),
        "simulate.source": lambda b: b["sources"],
        "simulate.warning": lambda b: b["warnings"],
    },
}

#: Routes whose payload is not a rendering contract with the browser.
_UNRENDERED_PAYLOADS = {
    ("GET", "/"): "a one-line 'the API is running' message",
    ("GET", "/manifest"): "deployment identity for probes and agents; the UI never requests it",
    ("GET", "/progress/{run_id}"): (
        "stage records are whatever the engine's progress callback reports "
        "mid-run; a finished run has none to enumerate"
    ),
    ("GET", "/dag"): (
        "the author's own tree definition, echoed back; what the engine did "
        "with each declaration is on the analysis payloads above"
    ),
    ("GET", "/shapley/{name}"): (
        "the UI never requests it: POST /rca/{name} carries the same attribution"
    ),
}

#: Fields a rendered payload carries that no surface of the UI reads, on
#: purpose, each with why a reader does not need it. `family.field -> reason`.
#: This list is the thing a reviewer reads: a field belongs here only if the
#: page is *complete* without it.
_DELIBERATELY_UNRENDERED = {
    # Operational: for a monitor polling unauthenticated (GitHub #117), which
    # is why they are on the open route at all. The browser's copy of the
    # same three facts is `/meta`'s, and that copy is checked.
    "/health.data_through_bounded_by": "monitor-facing; the UI anchors on /meta.data_through",
    "/health.short_series": "monitor-facing; the browser's copy is /meta's",
    "/health.sparse_fills": "monitor-facing; the browser's copy is /meta's",
    # `/dag` carries every node's `kind` on its definition, which is what the
    # cards read; this is the same map for a client that skips `/dag`.
    "/meta.kinds": "duplicates definition.kind from /dag, which the UI reads",
    # Roadmap 3.10's background warm: whether the cache is being pre-filled.
    # It changes how long the first analysis takes and nothing about what any
    # number means.
    "/meta.warm": "operational status of the background warm",
    # The cache key the fit was stored under (exclusive). `fit_window`, which
    # is rendered, is the same fact in the form a reader can use: the periods
    # actually trained on.
    "/metrics/{name}.fit_end": "the trace-cache key; fit_window is the rendered form",
    # The declaration behind `claim` and `fit_window.extended_for`, both of
    # which are rendered: `learn_from: window` is what makes them appear.
    "rca.intervention.learn_from": "rendered through its consequences, claim and extended_for",
    # The machine id of the alternative block; `label` is the same thing in
    # words and is what the details line prints.
    "rca.alternative.shift": "machine id; `label` is rendered",
    # The provider-side name of the dimension (`customer__region`). The
    # declared name (`dimension`) is rendered; the source name is provenance,
    # shown by the query panel.
    "slice.dimension_source": "provider-side name; the declared dimension is rendered",
    # The verdict (`localization`) is the engine's and is rendered as the
    # engine reached it; the threshold it was reached against is a constant
    # documented in docs/model.md, published so an agent need not hard-code it.
    "slice.localization_threshold": "the constant behind the rendered verdict",
    # Which side folded the slices outside top_k, and why. It changes where
    # the work happened and not one number on the panel.
    "slice.rollup": "operational: where the top_k fold ran",
    # The slice's share of the weight in the reference window — the same
    # number as `baseline_share` on the same row (`slices.py` writes both from
    # `s_ref[j]`), and `baseline_share` is the one every consumer reads (C24).
    "slice.slice.share_reference": "the same number as the rendered baseline_share",
    # `status` and the residual's share of baseline are rendered; this is the
    # worst single period behind them.
    "slice.reconciliation.max_abs_residual": "detail behind the rendered reconciliation status",
    # Fixed at 0 so two identical requests agree; nothing a reader acts on.
    "simulate.seed": "the fixed seed behind reproducibility",
}


def test_every_rendered_route_is_classified():
    """Every route the degenerate probes drive either has its payload's fields
    checked against the UI, or says why the browser never renders it — so a
    new route's payload cannot skip the render-site check by not being
    listed."""
    known = set(_RENDERED_PAYLOADS) | set(_UNRENDERED_PAYLOADS)
    assert known == set(_ROUTE_PROBES), (
        f"{sorted(known ^ set(_ROUTE_PROBES))}: routes that are probed but not "
        "classified as rendered or unrendered, or the reverse. A route's "
        "payload is a contract with whatever draws it (the fifth rule)."
    )
    assert not set(_RENDERED_PAYLOADS) & set(_UNRENDERED_PAYLOADS)


#: The records only a real fit produces, out of `fitted_example`.
_FITTED_PAYLOADS = {
    "fit.diagnostics": lambda f: [f["metric"]["diagnostics"]],
    "fit.diagnostics.ppc": lambda f: [f["metric"]["diagnostics"].get("ppc")],
    "fit.diagnostics.ppc.statistic": lambda f: (
        (f["metric"]["diagnostics"].get("ppc") or {}).get("statistics") or []
    ),
    "fit.ppc_band": lambda f: [f["ppc"]["band"]],
}


def _payload_fields(recorded, fitted) -> dict:
    """`{family: {field, ...}}` over every 200 the degenerate probes recorded,
    plus the fit-only records of the one real fit."""
    families = {}
    for route, extractors in _RENDERED_PAYLOADS.items():
        for _probe, status, body in recorded["http"][route]:
            if status != 200:
                continue
            for family, extract in extractors.items():
                fields = families.setdefault(family, set())
                for record in _records(extract(body)):
                    fields |= set(record)
    # The engine's own list of every field a node can carry: a degraded node
    # answers the same shape as an attributed one, so nothing is missed by the
    # degenerate tree happening not to produce it.
    families["rca.node"] |= set(rca_mod._node_out())
    for family, extract in _FITTED_PAYLOADS.items():
        fields = families.setdefault(family, set())
        for record in _records(extract(fitted)):
            fields |= set(record)
    return families


def test_every_payload_field_reaches_a_render_site(degenerate_responses, fitted_example):
    """The invariant that would have caught grill H7 (roadmap C35) and, two
    reviews later, M8: the engine emitted `inference_method` on every RCA
    node, MCP published it with a comment on why an agent needs it — and no UI
    surface ever read it, so an ADVI analysis rendered byte-identical to a
    NUTS one for a year. Then `/meta` grew `sparse_fills`, MCP explained it to
    agents, and a browser user was shown a run of declared zeros as data.

    The first edition of this test enumerated `_node_out()` only, with a
    substring match. So: enumerated from the payloads themselves — every
    route the UI requests, and every nested record inside each — and matched
    as a property read (`_ui_reads`). Each field is read somewhere in the
    frontend, or is in `_DELIBERATELY_UNRENDERED` with the reason a reader's
    page is complete without it. A new field lands in neither and fails here —
    which is the only moment anyone will ask the question.

    This is the mechanical half of the fifth rule. It finds a field nobody
    reads; it cannot find a field read and then dropped by a filter (grill
    2026-10-05 H5), which still takes opening the UI.
    """
    js = _ui_code()
    families = _payload_fields(degenerate_responses, fitted_example)
    empty = sorted(name for name, fields in families.items() if not fields)
    declared = {family for extractors in _RENDERED_PAYLOADS.values() for family in extractors}
    declared |= set(_FITTED_PAYLOADS)
    assert not empty and declared == set(families), (
        f"{empty or sorted(declared ^ set(families))}: a payload family the degenerate "
        "tree never produced, so its fields are unchecked. Extend the tree "
        "(`_DEGENERATE_TREE`) or its stub fits until it does."
    )
    emitted = {f"{family}.{field}" for family, fields in families.items() for field in fields}
    stale = sorted(set(_DELIBERATELY_UNRENDERED) - emitted)
    assert not stale, f"{stale} are exempt from rendering and no payload carries them any more"

    unread = {
        f"{family}.{field}": "emitted, and read by no surface of the UI"
        for family, fields in families.items()
        for field in fields
        if f"{family}.{field}" not in _DELIBERATELY_UNRENDERED and not _ui_reads(js, field)
    }
    _settle(
        "render-site",
        unread,
        "Payload fields the server emits and no frontend surface reads. Render "
        "each on at least one surface (new wording goes in disclosures.js), or "
        "add it to `_DELIBERATELY_UNRENDERED` with the reason a reader never "
        "needs it (roadmap C35 — a correct payload rendered incompletely is "
        "the fifth rule's defect):",
    )


# --- Every status the engine can emit has words in the disclosure vocabulary --


def _string_leaves(expr: ast.AST) -> set:
    """The string constants an expression can evaluate to, through
    conditionals: `"a" if x else "b" if y else "c"` is `{a, b, c}`."""
    if isinstance(expr, ast.Constant):
        return {expr.value} if isinstance(expr.value, str) else set()
    if isinstance(expr, ast.IfExp):
        return _string_leaves(expr.body) | _string_leaves(expr.orelse)
    if isinstance(expr, ast.BoolOp):
        return set().union(*(_string_leaves(v) for v in expr.values))
    return set()


def _emitted_strings(source, *, name=None, key=None, keyword=None, within=None, callee=None):
    """Every string literal a Python module can put under one payload key.

    Three ways a value reaches a payload, and each field uses a different
    one: an assignment to a local later passed through (`name="ci_status"`), a
    dict literal's value (`key="fit_quality"`), and a keyword argument
    (`keyword="status"`, optionally only in calls to `callee`, or only to
    anything *but* it when `callee` starts with `!`). `within` restricts the
    scan to the named functions.
    """
    tree = ast.parse(Path(source).read_text())
    scopes = [tree]
    if within:
        scopes = [fn for qualname, fn in _functions(tree) if qualname in within]
        assert len(scopes) == len(within), f"{within} not all found in {source}"
    found = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if name and isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    found |= _string_leaves(node.value)
            if key and isinstance(node, ast.Dict):
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == key:
                        found |= _string_leaves(v)
            if keyword and isinstance(node, ast.Call):
                called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if callee and (called == callee.lstrip("!")) == callee.startswith("!"):
                    continue
                for kw in node.keywords:
                    if kw.arg == keyword:
                        found |= _string_leaves(kw.value)
    return found


def _js_table(js: str, name: str) -> set:
    """The keys of a top-level `const NAME = { key: ..., }` table."""
    match = re.search(rf"^const {name} = \{{\n(.*?)^\}};", js, re.S | re.M)
    assert match, f"disclosures.js no longer declares a `{name}` table"
    return set(re.findall(r"^  ([A-Za-z_]\w*):", match.group(1), re.M))


def _js_function(js: str, name: str) -> str:
    match = re.search(rf"^function {name}\(.*?^\}}", js, re.S | re.M)
    assert match, f"disclosures.js no longer defines `{name}`"
    return match.group(0)


def test_every_status_the_engine_emits_has_an_entry_in_the_disclosure_vocabulary():
    """The fifth rule's vocabulary, held to the engine's.

    `disclosures.js` is where an engine verdict becomes words, one table per
    field. Each table has an unknown-value fallback, which stops a new status
    rendering as *nothing* — but "interval flagged: nonfinite_posterior" is
    not an explanation, and `INTERVENTION_CI_NOTE` has no fallback at all
    (grill 2026-10-05 L8). The only test tying the two sides together asserted
    that an engine literal was in the engine's own tuple (M4).

    So for each field with a table, enumerate what the Python can emit and
    require every value but the silent `ok` to have an entry. The Python side
    comes from a declared tuple where one exists — only
    `REFERENCE_SENSITIVITY_STATUSES` does — and is otherwise read off the AST
    as the string literals that reach the key (`_emitted_strings`). Where the
    AST is the source, the declared tuple is checked against it too.
    """
    js = _js_code(PACKAGE / "static" / "disclosures.js")
    rca, slices, model = (PACKAGE / "engine" / f for f in ("rca.py", "slices.py", "model.py"))

    # AST: `_node_out(status=...)`, the only way a node gets a status.
    node_status = _emitted_strings(rca, keyword="status", callee="_node_out")
    # AST: the `ci_status = (...)` each attribution branch computes, and the
    # slice result's own, which the slice panel passes through the same table.
    node_ci = _emitted_strings(rca, name="ci_status") | _emitted_strings(slices, key="ci_status")
    # AST: `entry.update(ci_status=...)` on an intervention record — every
    # `ci_status=` keyword that is not the node's own.
    intervention_ci = _emitted_strings(rca, keyword="ci_status", callee="!_node_out")
    # AST: the two diagnostics builders' `"fit_quality": ...`.
    fit_quality = _emitted_strings(model, key="fit_quality")
    # AST: how a decomposition says it was computed.
    attribution = (
        _emitted_strings(rca, name="attribution_method")
        | _emitted_strings(rca, key="attribution_method")
        | _emitted_strings(slices, key="attribution_method")
    )
    # Declared, then cross-checked against the AST of the function that
    # builds the field.
    sensitivity = set(rca_mod.REFERENCE_SENSITIVITY_STATUSES)
    built = _emitted_strings(rca, key="status", within=["_reference_sensitivity"])
    assert built and built <= sensitivity, (
        f"`_reference_sensitivity` can emit {sorted(built - sensitivity)}, which "
        "`REFERENCE_SENSITIVITY_STATUSES` does not declare."
    )

    for emitted, what in (
        (node_status, "node status"),
        (node_ci, "ci_status"),
        (intervention_ci, "intervention ci_status"),
        (fit_quality, "fit_quality"),
        (attribution, "attribution_method"),
    ):
        assert len(emitted) >= 2, f"the scan for {what} found {emitted}; has the code moved?"

    missing = []
    for emitted, table, silent in (
        (node_status, "NODE_STATUS", {"ok"}),
        (node_ci, "CI_STATUS_NOTE", {"ok"}),
        (intervention_ci, "INTERVENTION_CI_NOTE", {"ok"}),
        (attribution, "ATTRIBUTION_LABEL", set()),
        (sensitivity, "REFERENCE_SENSITIVITY_NOTE", set()),
    ):
        for status in sorted(emitted - silent - _js_table(js, table)):
            missing.append(f"{table}.{status}")
    # `fit_quality` has a function, not a table: `fitQualityNote` branches on
    # each verdict it can explain.
    explained = _js_function(js, "fitQualityNote")
    for status in sorted(fit_quality - {"ok"}):
        if f'"{status}"' not in explained:
            missing.append(f"fitQualityNote: {status}")
    assert not missing, (
        f"{missing}: statuses the engine can emit that disclosures.js has no "
        "words for. A reader gets the raw identifier, or — where the lookup "
        "has no fallback — nothing at all. Add the entry (new verdict wording "
        "goes in disclosures.js, never inline in a renderer)."
    )


def test_an_unanswered_alternative_or_a_dropped_term_carries_its_reason():
    """The two fields with no table, because their words are the engine's own.

    An alternative reference block that could not answer, and a declared
    intervention the fit left out, are each rendered as "not checked — " /
    "not fitted: " followed by the engine's `reason`, verbatim. There is no
    vocabulary to keep in step, so the property is that the reason is always
    there to print: every status but `ok` an alternative can be given comes
    with a `reason` in the same call, every dropped record is built with one,
    and the renderer branches on `!== "ok"` rather than on a list of statuses
    it knows (`gap_unavailable` is in no table and no doc — grill L8).

    Read off the AST of the three functions that build these records.
    """
    tree = ast.parse((PACKAGE / "engine" / "rca.py").read_text())
    functions = dict(_functions(tree))
    statuses, unreasoned = set(), []
    for qualname in ("_reference_alternatives", "_reference_sensitivity"):
        assert qualname in functions, (
            f"rca.py no longer defines `{qualname}`; point this test at whatever "
            "builds the reference-sensitivity alternatives now."
        )
        for node in ast.walk(functions[qualname]):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "update"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            given = _string_leaves(kwargs["status"]) if "status" in kwargs else set()
            statuses |= given
            reason = kwargs.get("reason")
            has_reason = reason is not None and not (
                isinstance(reason, ast.Constant) and reason.value is None
            )
            if given - {"ok"} and not has_reason:
                unreasoned.append(f"rca.py:{node.lineno} status={sorted(given)}")
    assert statuses >= {"ok", "unavailable"}, f"the alternative-status scan found {statuses}"
    assert not unreasoned, (
        f"{unreasoned}: an alternative reference block is marked as not "
        "answering with no `reason`. The UI prints 'not checked — <reason>'; "
        "without one it prints 'no answer', which is not a disclosure."
    )

    model_tree = ast.parse((PACKAGE / "engine" / "model.py").read_text())
    builder = dict(_functions(model_tree)).get("_intervention_columns")
    assert builder is not None, (
        "model.py no longer defines `_intervention_columns`; point this test at "
        "whatever builds the dropped-intervention records now."
    )
    dropped = [
        node
        for node in ast.walk(builder)
        if isinstance(node, ast.Dict)
        and any(isinstance(k, ast.Constant) and k.value == "intervention" for k in node.keys)
    ]
    assert dropped, "expected `_intervention_columns` to build the dropped-intervention records"
    for record in dropped:
        keys = {k.value for k in record.keys if isinstance(k, ast.Constant)}
        assert "reason" in keys, (
            f"model.py:{record.lineno} builds a dropped-intervention record with "
            f"no `reason` ({sorted(keys)}): a declared step absent from the table "
            "reads as 'no effect' unless something says 'not fitted', and why."
        )

    js = _js_code(PACKAGE / "static" / "disclosures.js")
    assert re.search(r"\.status\s*!==\s*\"ok\"", _js_function(js, "referenceSensitivityDetails")), (
        "`referenceSensitivityDetails` no longer treats every non-`ok` "
        "alternative as not checked; a status it does not list would render as "
        "an answered block."
    )
    for renderer in ("referenceSensitivityDetails", "droppedInterventionRowsHtml"):
        assert re.search(r"\.reason\b", _js_function(js, renderer)), (
            f"`{renderer}` no longer prints the engine's `reason`"
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
        line
        for line in app_js.splitlines()
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
    _check_contiguous(kept, "day", ["m"])
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


# --- disclosures.js and app.js never declare the same top-level name ---------


def test_the_two_ui_scripts_declare_disjoint_top_level_names():
    """The fifth rule, third instance, and this one rendered a correct payload
    as `undefined = —`.

    The UI is two classic scripts sharing one global lexical environment
    (AGENTS.md: the disclosure vocabulary in `disclosures.js`, loaded first;
    everything else in `app.js`). A `function` declared in both is not an
    error in that world — the later file silently wins. S24 added
    `interventionLabel(iv)` to `disclosures.js` for a declared step or pulse
    (`{name, kind, date, until}`), and `app.js` had carried a what-if
    `interventionLabel(iv)` for `{metric, mode, value}` since 0.1.0. app.js
    loads second, so every S24 row in the coefficient table, the RCA node
    detail and the export read "tier_2_flip — undefined = —" while the
    payload behind it was right. The renderers had been checked in Node
    against `disclosures.js` alone, which is exactly how a collision with the
    *other* file goes unseen.

    So: the set of names each file declares at column zero must be disjoint.
    Structural, not a pin — it fails on the next collision, whatever its name.
    """
    decl = re.compile(r"^(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)
    declared = {
        name: set(decl.findall(_js_code(PACKAGE / "static" / name)))
        for name in ("disclosures.js", "app.js")
    }
    shared = declared["disclosures.js"] & declared["app.js"]
    assert not shared, (
        "declared at top level in both disclosures.js and app.js — classic "
        "scripts share one global scope, so app.js's definition silently "
        f"replaces the vocabulary's: {sorted(shared)}"
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


# --- A policy applied over HTTP is applied over MCP (grill 2026-10-05) --------
#
# The meta-defect, a fourth time, and this time the neighbour was a whole
# surface: HTTP routes ran the engine through `_guarded` (C41) while the MCP
# tools called `asyncio.to_thread` directly; HTTP mapped `RuntimeError` to a
# 422 with its message (C38) while MCP's `_REFUSALS` — whose own docstring
# claims parity with `api/main.py` — did not list it; `/dag` redacted `sql`
# and `bind` behind the token (C31) while `GET /metrics/{name}` returned
# `metric.model_dump()`. Each was fixed on the surface it was reported on.
#
# So the parity is asserted rather than remembered. Both halves read the HTTP
# side off `api/main.py` — what it guards, what it maps to a 4xx — and require
# the MCP side to do the same, so a seventh tool or a new refusal is asked the
# question on the day it is added.


def _import_aliases(tree: ast.AST) -> dict:
    """`{local name: imported name}` for every `from x import a as b`, at any
    depth — `mcp/server.py` imports `run_rca as _engine_run_rca` at the top
    and `_run_slice` inside a function body."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
    return aliases


def _to_thread_calls(func: ast.AST):
    """Every `asyncio.to_thread(...)` (or bare `to_thread(...)`) in a function."""
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "to_thread"
    ]


def _engine_entry_points_http_guards() -> set:
    """The callables `api/main.py` hands to `_guarded`: the engine's entry
    points, as the service layer itself defines the set."""
    tree = ast.parse((PACKAGE / "api" / "main.py").read_text())
    aliases = _import_aliases(tree)
    guarded = set()
    for call in _to_thread_calls(tree):
        if len(call.args) >= 3 and getattr(call.args[0], "id", None) == "_guarded":
            name = getattr(call.args[2], "id", None)
            if name:
                guarded.add(aliases.get(name, name))
    return guarded


def test_every_engine_call_from_mcp_goes_through_the_same_guard_as_http():
    """C41's guard, on the surface most likely to need it.

    An MCP client is the caller most likely to time out on a multi-minute
    `run_rca`. Its orphaned engine thread keeps running; if the tool that
    started it took no guard, the next request starts a second sampler beside
    it — the OOM `_guarded` exists to prevent — and HTTP's 409 for the same
    tree at the same moment is a policy only one door enforces (grill H7).

    Two checks over every function in `mcp/server.py`: nothing reaches a
    worker thread except through `_guarded`, and nothing `api/main.py` guards
    is called inline either (which would also put it on the event loop — C33).
    """
    guarded = _engine_entry_points_http_guards()
    assert {"run_rca", "run_scenario", "_run_slice", "fit_metric"} <= guarded, (
        f"the scan of api/main.py found only {sorted(guarded)} behind `_guarded`; "
        "has the call shape changed?"
    )

    tree = ast.parse((PACKAGE / "mcp" / "server.py").read_text())
    aliases = _import_aliases(tree)
    # Every function in the module, not only the decorated tools: a helper
    # the tools share is where the next unguarded `to_thread` would hide.
    unguarded = {}
    for qualname, func in _functions(tree):
        for call in _to_thread_calls(func):
            first = call.args[0] if call.args else None
            if getattr(first, "id", None) != "_guarded":
                target = aliases.get(getattr(first, "id", None)) or (
                    ast.unparse(first) if first is not None else "?"
                )
                unguarded[qualname] = (
                    f"line {call.lineno}: `to_thread({target}, ...)` runs the engine "
                    "with no guard; use `to_thread(_guarded, state, ...)`"
                )
        for node in ast.walk(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and aliases.get(node.func.id, node.func.id) in guarded
            ):
                unguarded[qualname] = (
                    f"line {node.lineno}: calls `{node.func.id}(...)` inline — on the "
                    "event loop and outside the guard"
                )
    _settle(
        "mcp-guard",
        unguarded,
        "MCP tools that run the engine outside `_guarded`, which every HTTP "
        "route goes through (roadmap C41). An orphaned run on this tree would "
        "not stop a second one starting beside it:",
    )


def _exceptions_http_answers_with_a_4xx() -> dict:
    """`{exception class: where}` for everything `api/main.py` turns into a
    4xx **around an engine call**, read off the module.

    An `except X: raise HTTPException(status_code=4xx)` whose `try` wraps a
    `to_thread` call. Restricted to engine calls on purpose — `except
    KeyError` around a series lookup is a 404 about the caller's metric name,
    not a statement that the engine's `KeyError`s are refusals. (`EngineBusy`
    is mapped by an `@app.exception_handler` instead and is raised by the
    guard, not the engine; `test_a_busy_engine_is_a_named_refusal_over_mcp`
    covers it by holding the guard.)
    """
    from breakdown.api import main as main_mod

    tree = ast.parse((PACKAGE / "api" / "main.py").read_text())

    def status_of(node: ast.AST):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                for kw in sub.keywords:
                    if kw.arg == "status_code" and isinstance(kw.value, ast.Constant):
                        return kw.value.value
        return None

    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(_to_thread_calls(stmt) for stmt in node.body):
            for handler in node.handlers:
                status = status_of(handler)
                if handler.type is None or status is None or not 400 <= status < 500:
                    continue
                names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                for name in names:
                    found.setdefault(
                        ast.unparse(name), f"except -> {status} (line {handler.lineno})"
                    )
    import builtins

    return {
        getattr(main_mod, name, None) or getattr(builtins, name): where
        for name, where in found.items()
    }


def test_everything_http_refuses_with_a_message_mcp_refuses_with_a_message():
    """C38's mapping, on both surfaces, behaviourally.

    For a month node with a short day parent, HTTP `/simulate` answers 422
    naming the parent that runs no whole month; MCP `run_whatif` answered
    `Error executing tool run_whatif` and nothing else (grill M6) — the
    per-metric-window refusal #135 wrote *for readers*, withheld from the
    reader with no log to open.

    Every exception class `api/main.py` answers with a 4xx around an engine
    call is raised through `_surface_refusals` here and must come out as the
    SDK's anticipated-failure type, carrying its message. Asked of the wrapper
    rather than of `_REFUSALS`' spelling, so a class handled by its own
    `except` passes and a subclass of a listed one passes.
    """
    import asyncio

    from mcp.server.mcpserver.exceptions import ToolError

    from breakdown.mcp.server import _surface_refusals

    mapped = _exceptions_http_answers_with_a_4xx()
    assert {ValueError, RuntimeError} <= set(mapped), (
        f"the scan of api/main.py found only {sorted(c.__name__ for c in mapped)} "
        "mapped to a 4xx around an engine call; has the handler shape changed?"
    )

    opaque = {}
    for exc, where in mapped.items():

        @_surface_refusals
        async def tool(exc=exc):
            raise exc("narrow the window to whole months")

        try:
            asyncio.run(tool())
        except ToolError as e:
            assert "whole months" in str(e), f"{exc.__name__}'s message was lost: {e}"
        except exc:
            opaque[exc.__name__] = (
                f"api/main.py: {where}; over MCP it is a crash, and the caller "
                "gets `Error executing tool <name>` with the message withheld"
            )
    _settle(
        "mcp-refusals",
        opaque,
        "Exceptions HTTP answers with a 4xx and the engine's own message, which "
        "`@_surface_refusals` does not convert to `ToolError`. A model has no "
        "log to open: it cannot recover, explain or stop (roadmap C38):",
    )


def test_a_busy_engine_is_a_named_refusal_over_mcp(tmp_path, monkeypatch):
    """The other half of the guard: what the caller is told when it is held.

    Over HTTP a held guard is a 409 whose body says an earlier analysis is
    still finishing and to retry. `EngineBusy` is neither `ValueError` nor
    `RuntimeError`, so once the MCP tools take the guard (grill H7) it falls
    straight through `_surface_refusals` unless someone maps it — and the one
    caller with no log to read gets `Error executing tool run_rca` for a
    condition that clears itself in a minute.

    Driven rather than read: the guard is held, as an orphaned run holds it,
    and every tool is called. A tool that does not touch the engine answers as
    usual; one that does must fail *by name*. (Whether each tool takes the
    guard at all is the test above; while none does, nothing here can fail.)
    """
    with _DegenerateApp(tmp_path, monkeypatch) as served:
        assert served.state.engine_guard.acquire(blocking=False)
        try:
            # The guard on the guard: HTTP really is refusing right now.
            held = served.request(
                "POST", "/rca/{name}", {"path": {"name": "bookings"}, "params": _ANALYSIS}
            )
            assert held.status_code == 409, f"HTTP did not refuse on a held guard: {held.text}"
            crashed = {}
            for tool, calls in _MCP_PROBES.items():
                for arguments in calls:
                    result = served.call_tool(tool, arguments)
                    if not result["isError"]:
                        continue
                    text = result["content"][0]["text"]
                    if not text.removeprefix(f"Error executing tool {tool}").strip(" :"):
                        crashed[tool] = f"answered a held engine guard with {text!r}"
        finally:
            served.state.engine_guard.release()
    assert not crashed, (
        f"{crashed}: an MCP tool met the engine guard and crashed instead of "
        "refusing. Map `EngineBusy` to `ToolError` with its message, the way "
        "the HTTP layer maps it to a 409 (roadmap C41)."
    )


def test_no_route_hands_out_what_the_token_is_configured_to_protect(tmp_path, monkeypatch):
    """C31's redaction, asked of every route instead of the two that had it.

    `GET /dag` nulls `sql` and `bind` when `BREAKDOWN_API_TOKEN` is set and
    the caller does not present it; `GET /metrics/{name}/query` refuses under
    the same condition (C31, the second door). `GET /metrics/{name}` returned
    `metric.model_dump()` — relation, columns, filter logic — to anyone, a
    third door on the same finding (grill 2026-10-05 H6). And with a failing
    load, every data route's 503 and the `/trees` card carried the provider's
    error text, generated SQL and server paths included, where C43 had
    scrubbed `/health` only.

    Not a list of the routes that return a definition: every probed route is
    requested with the token configured and not presented, and no response
    may contain the tree's `sql`, its `bind`, or the text of its load error.
    A route added tomorrow that serializes a definition is covered the day
    its probe is added — and it cannot be added without one.

    The guard on the guard: with the token presented, `/dag` does carry both,
    so the sentinels are really in the tree and the check is not vacuous.
    """
    token = "s3cret-token"
    with _DegenerateApp(tmp_path, monkeypatch, env={"BREAKDOWN_API_TOKEN": token}) as served:
        authed = served.request(
            "GET", "/dag", {}, headers={"Authorization": f"Bearer {token}"}
        ).text
        assert _SENTINEL_SQL in authed and _SENTINEL_BIND in authed, (
            "the degenerate tree's `sql`/`bind` sentinels are not on `/dag` even "
            "with the token; the redaction check below would pass vacuously."
        )

        def leaks(sentinels):
            leaking = {}
            for (method, template), probes in _ROUTE_PROBES.items():
                for probe in probes:
                    text = served.request(method, template, probe).text
                    seen = [s for s in sentinels if s in text]
                    if seen:
                        leaking[f"{method} {template}"] = (
                            f"answers a caller with no token with {seen}"
                        )
            return leaking

        # The mock provider has no statement to show, so the route that exists
        # to show one is given one: the generated SQL a real provider would
        # hand back, which is the same `sql`/`bind` by another road.
        served.state.fetcher.query_provenance = lambda *args, **kwargs: (
            f"SELECT 1 FROM {_SENTINEL_QUERY}"
        )
        with_token = served.request(
            "GET",
            "/metrics/{name}/query",
            {"path": {"name": "sessions"}},
            headers={"Authorization": f"Bearer {token}"},
        ).text
        assert _SENTINEL_QUERY in with_token, (
            "the query route does not return the provider's statement even "
            "with the token; its check below would pass vacuously."
        )
        definition_leaks = leaks([_SENTINEL_SQL, _SENTINEL_BIND, _SENTINEL_QUERY])

        # A failed load: what `load_tree` records when the provider raises,
        # which is the exception's own text — for the duckdb provider, the
        # generated statement and the absolute path of the file it could not
        # read.
        secret = "SENTINEL_LOAD_ERROR /srv/private/orders.csv"
        served.state.load_error = f"IOException: {secret}"
        served.state.load_error_kind = "data_load_error"
        load_error_leaks = leaks([secret])

    _settle(
        "redaction-definition",
        definition_leaks,
        "Routes that return a metric's `sql` or `bind` when BREAKDOWN_API_TOKEN "
        "is set and the caller does not present it. `/dag` redacts both and "
        "`/metrics/{name}/query` refuses (roadmap C31); use the same redaction:",
    )
    _settle(
        "redaction-load-error",
        load_error_leaks,
        "Routes that return a failed load's raw exception text when "
        "BREAKDOWN_API_TOKEN is set and the caller does not present it. It can "
        "carry the tree's SQL and the server's paths; return the classified "
        "`load_error_kind`, as `/health` does (roadmap C43):",
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


def test_no_per_node_payload_carries_a_series(fitted_example):
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
    from breakdown.mcp.shaping import compact_rca

    # 32 is comfortably above every legitimate fixed-length array on a node
    # (five quantile levels, two interval bounds) and far below any window a
    # tree is worth fitting on — `MIN_FIT_PERIODS` alone is larger.
    cap = 32

    payloads = {"GET /metrics/{name}.diagnostics": fitted_example["metric"]["diagnostics"]}
    rca = fitted_example["rca"]
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


# --- The repository map names every module (grill 2026-10-05 L10) -------------


def test_every_module_is_named_in_the_repository_map():
    """`AGENTS.md`'s project-structure block is where a contributor — human or
    agent — learns what exists, and it went stale silently: `check.py`,
    `engine/stats.py`, `engine/windows.py` and `engine/warm.py` each shipped
    without a line, and `cli.py`'s still described two subcommands of three.
    A map that omits the module holding the shared bootstrap is how a fourth
    copy of the bootstrap gets written.

    Every `*.py` under `breakdown/` (bar `__init__.py`) has a line in the
    block, under its own directory, and no line names a module that is gone.
    """
    text = (PACKAGE.parent / "AGENTS.md").read_text()
    match = re.search(r"### Project structure\n+```\n(.*?)\n```", text, re.S)
    assert match, "AGENTS.md no longer has a fenced block under '### Project structure'"

    # Rebuild each entry's path from the block's two-space indentation.
    mapped, stack = set(), []
    for line in match.group(1).splitlines():
        entry = line.split("#")[0].rstrip()
        if not entry.strip():
            continue
        depth = (len(entry) - len(entry.lstrip())) // 2
        name = entry.strip()
        del stack[depth:]
        if name.endswith("/"):
            stack.append(name.rstrip("/"))
        else:
            mapped.add("/".join([*stack, name]))

    modules = {
        str(path.relative_to(PACKAGE.parent))
        for path in PACKAGE.rglob("*.py")
        if path.name != "__init__.py"
    }
    assert len(modules) >= 20, f"only {len(modules)} modules found; is PACKAGE right?"
    missing = sorted(modules - mapped)
    assert not missing, (
        f"{missing} are not in AGENTS.md's project-structure block. Add one line "
        "each, in the existing `name  # what it is` style (grill 2026-10-05 L10)."
    )
    gone = sorted(
        p for p in mapped if p.startswith("breakdown/") and p.endswith(".py") and p not in modules
    )
    assert not gone, f"{gone} are in AGENTS.md's project-structure block and no longer exist"
