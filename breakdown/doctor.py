"""`breakdown doctor`: walk a tree's provider auth chain and say what's broken.

Each step is a CheckResult with copy-paste remediation. All checks run (a
failed prerequisite marks its dependents SKIP, not FAIL) so a partner sees
the whole picture in one run instead of peeling failures one restart at a
time. Connection logic is the real fetchers' — the doctor proves the same
code path the server will use, not a lookalike.
"""

import datetime
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Tuple

import yaml

from breakdown.engine.simulate import validate_cold_start
from breakdown.parser import _ENV_REF, MetricTreeConfig, Parser


@dataclass
class CheckResult:
    name: str
    status: Literal["pass", "fail", "skip", "warn"]
    detail: str = ""
    remediation: str = ""  # copy-paste command(s), possibly multiline

    @classmethod
    def ok(cls, name: str, detail: str = "") -> "CheckResult":
        return cls(name, "pass", detail)

    @classmethod
    def fail(cls, name: str, detail: str, remediation: str = "") -> "CheckResult":
        return cls(name, "fail", detail, remediation)

    @classmethod
    def skip(cls, name: str, detail: str = "") -> "CheckResult":
        return cls(name, "skip", detail)

    @classmethod
    def warn(cls, name: str, detail: str, remediation: str = "") -> "CheckResult":
        """Ran, did not fail, and is not clean.

        Added for `filters narrow`, which has a genuinely ambiguous middle
        answer: a predicate that excluded nothing over a seven-day probe window
        is either constant-true (C15's defect through a new door) or a real
        filter that happens to be vacuous this week. Failing would block a
        correct tree; passing silently is the "green `doctor` beside a wrong
        number" pattern this codebase keeps deciding against. Use it only where
        both of those are true — a warn nobody can act on is noise, and noise is
        how a real one gets scrolled past.
        """
        return cls(name, "warn", detail, remediation)


@dataclass
class _TreeCheck:
    results: List[CheckResult] = field(default_factory=list)
    config: Optional[MetricTreeConfig] = None
    parser: Optional[Parser] = None


def _check_tree(tree_path: str) -> _TreeCheck:
    out = _TreeCheck()

    if not os.path.isfile(tree_path):
        out.results.append(CheckResult.fail("tree file", f"not found: {tree_path}"))
        out.results.append(CheckResult.skip("tree parses"))
        return out
    out.results.append(CheckResult.ok("tree file", tree_path))

    try:
        with open(tree_path) as f:
            raw = yaml.safe_load(f.read())
    except yaml.YAMLError as e:
        out.results.append(CheckResult.fail("tree parses", f"not valid YAML: {e}"))
        return out
    if not isinstance(raw, dict):
        out.results.append(CheckResult.fail("tree parses", "YAML root must be a mapping"))
        return out

    # Report every unset ${VAR} in the provider block before the full parse,
    # which would abort on the first one with a Pydantic traceback.
    unset = sorted(
        var
        for value in (raw.get("provider") or {}).values()
        if isinstance(value, str)
        for var in _ENV_REF.findall(value)
        if var not in os.environ
    )
    if unset:
        out.results.append(
            CheckResult.fail(
                "provider env vars",
                f"referenced but not set: {', '.join(unset)}",
                "\n".join(f"export {var}=..." for var in unset),
            )
        )
        out.results.append(CheckResult.skip("tree parses", "unset env vars above"))
        return out
    out.results.append(CheckResult.ok("provider env vars", "all ${VAR} references resolve"))

    try:
        with open(tree_path) as f:
            out.parser = Parser(f.read())
        out.config = out.parser.config
    except Exception as e:
        out.results.append(CheckResult.fail("tree parses", str(e)))
        return out
    n = len(out.config.metrics)
    out.results.append(
        CheckResult.ok("tree parses", f"{n} metrics, provider '{out.config.provider.type}'")
    )
    out.results.append(_check_rate_denominators(out.parser))
    return out


def _check_rate_denominators(parser) -> CheckResult:
    """Which rates cannot be aggregated from their components (roadmap 1.11).

    A warning in the startup log is where this fact goes to be ignored, so
    `doctor` names the nodes: a rate with no `denominator` reports a window
    value that is the plain average of its per-period ratios, which is not what
    a window's rate is.

    **Three states, not two.** A rate that declares `no_denominator: "<why>"`
    has been *asked and answered* — it is not outstanding work, and the old
    message told its author to "Add `denominator: <metric>`", which for a median
    is impossible advice. So an answered rate passes, with its reason quoted:
    the point of that field is that the argument travels to the next reader,
    and this is one of the places the next reader is standing.

    The unanswered ones `fail`. The parser stays permissive — an unanswered
    rate must not take the whole tree down at load time, before anyone has
    seen a single value — but `doctor` is the trust gate, the thing every
    failure path in the product points users at, and *"can I trust this?"* is
    the question it is answering. A window value that is silently the wrong
    arithmetic is exactly what this project's four rules refuse to let a
    warning sit on.
    """
    rates = [m.name for m in parser.config.metrics if getattr(m, "kind", "flow") == "rate"]
    unanswered = list(getattr(parser, "rates_denominator_unanswered", []))
    answered = dict(getattr(parser, "rates_denominator_none", {}))
    if not rates:
        return CheckResult.skip("rate denominators", "no `kind: rate` metrics in this tree")
    # Quoted rather than counted: "3 rates declare `no_denominator`" is a number
    # nobody can check, and the reason is the whole content of the declaration.
    reasons = "".join(f"\n    {name}: {why}" for name, why in sorted(answered.items()))
    if not unanswered:
        declared = len(rates) - len(answered)
        if not answered:
            return CheckResult.ok(
                "rate denominators",
                f"all {len(rates)} rate(s) declare one — window values recompute from components",
            )
        return CheckResult.ok(
            "rate denominators",
            f"all {len(rates)} rate(s) answered — {declared} declare one (window values "
            f"recompute from components); {len(answered)} declare `no_denominator`, so their "
            f"window value is the average of the defined periods and there is no component "
            f"aggregate to compute:{reasons}",
        )
    shown = ", ".join(unanswered[:6]) + (" …" if len(unanswered) > 6 else "")
    remedy = (
        f"{len(unanswered)} of {len(rates)} rate(s) say nothing either way: {shown}. Their "
        "window values are the average of the per-period ratios, not "
        "Σnumerator / Σdenominator, and an undefined period cannot be told "
        "from a missing one. Add `denominator: <metric>` to each — or, where "
        'the rate genuinely has none, `no_denominator: "<why>"`, which records '
        "the reason instead of leaving the question open."
    )
    if answered:
        remedy += f" ({len(answered)} other rate(s) already answer with `no_denominator`:{reasons})"
    return CheckResult.fail("rate denominators", remedy)


# The `dbt` chain, in the order a failure actually cascades: the manifest has to
# exist before it can be read, the profile has to resolve before a connection can
# be opened, and nothing can be asserted about a metric that does not resolve to
# a binding.
#
# Everything after the connection is about the *bindings*, not about dbt, and
# is shared with the `duckdb` provider (`_check_bindings`): a tree's own
# `bind:` blocks over a folder of exports earn exactly the same grain claim,
# dimension check and filter probe as bindings imported from a manifest.
_BINDING_CHECKS = [
    "tree metrics bind",
    "declared dimensions exist",
    "grain claims hold",
    "filters narrow",
    "entity grain resolves",
    "metric sql runs",
]
_DBT_CHECKS = ["semantic manifest", "dbt profile", "warehouse connection", *_BINDING_CHECKS]
_DUCKDB_CHECKS = ["data files", *_BINDING_CHECKS]


def _over(start_date: Optional[str], end_date: Optional[str]) -> str:
    """Name the window a sampled check actually looked at.

    Bounding these to a probe window keeps `doctor` from full-scanning a large
    fact table twice per metric — but it turns proof into a sample, and absence
    over a few days is not absence. Saying which days were checked is what keeps
    the pass honest.
    """
    if not (start_date and end_date):
        return ""
    return f" (checked {start_date} → {end_date})"


def _skip_rest(names: List[str], reason: str) -> List[CheckResult]:
    return [CheckResult.skip(name, reason) for name in names]


def _nothing_to_check(
    name: str, what: str, start: Optional[str], end: Optional[str], explicit: bool
) -> CheckResult:
    """The result for a check whose every query came back with zero rows.

    Not a pass, whichever way it goes: `count(*) == count(distinct key)` holds
    over no rows, and a query that returns nothing "runs", so for a release a
    tree whose window missed its data altogether passed `grain claims hold`
    and `metric sql runs` and then served a healthy-looking tree of zeros
    (grill 2026-10-05 H3).

    Which non-pass depends on whose window it was. A window the operator
    *gave* (flags, or the BREAKDOWN_*_DATE pair the server reads) is the one
    the server will load, and it holds nothing — that is a failure. With no
    window given, doctor probed its own last seven days, and a tree over last
    year's export, or a monthly mart mid-month, is simply not in them: the
    check is skipped, saying so, with the command that would run it.
    """
    span = f"[{start}, {end}]" if start and end else "the relation"
    if explicit:
        return CheckResult.fail(
            name,
            f"{what} returned no rows over {span} — nothing was checked",
            "The window holds no data for this tree, so a server started on it "
            "would have nothing to load. Pass the dates your data actually covers:\n"
            "breakdown doctor --tree <tree> --start-date YYYY-MM-DD --end-date YYYY-MM-DD\n"
            "(and give `breakdown serve` the same pair).",
        )
    return CheckResult(
        name,
        "skip",
        f"{what} returned no rows over the default probe window {span} — nothing was checked",
        "No window was given, so doctor looked at the last 7 days only. Pass the "
        "dates your data covers to run this check:\n"
        "breakdown doctor --tree <tree> --start-date YYYY-MM-DD --end-date YYYY-MM-DD",
    )


class _counting_rows:
    """Count the rows each of a fetcher's queries returns, for one block.

    `fetch_metric` hands back the series *after* alignment, where an empty
    result and a quiet one are the same run of zeros, so the only place the
    difference still exists is the raw frame. Wrapping `_query` reads it there
    without a second round-trip per metric; a fetcher with no `_query` (a test
    double) yields no counts and the caller treats the result as unknown.
    """

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.counts: List[int] = []

    def __enter__(self) -> List[int]:
        inner = getattr(self.fetcher, "_query", None)
        if inner is not None:

            def counting(sql):
                df = inner(sql)
                self.counts.append(len(df))
                return df

            self.fetcher._query = counting
        return self.counts

    def __exit__(self, *exc) -> None:
        self.fetcher.__dict__.pop("_query", None)


def check_provider_extra(provider: str) -> Optional[CheckResult]:
    """Provider SDKs ship as extras, so "not installed" is a distinct failure
    from "installed and misconfigured". Report it first and by name — every
    downstream check would otherwise fail with the same ImportError wearing a
    connectivity check's remediation."""
    from breakdown.data_fetch import PROVIDER_EXTRAS, provider_extra_missing

    extra = PROVIDER_EXTRAS.get(provider)
    if extra is None:
        return None
    problem = provider_extra_missing(provider)
    if problem:
        return CheckResult.fail(
            f"{extra} extra installed",
            problem,
            f"pip install 'metric-breakdown[{extra}]'"
            + (
                "\nor, to keep MetricFlow out of this environment:"
                "\n  uv tool install dbt-metricflow"
                if extra == "dbt"
                else ""
            ),
        )
    return CheckResult.ok(f"{extra} extra installed", f"`{provider}` provider dependencies present")


def check_warehouse(config: MetricTreeConfig, start_date: str, end_date: str) -> List[CheckResult]:
    from breakdown.data_fetch import WarehouseDataFetcher, sparse_kw

    cfg = config.provider
    results: List[CheckResult] = []

    metric_sql = {m.name: m.sql for m in config.metrics if m.sql}
    # Derived nodes are computed from parents and never fetched, so they owe
    # no `sql` (roadmap 1.11a).
    missing_sql = [m.name for m in config.metrics if not m.sql and not m.derived]
    if missing_sql:
        results.append(
            CheckResult.fail(
                "per-metric sql",
                f"warehouse provider requires `sql` on every metric; missing for: {missing_sql}",
                "Add a `sql` block returning (date, value) to each listed metric.",
            )
        )

    try:
        fetcher = WarehouseDataFetcher(
            host=cfg.host,
            http_path=cfg.http_path,
            token=cfg.token,
            metric_sql=metric_sql,
            catalog=cfg.catalog,
            schema=cfg.db_schema,
            profile=cfg.profile,
        )
    except ValueError as e:
        results.append(
            CheckResult.fail(
                "auth configured",
                str(e),
                "Set `token: ${DATABRICKS_TOKEN}` or `profile: <name>` in the provider block.",
            )
        )
        results.extend(_skip_rest(["warehouse connection", "metric sql runs"], "no auth"))
        return results
    auth = f"profile '{cfg.profile}'" if cfg.profile else "token (PAT)"
    results.append(CheckResult.ok("auth configured", auth))

    if cfg.profile:
        if shutil.which("databricks") is None:
            # The SDK reads ~/.databrickscfg itself, but without the CLI the
            # partner cannot mint the OAuth session in the first place.
            results.append(
                CheckResult.fail(
                    "databricks CLI",
                    "`databricks` not found on PATH",
                    "brew install databricks   # or https://docs.databricks.com/dev-tools/cli/install",
                )
            )
        else:
            results.append(CheckResult.ok("databricks CLI", shutil.which("databricks")))
        try:
            from databricks.sdk.core import Config

            host = cfg.host or Config(profile=cfg.profile).host
            if not host:
                raise ValueError("profile resolved no host")
            results.append(CheckResult.ok("profile resolves", f"host {host}"))
        except Exception as e:
            results.append(
                CheckResult.fail(
                    "profile resolves",
                    f"profile '{cfg.profile}': {e}",
                    f"databricks auth login --host https://<workspace-host> --profile {cfg.profile}",
                )
            )
            results.extend(_skip_rest(["warehouse connection", "metric sql runs"], "no profile"))
            return results

    try:
        con = fetcher._connect()
        cur = con.cursor()
        try:
            cur.execute("SELECT 1")
            if cfg.catalog and cfg.db_schema:
                cur.execute(f"USE {cfg.catalog}.{cfg.db_schema}")
        finally:
            cur.close()
        fetcher._con = con  # metric probes reuse the proven connection
        where = f"{cfg.catalog}.{cfg.db_schema}" if cfg.catalog else "(no catalog set)"
        results.append(CheckResult.ok("warehouse connection", f"connected, USE {where}"))
    except Exception as e:
        results.append(
            CheckResult.fail(
                "warehouse connection",
                str(e),
                "Check `http_path` (SQL Warehouses -> your warehouse -> Connection details),\n"
                "that the warehouse is running or can auto-start, and that the token/profile\n"
                f"is valid: databricks auth login --profile {cfg.profile or '<name>'}",
            )
        )
        results.extend(_skip_rest(["metric sql runs"], "no connection"))
        return results

    failed = 0
    for m in config.metrics:
        if not m.sql:
            continue  # already reported under "per-metric sql"
        try:
            # The full fetch path: validates (date, value) columns, period
            # alignment, and gap rules — not just that the SQL executes.
            fetcher.fetch_metric(
                m.name, start_date, end_date, grain=m.grain, kind=m.kind, **sparse_kw(m.sparse)
            )
        except Exception as e:
            failed += 1
            results.append(CheckResult.fail(f"metric sql: {m.name}", str(e)))
    if not failed and metric_sql:
        results.append(
            CheckResult.ok(
                "metric sql runs",
                f"{len(metric_sql)} metrics over [{start_date}, {end_date}]",
            )
        )
    return results


def check_cloud(config: MetricTreeConfig) -> List[CheckResult]:
    from breakdown.data_fetch import CloudDataFetcher

    cfg = config.provider
    results: List[CheckResult] = []

    missing = [k for k in ("environment_id", "host", "token") if not getattr(cfg, k)]
    if missing:
        results.append(
            CheckResult.fail(
                "cloud config",
                f"provider is missing: {', '.join(missing)}",
                "environment_id: dbt Cloud -> Deploy -> Environments -> your prod environment URL.\n"
                "host: your account's cell-based Semantic Layer host, e.g.\n"
                "  hx123.semantic-layer.us1.dbt.com (NOT cloud.getdbt.com — find it under\n"
                "  Account settings -> Semantic Layer).\n"
                "token: a service token with Semantic Layer Only permissions, e.g. ${DBT_SL_TOKEN}.",
            )
        )
        results.extend(
            _skip_rest(["semantic layer reachable", "tree metrics exist"], "config incomplete")
        )
        return results
    results.append(
        CheckResult.ok("cloud config", f"environment {cfg.environment_id} at {cfg.host}")
    )

    try:
        client = CloudDataFetcher(
            environment_id=cfg.environment_id, host=cfg.host, token=cfg.token
        ).client
        # One call proves the whole chain: token valid -> host cell right ->
        # environment exists -> SL enabled -> service token mapped to an SL
        # credential. Each is a documented way a dbt Cloud SL setup half-works.
        with client.session():
            available = {m.name for m in client.metrics()}
        results.append(
            CheckResult.ok("semantic layer reachable", f"{len(available)} metrics listed")
        )
    except Exception as e:
        results.append(
            CheckResult.fail(
                "semantic layer reachable",
                str(e),
                "Walk the chain in dbt Cloud:\n"
                "  1. Host must be your cell-based SL host (Account settings -> Semantic Layer),\n"
                "     e.g. hx123.semantic-layer.us1.dbt.com — not cloud.getdbt.com.\n"
                "  2. The Semantic Layer must be enabled for the environment (plan-gated;\n"
                "     Team/Enterprise only, and credentials may be limited on lower plans).\n"
                "  3. The service token must be MAPPED to a Semantic Layer credential:\n"
                "     Account settings -> Semantic Layer -> Credentials -> add mapping.\n"
                "     An unmapped token authenticates but returns errors on query.",
            )
        )
        results.extend(_skip_rest(["tree metrics exist"], "semantic layer unreachable"))
        return results

    missing_metrics = sorted(
        {m.source.split(".")[-1] for m in config.metrics if m.source} - available
    )
    if missing_metrics:
        results.append(
            CheckResult.fail(
                "tree metrics exist",
                f"not in the semantic layer: {', '.join(missing_metrics)}",
                "Check each metric's `source` — its last segment must be a semantic-layer\n"
                "metric name — and that the environment has a successful production run.",
            )
        )
    else:
        results.append(CheckResult.ok("tree metrics exist", "every `source` matches an SL metric"))
    return results


def check_local(config: MetricTreeConfig) -> List[CheckResult]:
    cfg = config.provider
    results: List[CheckResult] = []

    if shutil.which("mf") is None:
        results.append(
            CheckResult.fail(
                "metricflow CLI",
                "`mf` not found on PATH",
                "pip install 'metric-breakdown[dbt]'   # or: uv tool install dbt-metricflow",
            )
        )
        results.extend(_skip_rest(["dbt project", "metrics listable"], "no mf CLI"))
        return results
    results.append(CheckResult.ok("metricflow CLI", shutil.which("mf")))

    project = cfg.project_path or ""
    if not os.path.isdir(project) or not os.path.isfile(os.path.join(project, "dbt_project.yml")):
        results.append(
            CheckResult.fail(
                "dbt project",
                f"no dbt_project.yml at project_path '{project}'",
                "Point `project_path` in the provider block at the dbt project root.",
            )
        )
        results.extend(_skip_rest(["metrics listable"], "no project"))
        return results
    results.append(CheckResult.ok("dbt project", project))

    try:
        proc = subprocess.run(
            ["mf", "list", "metrics"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        results.append(
            CheckResult.fail("metrics listable", "`mf list metrics` timed out after 120s")
        )
        return results
    if proc.returncode != 0:
        results.append(
            CheckResult.fail(
                "metrics listable",
                proc.stderr.strip() or proc.stdout.strip(),
                "Run `mf list metrics` in the project for the full error; usually a\n"
                "profiles.yml / warehouse-credentials problem.",
            )
        )
    else:
        results.append(CheckResult.ok("metrics listable", "`mf list metrics` succeeded"))
    results.append(_check_local_migration(config))
    return results


def _check_local_migration(config: MetricTreeConfig) -> CheckResult:
    """Whether *this* tree could move from `local` to the `dbt` provider.

    `local` is superseded for most trees (roadmap 2.13) but not all: it hands a
    metric name to MetricFlow, which plans the SQL, so it serves constructs the
    `dbt` provider refuses — cumulative metrics, offset windows, aggregations
    with no additive decomposition. Measured on two real projects, 2 of 24 and
    8 of 86 metrics fall in that gap.

    So this reports on the tree in front of it rather than asserting a general
    claim. A blanket deprecation warning would be noise for the author whose
    tree genuinely needs MetricFlow, and misleading for everyone if the general
    claim were taken at face value.
    """
    name = "dbt provider migration"
    project = config.provider.project_path or ""
    try:
        from breakdown.dbt_bridge import bridge_project, manifest_path
    except Exception:
        return CheckResult.skip(name, "the dbt-bridge extra is not installed")

    if not os.path.exists(manifest_path(project)):
        return CheckResult.skip(
            name,
            "no semantic manifest yet — run `dbt parse` in the project to check "
            "whether this tree can move to the `dbt` provider",
        )
    try:
        # Generic dialect deliberately: this runs for the `local` provider,
        # whose project need not have a resolvable warehouse profile, and the
        # question is whether these metrics *translate* at all. Filter
        # resolution is dialect-independent except for the final parse, so a
        # generic read is the conservative answer to that question.
        bridged = bridge_project(project)
    except Exception as e:
        return CheckResult.skip(name, f"could not read the semantic manifest: {e}")

    servable = set(bridged.bindings) | set(bridged.formulas)
    reasons = {s.name: s.reason for s in bridged.skipped}
    # Derived nodes (no `source`) are computed from their parents and never
    # fetched, so they neither need nor can have a manifest entry.
    fetched = [m for m in config.metrics if m.source]
    blocked = [
        (m.source.split(".")[-1], m.name)
        for m in fetched
        if m.source.split(".")[-1] not in servable
    ]
    if not blocked:
        return CheckResult.ok(
            name,
            f"all {len(fetched)} fetched metric(s) translate — this tree can move to "
            "`provider: {type: dbt}` and drop the `mf` binary",
        )
    detail = ", ".join(
        f"{tree} ({reasons.get(q, 'not in the semantic manifest')[:60]})" for q, tree in blocked[:3]
    )
    return CheckResult.skip(
        name,
        f"{len(blocked)} of {len(fetched)} fetched metric(s) need MetricFlow: {detail}"
        + (" …" if len(blocked) > 3 else "")
        + ". Stay on `local` for these, or express them with a node-level `bind:` "
        "block and move the rest.",
    )


def check_dbt(
    config: MetricTreeConfig,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    explicit_window: Optional[bool] = None,
) -> List[CheckResult]:
    """Walk the `dbt` provider's chain: manifest -> profile -> connection ->
    bindings -> dimensions -> grain claims.

    The last one is the check no other semantic layer can make. MetricFlow and
    Cube accept a declared relationship on trust, so a relation that is not one
    row per grain silently multiplies every aggregate over it. Owning the
    binding contract is what makes it assertable, and asserting it here is what
    turns it from a wrong number into a startup error.
    """
    from breakdown.dbt_bridge import manifest_path
    from breakdown.dbt_provider import (
        DbtProfileError,
        connect_from_profile,
        fetcher_from_project,
        resolve_profile,
    )

    cfg = config.provider
    results: List[CheckResult] = []
    project = cfg.project_path or ""
    remaining = list(_DBT_CHECKS)

    def stop(result: CheckResult, reason: str) -> List[CheckResult]:
        results.append(result)
        results.extend(_skip_rest(remaining[remaining.index(result.name) + 1 :], reason))
        return results

    # 1. the manifest
    if not os.path.isdir(project) or not os.path.isfile(os.path.join(project, "dbt_project.yml")):
        return stop(
            CheckResult.fail(
                "semantic manifest",
                f"no dbt_project.yml at project_path '{project}'",
                "Point `project_path` in the provider block at the dbt project root.",
            ),
            "no dbt project",
        )
    path = manifest_path(project)
    if not os.path.exists(path):
        return stop(
            CheckResult.fail(
                "semantic manifest",
                f"no semantic manifest at {path}",
                "cd " + project + " && dbt parse"
                "\n\nIf you are on dbt Fusion / dbt Core v2, note it does not write"
                "\nthis file at all for projects still using the legacy"
                "\n`semantic_models:` spec — those must migrate to the new metrics"
                "\nspec first.",
            ),
            "no semantic manifest",
        )
    try:
        # Overrides matter here as much as at runtime: a node's own `bind:`
        # block replaces what the manifest declares, so checking without them
        # validates a binding the server will never use. Mirrors loading.build_fetcher.
        from breakdown.data_fetch import provider_query_name

        overrides = {
            provider_query_name("dbt", m): m.bind
            for m in config.metrics
            if m.bind and not m.derived
        }
        bridged = fetcher_from_project(
            project,
            target=cfg.target,
            profiles_dir=cfg.profiles_dir,
            overrides=overrides,
        )
    except DbtProfileError as e:
        results.append(CheckResult.ok("semantic manifest", path))
        return stop(
            CheckResult.fail(
                "dbt profile",
                str(e),
                "Check the project's `profile:` and your profiles.yml target. "
                "`target:` and `profiles_dir:` in the provider block override "
                "what dbt would pick.",
            ),
            "profile unresolved",
        )
    except Exception as e:
        return stop(
            CheckResult.fail("semantic manifest", f"could not read {path}: {e}"),
            "manifest unreadable",
        )
    results.append(
        CheckResult.ok("semantic manifest", f"{len(bridged.bindings)} metrics bound from {path}")
    )

    out = resolve_profile(project, target=cfg.target, profiles_dir=cfg.profiles_dir)
    results.append(
        CheckResult.ok(
            "dbt profile",
            f"target '{out.get('_target')}' -> {out.get('type')} "
            f"(sqlglot dialect '{bridged.dialect or 'generic'}')",
        )
    )

    # 2. the connection — the project's own credentials, never a new one
    try:
        connect_from_profile(out).close()
    except Exception as e:
        return stop(
            CheckResult.fail(
                "warehouse connection",
                f"{type(e).__name__}: {e}",
                "The connection comes from the dbt project's own profiles.yml, "
                "so `dbt debug` in that project tests the same credentials.",
            ),
            "no connection",
        )
    results.append(CheckResult.ok("warehouse connection", f"{out.get('type')} reachable"))

    # 3–8. the bindings themselves — shared with the `duckdb` provider
    results.extend(_check_bindings(config, bridged, "dbt", start_date, end_date, explicit_window))
    bridged.close()
    return results


def check_duckdb(
    parser,
    tree_path: str,
    start_date: str,
    end_date: str,
    explicit_window: Optional[bool] = None,
) -> List[CheckResult]:
    """Walk the `duckdb` provider's chain: data files -> the binding checks.

    Short by design. There is no manifest, profile or credential to prove, so
    the only step of its own is the folder: does it exist, what relations does
    it hold, and from which files. After that the tree's `bind:` blocks are
    checked by exactly the code that checks a dbt project's — the grain claim
    in particular, since a hand-exported CSV is at least as likely to carry a
    duplicate `order_id` as a modelled fact table.

    The fetcher comes from `build_fetcher` with the tree path, the same call
    `load_tree` makes, so the folder this reports on is the one the server
    would read (`resolve_data_dir` runs once, there).
    """
    from breakdown.dbt_provider import list_data_files
    from breakdown.loading import build_fetcher, resolve_data_dir

    config = parser.config
    results: List[CheckResult] = []
    data_dir = resolve_data_dir(config.provider.data_dir, tree_path)
    try:
        tables = list_data_files(data_dir)
    except Exception as e:
        results.append(
            CheckResult.fail(
                "data files",
                str(e),
                "Point `data_dir` at the folder holding your .csv / .parquet exports "
                "(a relative path resolves against the tree file's directory), one "
                "file per relation, named by the stem the bindings refer to.",
            )
        )
        results.extend(_skip_rest(_BINDING_CHECKS, "no data files"))
        return results
    listed = ", ".join(f"{stem} ({name})" for stem, name in tables.items())
    results.append(CheckResult.ok("data files", f"{data_dir}: {listed}"))

    try:
        fetcher = build_fetcher(config.provider, parser.dag, config.metrics, tree_path=tree_path)
    except Exception as e:  # pragma: no cover - the parser already refused an unbound node
        results.append(CheckResult.fail("tree metrics bind", f"could not build fetcher: {e}"))
        results.extend(_skip_rest(_BINDING_CHECKS[1:], "no fetcher"))
        return results
    results.extend(
        _check_bindings(config, fetcher, "duckdb", start_date, end_date, explicit_window)
    )
    fetcher.close()
    return results


def _check_bindings(
    config: MetricTreeConfig,
    fetcher,
    provider_type: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    explicit_window: Optional[bool] = None,
) -> List[CheckResult]:
    """The checks a binding earns once there is a connection to run them on:
    tree metrics bind -> declared dimensions exist -> grain claims hold ->
    filters narrow -> entity grain resolves -> metric sql runs.

    One function for both providers that serve `bind:` blocks (`dbt` and
    `duckdb`). It was the back half of `check_dbt`, and copying it for the
    on-ramp would have been the four rules' founding defect: the grain claim
    asserted for a manifest binding and taken on trust for a hand-written one.
    `fetcher` is any `DbtDataFetcher`; `provider_type` decides which name a
    tree metric is looked up by (`provider_query_name`).

    `explicit_window` says whether the dates are the operator's own (flags or
    the environment pair) rather than doctor's seven-day default; it decides
    whether a check that found no rows fails or is skipped
    (`_nothing_to_check`). `run_doctor` passes it. Left None, a pair of dates
    counts as explicit and their absence does not.
    """
    from breakdown.data_fetch import provider_query_name, sparse_kw

    results: List[CheckResult] = []
    remaining = list(_BINDING_CHECKS)
    dated = start_date is not None and end_date is not None
    probe_start, probe_end = _probe_window(start_date, end_date)
    probe_explicit = dated if explicit_window is None else explicit_window
    # The grain claim with no dates at all runs over the whole relation, and
    # a relation with no rows anywhere is not a window that missed.
    grain_explicit = True if not dated else probe_explicit

    def stop(result: CheckResult, reason: str) -> List[CheckResult]:
        results.append(result)
        results.extend(_skip_rest(remaining[remaining.index(result.name) + 1 :], reason))
        return results

    # every tree metric resolves to a binding
    wanted = {
        provider_query_name(provider_type, m): m.name for m in config.metrics if not m.derived
    }
    unbound = sorted(q for q in wanted if q not in fetcher.bindings)
    if unbound:
        return stop(
            CheckResult.fail(
                "tree metrics bind",
                f"{len(unbound)} metric(s) have no binding: {unbound[:6]}"
                + (" …" if len(unbound) > 6 else ""),
                "The queried name is the last segment of `source`. Either add the "
                "metric to the dbt project, or give the node its own `bind:` block.",
            ),
            "unbound metrics",
        )
    # A filtered node is deliberately smaller than the metric a reader may have
    # in a dashboard under the same name, so the count belongs where a reader
    # looks rather than only in the generated SQL.
    filtered = sorted(q for q in wanted if fetcher.bindings[q].where)
    results.append(
        CheckResult.ok(
            "tree metrics bind",
            f"{len(wanted)} metric(s) resolved"
            + (f", {len(filtered)} carry a filter" if filtered else ""),
        )
    )

    # declared dimensions exist — otherwise the first slice click fails.
    #
    # Two halves, and for a release only the first was checked: that the
    # dimension's `source` is a *key* on the binding, and that the sliced query
    # it produces actually *runs*. A binding dimension with `column: regionn`
    # has the key and no such column, so it passed here, passed `metric sql
    # runs` (which fetches the unsliced series) and failed on the first
    # `POST /rca/{name}/slices` (grill 2026-10-05 M7). So each declared
    # dimension now gets one sliced fetch over the probe window, through the
    # same `fetch_metric_sliced` the server calls — which also catches a
    # dimension shape the generator refuses to compile (`UnsupportedBinding`).
    missing, unrunnable, ran = [], [], 0
    for query_name, tree_name in wanted.items():
        m = next((m for m in config.metrics if m.name == tree_name), None)
        available = fetcher.bindings[query_name].dimensions
        for dim_name, spec in (m.dimensions if m else {}).items():
            where = f"{tree_name}.{dim_name} -> '{spec.source}'"
            if spec.source not in available:
                missing.append(where)
                continue
            try:
                fetcher.fetch_metric_sliced(
                    query_name, spec.source, probe_start, probe_end, grain=m.grain, kind=m.kind
                )
                ran += 1
            except Exception as e:
                unrunnable.append(f"{where} ({type(e).__name__}: {_first_lines(str(e), 2)})")
    if missing or unrunnable:
        parts = []
        if missing:
            parts.append(
                f"{len(missing)} declared dimension(s) not on their binding: {missing[:5]}"
                + (" …" if len(missing) > 5 else "")
            )
        if unrunnable:
            parts.append(
                f"{len(unrunnable)} declared dimension(s) whose sliced query does not run: "
                + "; ".join(unrunnable[:4])
                + (" …" if len(unrunnable) > 4 else "")
            )
        remedies = []
        if missing:
            remedies.append("A dimension's `source` must name one the binding exposes.")
        if unrunnable:
            remedies.append(
                "The binding exposes the dimension but the source refused the query "
                "grouped by it: check that `bind.dimensions.<source>.column` names a "
                "real column on the relation (for a `duckdb` tree, a header in the "
                "file), spelled as the source spells it."
            )
        remedies.append("Without this check the failure arrives on the first slice.")
        results.append(
            CheckResult.fail("declared dimensions exist", "; ".join(parts), " ".join(remedies))
        )
    else:
        results.append(
            CheckResult.ok(
                "declared dimensions exist",
                "all declared slices resolve"
                + (f", {ran} sliced query(ies) ran" + _over(probe_start, probe_end) if ran else ""),
            )
        )

    # the grain claim
    fanned, errors, no_rows = [], [], []
    for query_name in sorted(wanted):
        try:
            rows, distinct = fetcher.check_grain(
                query_name, start_date=start_date, end_date=end_date
            )
        except Exception as e:
            errors.append(f"{query_name}: {type(e).__name__}")
            continue
        if rows == 0:
            # `0 == 0` is not a grain claim that held; it is one that was
            # never put to anything.
            no_rows.append(query_name)
        elif rows != distinct:
            fanned.append(f"{query_name} ({rows:,} rows / {distinct:,} distinct)")
    if fanned:
        results.append(
            CheckResult.fail(
                "grain claims hold",
                f"{len(fanned)} relation(s) are not one row per grain_key: {fanned[:4]}"
                + (" …" if len(fanned) > 4 else ""),
                "Every aggregate over such a relation is silently multiplied. "
                "Fix the model so it is one row per grain, or bind the node to a "
                "`bind.sql` relation that already is.",
            )
        )
    elif errors:
        results.append(CheckResult.fail("grain claims hold", f"could not check: {errors[:4]}"))
    elif wanted and len(no_rows) == len(wanted):
        results.append(
            _nothing_to_check(
                "grain claims hold",
                f"all {len(wanted)} relation(s)",
                start_date,
                end_date,
                grain_explicit,
            )
        )
    elif no_rows:
        # Some relations had rows and held; the others were not checked, and
        # the line says which rather than counting them in.
        results.append(
            CheckResult.warn(
                "grain claims hold",
                f"{len(wanted) - len(no_rows)} relation(s) one row per grain; "
                f"{len(no_rows)} returned no rows and were not checked: {no_rows[:6]}"
                + (" …" if len(no_rows) > 6 else "")
                + _over(start_date, end_date),
                "A metric with no rows in the window loads as an all-zero (or "
                "undefined) series. Either the window misses its data — widen it "
                "with --start-date/--end-date — or the relation or its filter is wrong.",
            )
        )
    else:
        # The assertion runs over the rows the node actually aggregates, so a
        # filtered relation is asserted *under its filter* (2.17 §3.4). Saying
        # so is what keeps the pass honest: it reads as "one row per grain over
        # these filtered rows", not "checked".
        results.append(
            CheckResult.ok(
                "grain claims hold",
                f"{len(wanted)} relation(s) one row per grain"
                + (f", {len(filtered)} under a filter" if filtered else "")
                + _over(start_date, end_date),
            )
        )

    results.append(_check_filters(fetcher, wanted, filtered, start_date, end_date))
    results.append(_check_entity_grain(config, fetcher, wanted, start_date, end_date))

    # every metric's generated query runs, through the full fetch path — the
    # grain claim selects only the key, so a misspelt `measure` or a
    # `time_column` the dialect cannot cast survives every check above it.
    failed, returned_nothing = [], []
    for query_name, tree_name in sorted(wanted.items()):
        m = next(m for m in config.metrics if m.name == tree_name)
        try:
            with _counting_rows(fetcher) as counts:
                fetcher.fetch_metric(
                    query_name,
                    probe_start,
                    probe_end,
                    grain=m.grain,
                    kind=m.kind,
                    **sparse_kw(m.sparse),
                )
            # The aligned frame cannot say this (an empty flow is filled to
            # zeros across the window); the raw one can.
            if counts and sum(counts) == 0:
                returned_nothing.append(tree_name)
        except Exception as e:
            failed.append(f"{tree_name}: {_first_lines(str(e), 2)}")
    if failed:
        results.append(
            CheckResult.fail(
                "metric sql runs",
                f"{len(failed)} metric(s) failed over [{probe_start}, {probe_end}]: "
                + "; ".join(failed[:4])
                + (" …" if len(failed) > 4 else ""),
                "`GET /metrics/{name}/query` shows the generated statement once the "
                "server is up; until then the binding's `measure`, `time_column` and "
                "`numerator`/`denominator` are the columns to check.",
            )
        )
    elif wanted and len(returned_nothing) == len(wanted):
        # Every statement compiled and executed, which does prove the columns
        # exist — and every one came back empty, which is the load the server
        # would make. Not a pass.
        results.append(
            _nothing_to_check(
                "metric sql runs",
                f"all {len(wanted)} metric quer(ies) ran and",
                probe_start,
                probe_end,
                probe_explicit,
            )
        )
    elif returned_nothing:
        results.append(
            CheckResult.warn(
                "metric sql runs",
                f"{len(wanted)} metric(s) ran over [{probe_start}, {probe_end}]; "
                f"{len(returned_nothing)} returned no rows: {returned_nothing[:6]}"
                + (" …" if len(returned_nothing) > 6 else ""),
                "A metric with no rows in the window loads as an all-zero (or "
                "undefined) series, which RCA will read as a real collapse. Widen "
                "the window with --start-date/--end-date if it misses the data; "
                "otherwise check the metric's relation, `time_column` and filter.",
            )
        )
    else:
        results.append(
            CheckResult.ok(
                "metric sql runs", f"{len(wanted)} metric(s) over [{probe_start}, {probe_end}]"
            )
        )
    return results


def _check_filters(fetcher, wanted, filtered, start_date=None, end_date=None) -> CheckResult:
    """Whether each imported filter actually excludes some rows and not all.

    Deliberately shaped like the grain claim, and a differentiator for the same
    reason: **it checks the data instead of trusting the metadata.** MetricFlow
    does not do this either. It converts the whole class of silently-no-op and
    silently-everything-drops predicates from a wrong number into a startup
    result — the class C15 punished.

    What it cannot do is prove our row set is MetricFlow's. `kept/rows = 0.31`
    says the filter is doing something; it does not say it is doing the right
    thing. That is roadmap 2.14, and this check raises its priority rather than
    substituting for it.
    """
    if not filtered:
        return CheckResult.skip("filters narrow", "no imported filters on this tree")

    empty, vacuous, errors, live = [], [], [], []
    for query_name in filtered:
        tree_name = wanted[query_name]
        try:
            rows, kept = fetcher.check_filter(query_name, start_date=start_date, end_date=end_date)
        except Exception as e:
            predicate = "; ".join(fetcher.bindings[query_name].where)
            errors.append(f"{tree_name} ({type(e).__name__}: `{predicate}`)")
            continue
        if rows == 0:
            # Nothing in the window at all says nothing about the predicate.
            continue
        if kept == 0:
            empty.append(f"{tree_name} (0 of {rows:,} rows)")
        elif kept == rows:
            vacuous.append(f"{tree_name} ({rows:,} of {rows:,} rows)")
        else:
            live.append(f"{tree_name} ({kept:,}/{rows:,})")

    if empty or errors:
        detail = ", ".join(empty + errors)
        return CheckResult.fail(
            "filters narrow",
            f"{len(empty) + len(errors)} filter(s) exclude every row or cannot run: "
            f"{detail[:200]}" + (" …" if len(detail) > 200 else ""),
            "This node would serve an empty or all-zero series. It is the "
            "signature of a dialect-hostile predicate — `= TRUE` against a "
            "VARCHAR column, a date literal parsed as an identifier, a boolean "
            "stored as 'Y'. Check the predicate in the generated SQL (`show "
            "query` in the UI, or GET /metrics/{name}/query).",
        )
    if vacuous:
        return CheckResult.warn(
            "filters narrow",
            f"{len(vacuous)} filter(s) excluded nothing: {', '.join(vacuous[:4])}"
            + (" …" if len(vacuous) > 4 else "")
            + _over(start_date, end_date),
            "Either genuinely vacuous over this window — widen it with "
            "--start-date/--end-date and re-run — or the predicate evaluates "
            "constant-true, which is the dropped-filter defect (C15) arriving "
            "through a new door.",
        )
    if not live:
        return CheckResult.skip(
            "filters narrow",
            f"no rows in the probe window to check {len(filtered)} filter(s) against"
            + _over(start_date, end_date),
        )
    return CheckResult.ok(
        "filters narrow",
        f"{len(live)} filter(s) keep some rows and drop others: "
        f"{', '.join(live[:4])}" + (" …" if len(live) > 4 else "") + _over(start_date, end_date),
    )


def _check_entity_grain(config, fetcher, wanted, start_date=None, end_date=None) -> CheckResult:
    """Whether declared slices actually need resolution, and whether the ones
    that assert they do not are telling the truth.

    A dimension that is multi-valued for some entity inside a period makes the
    slices overstate the metric. `resolve: first|last` fixes it; `resolve:
    error` asserts it never happens, and this is what holds that assertion to
    account. Without the check the answer arrives as a wrong number on the
    first *slice by* click — the too-late failure class of C12.
    """
    from breakdown.dbt_sql import build_multivalue_assertion

    offenders, unresolved, unchecked, checked = [], [], [], 0
    for query_name, tree_name in wanted.items():
        bind = fetcher.bindings.get(query_name)
        if bind is None or not bind.is_non_additive:
            continue
        defn = next((m for m in config.metrics if m.name == tree_name), None)
        for dim_name, spec in (defn.dimensions if defn else {}).items():
            if spec.source not in bind.dimensions:
                continue  # already reported by the dimension check
            checked += 1
            try:
                sql = build_multivalue_assertion(
                    bind,
                    dimension=spec.source,
                    grain=defn.grain,
                    dialect=fetcher.dialect,
                    start_date=start_date,
                    end_date=end_date,
                )
                pairs = int(fetcher._query(sql).iloc[0]["multivalued_pairs"])
            except Exception as e:
                # A check that could not run is not a violated assertion: this
                # used to land in `offenders` and print as "`resolve: error` is
                # asserted but violated" with a remedy telling the author to
                # abandon an assertion nothing had tested — for a binding that
                # may not even declare one.
                unchecked.append(f"{tree_name}.{dim_name} ({type(e).__name__}: {e})")
                continue
            if not pairs:
                continue
            where = f"{tree_name}.{dim_name} ({pairs:,} multivalued entity-periods)"
            if bind.entity_grain is None:
                unresolved.append(where)
            elif bind.entity_grain.resolve == "error":
                offenders.append(where)

    if not checked:
        return CheckResult.skip("entity grain resolves", "no non-additive metrics declared")
    if offenders:
        return CheckResult.fail(
            "entity grain resolves",
            f"`resolve: error` is asserted but violated: {offenders[:4]}"
            + (" …" if len(offenders) > 4 else ""),
            "Either the data is not single-valued after all — switch to "
            "`resolve: first` or `last`, which answer different business "
            "questions — or fix the source so one entity holds one value per "
            "period.",
        )
    if unresolved:
        return CheckResult.fail(
            "entity grain resolves",
            f"slices overstate the metric and no `entity_grain` is declared: "
            f"{unresolved[:4]}" + (" …" if len(unresolved) > 4 else ""),
            "Add `entity_grain: {resolve: first|last}` to the binding to make "
            "the slices sum exactly. Without it they are reported as "
            "overlapping and contribution shares are withheld.",
        )
    if unchecked:
        return CheckResult.fail(
            "entity grain resolves",
            f"could not be checked: {unchecked[:4]}" + (" …" if len(unchecked) > 4 else ""),
            "The multivalue assertion query failed against the warehouse — fix "
            "the error above and re-run; until it runs, whether these slices "
            "sum exactly is unverified.",
        )
    return CheckResult.ok(
        "entity grain resolves",
        f"{checked} non-additive slice(s) resolve to one value per period"
        f"{_over(start_date, end_date)}",
    )


def check_snapshots(
    parser, tree_path: str, start_date: str, end_date: str, explicit_window: bool
) -> Tuple[Optional[CheckResult], bool]:
    """What the snapshot store can serve — and whether it covers the tree.

    `doctor` used to build the raw provider and never look at snapshots at
    all, while the server reads through them first (roadmap 2.20): on a
    snapshot-served deployment — the mode the README and the demo both ship —
    that meant a hard [FAIL] against a box answering perfectly, from the one
    command every failure path in the product points a user at.

    Returns (result, covered): `covered` is True only when every sourced
    metric has a definition-matched snapshot spanning the checked window,
    which is what lets `run_doctor` downgrade provider-chain failures from
    fatal to warnings. Coverage here is an inventory claim (a file spans the
    window); the end-to-end proof is fit readiness, which fetches through the
    same wrapped path the server uses.
    """
    from breakdown.data_fetch import provider_query_name
    from breakdown.snapshots import SnapshotStore, definition_sha, resolve_snapshot_dir

    cfg = parser.config.provider
    snapshot_dir = resolve_snapshot_dir(tree_path)
    if snapshot_dir is None:
        return CheckResult.skip("snapshots", "disabled (BREAKDOWN_SNAPSHOT_DIR=off)"), False
    if not os.path.isdir(snapshot_dir):
        return (
            CheckResult.skip(
                "snapshots", f"none yet at {snapshot_dir} — the first server run writes them"
            ),
            False,
        )

    store = SnapshotStore(snapshot_dir)
    # The definition fingerprint needs the provider, but the provider may be
    # exactly what is broken on a snapshot-only box — so its absence must cost
    # verification of the sha, never the inventory. `None` serves legacy-style
    # (with the store's own warning), which mirrors what the server would do.
    try:
        # From the loading pipeline, not the web app (roadmap C-grill M9):
        # this import used to pull FastAPI into a CLI connectivity check,
        # with a comment apologising for it.
        from breakdown.loading import build_fetcher

        inner = build_fetcher(cfg, parser.dag, parser.config.metrics, tree_path=tree_path)
    except Exception:
        inner = None

    sourced = [m for m in parser.config.metrics if not m.derived]
    covered, missing = [], []
    for m in sourced:
        query_name = provider_query_name(cfg.type, m)
        sha = definition_sha(inner, query_name) if inner is not None else None
        window = store.covering_window(query_name, start_date, end_date, m.grain, m.kind, sha)
        (covered if window else missing).append(m.name)

    span = f"[{start_date}, {end_date}]"
    if not explicit_window:
        span += " (7-day probe window — pass --start-date/--end-date for the real one)"
    if missing:
        preview = ", ".join(missing[:5]) + (", ..." if len(missing) > 5 else "")
        return (
            CheckResult.ok(
                "snapshots",
                f"{len(covered)} of {len(sourced)} sourced metrics covered for {span} "
                f"at {snapshot_dir}; not covered: {preview}",
            ),
            False,
        )
    return (
        CheckResult.ok(
            "snapshots",
            f"all {len(sourced)} sourced metrics covered for {span} at {snapshot_dir}",
        ),
        True,
    )


def check_fit_readiness(
    parser, tree_path: str, start_date: str, end_date: str
) -> List[CheckResult]:
    """Per-metric whole periods over the window vs the fit minimum — the
    graduation check for a tree migrating from cold start to fitted mode.
    Fetches through the real server path (never a lookalike) — which since
    roadmap 2.20 includes the snapshot read-through wrapper, so a
    snapshot-served metric passes here exactly as it serves there. A second
    result reports **history headroom**: whether the provider has history
    before --start-date (RCA trains on everything loaded, so an earlier start
    strengthens fits and default reference windows)."""
    from breakdown.data_fetch import provider_query_name, sparse_kw
    from breakdown.engine.model import MIN_FIT_PERIODS
    from breakdown.loading import build_fetcher, wrap_snapshots

    cfg = parser.config.provider
    try:
        fetcher = build_fetcher(cfg, parser.dag, parser.config.metrics, tree_path=tree_path)
        fetcher = wrap_snapshots(fetcher, cfg.type, tree_path, slice_span=(start_date, end_date))
    except Exception as e:
        return [CheckResult.fail("fit readiness", f"could not build fetcher: {e}")]

    lines, short = [], []
    headroom = []  # (metric, earliest) where history exists before start_date
    for m in parser.config.metrics:
        if m.derived:
            lines.append(f"{m.name}: derived from parents — nothing to fetch")
            continue
        query_name = provider_query_name(cfg.type, m)
        earliest = fetcher.earliest_date(query_name, m.grain)
        if earliest is not None and earliest < start_date:
            headroom.append((m.name, earliest))
        try:
            df = fetcher.fetch_metric(
                query_name, start_date, end_date, grain=m.grain, kind=m.kind, **sparse_kw(m.sparse)
            )
            n = len(df)
        except Exception as e:
            lines.append(f"{m.name}: fetch failed ({e})")
            short.append(m.name)
            continue
        ready = n >= MIN_FIT_PERIODS
        lines.append(
            f"{m.name}: {n}/{MIN_FIT_PERIODS} whole {m.grain} periods{'' if ready else ' — not fittable yet'}"
        )
        if not ready:
            short.append(m.name)

    detail = "; ".join(lines)
    if short:
        readiness = CheckResult.fail(
            "fit readiness",
            detail,
            f"Metrics below {MIN_FIT_PERIODS} periods cannot be fitted: "
            f"{', '.join(short)}. Widen --start-date/--end-date to cover more "
            "history, or wait for it to accumulate — what-if and RCA fit on "
            "demand and will fail on these nodes until then.",
        )
    else:
        readiness = CheckResult.ok("fit readiness", detail)

    if headroom:
        oldest = min(e for _, e in headroom)
        history = CheckResult.ok(
            "history headroom",
            f"history exists before --start-date for {len(headroom)} metric(s) "
            f"(earliest {oldest}); breakdown trains on everything loaded, so an "
            "earlier --start-date strengthens fits and default reference windows",
        )
    else:
        history = CheckResult.ok(
            "history headroom",
            "no history before --start-date detected (or the provider can't say)",
        )
    return [readiness, history]


_toolchain_memo: Optional[CheckResult] = None

_MACOS_TOOLCHAIN_FIX = (
    "# xcode-select can insist the tools are installed while their C++ headers are\n"
    "# missing (/Library/Developer/CommandLineTools/usr/include/c++/v1 absent).\n"
    "# A plain clang++ test compile passes via the SDK path; pytensor's does not.\n"
    "sudo rm -rf /Library/Developer/CommandLineTools\n"
    "xcode-select --install"
)


def check_inference_toolchain() -> CheckResult:
    """Compile and run a trivial gradient through pytensor's own C path.

    Issue #115: on macOS a broken Command Line Tools install makes every NUTS
    fit die inside pytensor's C compile with `fatal error: 'vector' file not
    found`, while `xcode-select --install` insists the tools are present and a
    plain `clang++` test compile succeeds through the SDK path. `doctor`
    passed, `serve` started, and the failure surfaced on the first fit. Only
    pytensor's own compile path reveals it, so that is what this runs:
    `FAST_RUN` on a squared-sum gradient, the smallest graph that exercises
    the C backend end to end. About two seconds cold, then cached by
    pytensor's compiledir.

    Memoized per process: the doctor runs once, and the test suite calls
    `run_doctor` dozens of times.
    """
    global _toolchain_memo
    if _toolchain_memo is None:
        _toolchain_memo = _probe_inference_toolchain()
    return _toolchain_memo


def _probe_inference_toolchain() -> CheckResult:
    import platform
    import time

    name = "inference compiler"
    started = time.perf_counter()
    try:
        import numpy as np
        import pytensor
        import pytensor.tensor as pt
    except Exception as e:  # the inference stack itself is broken
        return CheckResult.fail(
            name,
            f"pytensor could not be imported, so no fit can run: {_first_lines(str(e))}",
            "pip install --force-reinstall metric-breakdown",
        )
    cxx = pytensor.config.cxx
    if not cxx:
        # Nothing to compile through: pytensor already chose its Python
        # backend. Correct, slow, and worth saying rather than passing green.
        return CheckResult.warn(
            name,
            "no C++ compiler detected (pytensor `cxx` is empty); fits run on the "
            "Python backend, which is correct but many times slower",
            "# install g++ or clang++ so pytensor can compile its graphs\n"
            "sudo apt-get install -y g++      # Debian/Ubuntu\n"
            "xcode-select --install           # macOS",
        )
    try:
        x = pt.dvector("x")
        probe = pytensor.function([x], pytensor.grad((x**2).sum(), x), mode="FAST_RUN")
        out = np.asarray(probe(np.array([1.0, 2.0])))
        if not np.allclose(out, [2.0, 4.0]):
            raise RuntimeError(f"probe returned {out.tolist()}, expected [2.0, 4.0]")
    except Exception as e:  # pytensor raises CompileError, OSError, or its own wrappers
        if platform.system() == "Darwin":
            remedy = _MACOS_TOOLCHAIN_FIX
        else:
            remedy = (
                "# install a C++ compiler pytensor can find, then re-run doctor\n"
                "sudo apt-get install -y g++      # Debian/Ubuntu\n"
                "conda install -c conda-forge gxx  # conda"
            )
        return CheckResult.fail(
            name,
            "pytensor could not compile and run a trivial gradient, so every NUTS fit "
            f"will fail the same way: {_first_lines(str(e))}",
            remedy,
        )
    elapsed = time.perf_counter() - started
    return CheckResult.ok(name, f"pytensor compiled and ran a gradient via {cxx} in {elapsed:.1f}s")


def _first_lines(text: str, limit: int = 3) -> str:
    """The lines of a compiler error a reader can act on: the ones that say
    `error`, else the first few. pytensor appends the whole Apply node dump."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    errors = [ln for ln in lines if "error" in ln.lower()]
    picked = (errors or lines)[:limit]
    return " | ".join(picked) if picked else "(no message)"


# What each provider's own checks would have reported, had its SDK been there.
# Listed so a missing extra reads as one fixable failure plus skips, instead of
# a cascade of connectivity failures with misleading remediations.
_DOWNSTREAM_CHECKS = {
    "warehouse": ["auth configured", "warehouse connection", "metric sql runs"],
    "cloud": ["cloud config", "semantic layer reachable", "tree metrics exist"],
    "local": ["dbt project", "metrics listable", "dbt provider migration"],
    "dbt": _DBT_CHECKS,
    "duckdb": _DUCKDB_CHECKS,
}


def run_doctor(
    tree_path: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[CheckResult]:
    # "Explicit" includes the BREAKDOWN_START_DATE/BREAKDOWN_END_DATE pair the
    # server itself reads: a deployment that sets its window in the environment
    # (the demo image does) has told doctor the real window as surely as flags.
    explicit_window = (start_date is not None and end_date is not None) or (
        "BREAKDOWN_START_DATE" in os.environ and "BREAKDOWN_END_DATE" in os.environ
    )
    start_date, end_date = _probe_window(start_date, end_date)

    tree = _check_tree(tree_path)
    results = tree.results
    if tree.config is None:
        return results

    provider = tree.config.provider.type
    # Everything appended from here to the snapshots check is the provider
    # chain — the set a full snapshot covering can downgrade (2.20). Tree
    # checks above it are never downgraded: a broken tree is broken however
    # the data arrives.
    provider_section = len(results)
    extra = check_provider_extra(provider)
    if extra is not None:
        results.append(extra)

    if extra is not None and extra.status == "fail":
        results += _skip_rest(_DOWNSTREAM_CHECKS[provider], "provider extra not installed")
    elif provider == "warehouse":
        results += check_warehouse(tree.config, start_date, end_date)
    elif provider == "cloud":
        results += check_cloud(tree.config)
    elif provider == "local":
        results += check_local(tree.config)
    elif provider == "dbt":
        results += check_dbt(tree.config, start_date, end_date, explicit_window)
    elif provider == "duckdb":
        results += check_duckdb(tree.parser, tree_path, start_date, end_date, explicit_window)
    elif provider == "none":
        # Cold-start tree: no connection to prove — readiness means every
        # belief the what-if engine needs is declared. Same check the server
        # runs at startup.
        problems = validate_cold_start(tree.parser.dag)
        if problems:
            results.append(
                CheckResult.fail(
                    "cold-start declarations",
                    f"{len(problems)} missing: " + "; ".join(problems),
                    "Declare `baseline` on every non-formula metric and an "
                    "explicit prior on every probabilistic edge (see README "
                    "'Cold-start mode').",
                )
            )
        else:
            results.append(
                CheckResult.ok(
                    "cold-start declarations",
                    "no data provider — every baseline and edge prior is declared",
                )
            )
    else:
        results.append(CheckResult.ok("mock provider", "nothing to check — data is synthetic"))

    # The snapshot store: what it can serve, and — when it covers the whole
    # tree for the checked window — permission to survive a dead provider.
    # A snapshot-served deployment is a mode the docs recommend, and doctor
    # failing hard against a healthy one was 2.20's second half.
    covered = False
    # `duckdb` is never wrapped (`loading.wrap_snapshots` says why: the files
    # are the artifact), so reporting a snapshot store for it would describe a
    # cache the server does not read.
    if provider not in ("mock", "none", "duckdb"):
        snap_result, covered = check_snapshots(
            tree.parser, tree_path, start_date, end_date, explicit_window
        )
        if snap_result is not None:
            results.append(snap_result)
        if covered:
            for i in range(provider_section, len(results)):
                r = results[i]
                if r.status == "fail":
                    results[i] = CheckResult.warn(
                        r.name,
                        r.detail + " — not fatal here: every metric is snapshot-covered for the "
                        "checked window, so the server will serve. Fix this before "
                        "refetching (BREAKDOWN_REFRESH=1) or widening the window.",
                        r.remediation,
                    )

    # Fit readiness: periods-per-metric vs the fit minimum. Only meaningful
    # over the tree's real analysis window (the default probe window is a
    # deliberately tiny 7 days), so it runs when the window is explicit —
    # flags or the BREAKDOWN_*_DATE pair the server reads.
    if provider == "none":
        results.append(
            CheckResult.skip("fit readiness", "cold-start tree — nothing is ever fitted")
        )
    elif not explicit_window:
        results.append(
            CheckResult.skip(
                "fit readiness",
                "pass --start-date/--end-date covering your data window to "
                "check per-metric history against the fit minimum",
            )
        )
    elif any(r.status == "fail" for r in results):
        results.append(CheckResult.skip("fit readiness", "provider checks failed above"))
    else:
        results += check_fit_readiness(tree.parser, tree_path, start_date, end_date)

    # The machine, not the data: can pytensor compile at all here? Last, and
    # never downgraded by snapshot coverage — a snapshot-served deployment
    # still has to fit. Issue #115 is a green doctor beside a dead sampler.
    if provider == "none":
        results.append(
            CheckResult.skip("inference compiler", "cold-start tree — nothing is ever fitted")
        )
    else:
        results.append(check_inference_toolchain())
    return results


def _probe_window(start_date: Optional[str], end_date: Optional[str]) -> Tuple[str, str]:
    """A small recent window: live-warehouse probes should scan days, not the
    tree's whole (possibly multi-year) analysis window.

    Flags win; the BREAKDOWN_START_DATE/BREAKDOWN_END_DATE pair the server
    reads is next, so a deployment configured by environment (the demo image)
    is checked over the window it will actually load; the 7-day probe is last.
    The server's jaffle-shop *defaults* are deliberately not used here — a
    probe against someone else's demo window would be worse than a small
    recent one."""
    start_date = start_date or os.environ.get("BREAKDOWN_START_DATE")
    end_date = end_date or os.environ.get("BREAKDOWN_END_DATE")
    for label, value in (("--start-date", start_date), ("--end-date", end_date)):
        if value:
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise SystemExit(f"{label} must be a valid YYYY-MM-DD date, got '{value}'")
    today = datetime.date.today()
    return (
        start_date or str(today - datetime.timedelta(days=7)),
        end_date or str(today),
    )


_TAGS = {"pass": "[PASS]", "fail": "[FAIL]", "skip": "[SKIP]", "warn": "[WARN]"}


def print_report(results: List[CheckResult]) -> int:
    for r in results:
        line = f"{_TAGS[r.status]} {r.name}"
        if r.detail:
            line += f" — {r.detail}"
        print(line)
        if r.remediation:
            for rem_line in r.remediation.splitlines():
                print(f"       {rem_line}")
    counts = {s: sum(1 for r in results if r.status == s) for s in _TAGS}
    summary = f"\n{counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped"
    if counts["warn"]:
        summary += f", {counts['warn']} warned"
    print(summary)
    # A warning is not a failure: the exit code gates CI and a deploy, and a
    # filter that is vacuous over a seven-day probe window must not stop either.
    return 1 if counts["fail"] else 0
