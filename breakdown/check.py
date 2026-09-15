"""`breakdown check`: would `serve` accept this tree? Answered without serving.

Issue #117: a production tree upgraded 0.1.0 → 0.2.0 was refused at load by
C12's slice-weight grain rule — a correct, well-worded refusal, found by
restarting the production server, which is the wrong place to find it.
`doctor` was not the answer either: it is a connectivity tool, takes one file,
and its exit code says whether the *provider* answers, not whether the tree
would boot.

This runs exactly the refusals `serve` runs before it touches a provider, in
the order it runs them, through the same functions, so the message printed
here is the message the server log would carry:

1. **discovery** — `discover_trees`: the path exists; a directory holds at
   least one `*.yml`.
2. **parse** — `parse_tree`: YAML, the Pydantic schema, every DAG rule
   (duplicate names, formula references, edge grains, C12's dimension-weight
   grain, cold-start prior shapes, …), and `${VAR}` resolution.
3. **default** — `resolve_default`: `--default-tree` names a discovered tree.
4. **pre-fetch load** — the checks `load_tree` makes before its first fetch:
   the provider's extra is installed, a `warehouse` tree has `sql` on every
   fetched metric, a cold-start tree declares every belief `validate_cold_start`
   needs.

**What it cannot see**, and says so rather than implying otherwise: anything
that needs data. Window coverage, a short series bounding the analyses that
read it (#112), identity checks on fetched formula nodes, fit readiness — all of
that is the load itself, and `doctor --start-date … --end-date …` is the
tool that runs it against the real provider. `check` exiting 0 means the tree
will *parse and start*, not that it will serve every window.

Deliberately not `doctor --offline`: `doctor` reports on one file and treats
an unanswered rate denominator as a failure (it is the trust gate), while
`serve` warns and starts. Folding this in would either change `doctor`'s exit
contract or make `check` refuse what `serve` accepts. Two commands, two
questions: *will it start* and *can I trust the data*.
"""

import logging
import os
from typing import Dict, List, Optional

from breakdown.api.trees import TreeState, discover_trees, parse_tree, resolve_default
from breakdown.doctor import CheckResult, _check_rate_denominators


def _pre_fetch_load_error(tree: TreeState) -> Optional[str]:
    """The reason `load_tree` would set `load_error` *before* fetching, or
    None. Mirrors `api/main.py:load_tree` up to the first provider call —
    keep the two in step: a refusal added there belongs here too."""
    from breakdown.data_fetch import provider_extra_missing

    provider_cfg = tree.parser.config.provider
    if provider_cfg.type == "none":
        from breakdown.engine.simulate import validate_cold_start

        problems = validate_cold_start(tree.parser.dag)
        if problems:
            return "tree declares no data provider but is not cold-start ready: " + "; ".join(
                problems
            )
        return None
    # Serve hits this as an ImportError from `build_fetcher`; the non-raising
    # form carries the same `pip install` hint without importing the SDK.
    missing = provider_extra_missing(provider_cfg.type)
    if missing:
        return missing
    if provider_cfg.type == "warehouse":
        # The one `build_fetcher` refusal that is about the tree rather than
        # the environment; quoted from `loading.build_fetcher`.
        absent = [m.name for m in tree.parser.config.metrics if not m.sql and not m.derived]
        if absent:
            return f"warehouse provider requires `sql` on every metric; missing for: {absent}"
    return None


def _summary(tree: TreeState) -> str:
    cfg = tree.parser.config
    grains = sorted({m.grain for m in cfg.metrics}, key=("day", "week", "month").index)
    line = f"{len(cfg.metrics)} metrics, grain {'/'.join(grains)}, provider '{cfg.provider.type}'"
    # A declaration this command can see without data: which metrics will
    # have absent periods filled by their own statement at load (#112). The
    # count of periods actually filled needs the fetch and is on `/meta`.
    sparse = [m.name for m in cfg.metrics if m.sparse]
    if sparse:
        line += f", {len(sparse)} sparse ({', '.join(sparse)})"
    return line


def run_check(path: str, default_tree: Optional[str] = None) -> List[CheckResult]:
    results: List[CheckResult] = []
    try:
        trees: Dict[str, TreeState] = discover_trees(os.path.abspath(path))
    except Exception as e:
        results.append(CheckResult.fail("tree discovery", f"{type(e).__name__}: {e}"))
        return results

    # `parse_tree` is failure-soft (records `load_error`, never raises) and
    # logs "it will show as errored", which is true of a server and not of
    # this command; the report line below carries the same text once.
    trees_log = logging.getLogger("breakdown.api.trees")
    level = trees_log.level
    trees_log.setLevel(logging.CRITICAL)
    try:
        for tree in trees.values():
            parse_tree(tree)
    finally:
        trees_log.setLevel(level)

    for tree_id, tree in trees.items():
        if tree.parser is None:
            results.append(CheckResult.fail(f"tree '{tree_id}'", tree.load_error or "parse failed"))
            continue
        problem = _pre_fetch_load_error(tree)
        if problem:
            results.append(
                CheckResult.fail(
                    f"tree '{tree_id}'",
                    f"parses, but serve would refuse it at load: {problem}",
                )
            )
            continue
        results.append(CheckResult.ok(f"tree '{tree_id}'", _summary(tree)))
        # Serve starts with a warning here; doctor fails. This command sits
        # with serve on the exit code and with doctor on saying it out loud.
        rates = _check_rate_denominators(tree.parser)
        if rates.status == "fail":
            results.append(
                CheckResult.warn(
                    f"tree '{tree_id}' rate denominators",
                    rates.detail + " (serve starts; `breakdown doctor` fails this)",
                )
            )

    if default_tree is not None or len(trees) > 1:
        try:
            chosen = resolve_default(trees, default_tree)
            results.append(
                CheckResult.ok(
                    "default tree",
                    f"'{chosen}'" + ("" if default_tree else " (alphabetically first)"),
                )
            )
        except RuntimeError as e:
            results.append(CheckResult.fail("default tree", str(e)))
    return results
