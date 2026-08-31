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
_DBT_CHECKS = [
    "semantic manifest",
    "dbt profile",
    "warehouse connection",
    "tree metrics bind",
    "declared dimensions exist",
    "grain claims hold",
    "filters narrow",
    "entity grain resolves",
]


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
    from breakdown.data_fetch import WarehouseDataFetcher

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
            fetcher.fetch_metric(m.name, start_date, end_date, grain=m.grain, kind=m.kind)
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

    # 3. every tree metric resolves to a binding
    from breakdown.data_fetch import provider_query_name

    wanted = {provider_query_name("dbt", m): m.name for m in config.metrics if not m.derived}
    unbound = sorted(q for q in wanted if q not in bridged.bindings)
    if unbound:
        return stop(
            CheckResult.fail(
                "tree metrics bind",
                f"{len(unbound)} metric(s) not in the manifest: {unbound[:6]}"
                + (" …" if len(unbound) > 6 else ""),
                "The queried name is the last segment of `source`. Either add the "
                "metric to the dbt project, or give the node its own `bind:` block.",
            ),
            "unbound metrics",
        )
    # A filtered node is deliberately smaller than the metric a reader may have
    # in a dashboard under the same name, so the count belongs where a reader
    # looks rather than only in the generated SQL.
    filtered = sorted(q for q in wanted if bridged.bindings[q].where)
    results.append(
        CheckResult.ok(
            "tree metrics bind",
            f"{len(wanted)} metric(s) resolved"
            + (f", {len(filtered)} carry a filter" if filtered else ""),
        )
    )

    # 4. declared dimensions exist — otherwise the first slice click 500s
    missing = []
    for query_name, tree_name in wanted.items():
        declared = next((m.dimensions for m in config.metrics if m.name == tree_name), {})
        available = bridged.bindings[query_name].dimensions
        for dim_name, spec in declared.items():
            if spec.source not in available:
                missing.append(f"{tree_name}.{dim_name} -> '{spec.source}'")
    if missing:
        results.append(
            CheckResult.fail(
                "declared dimensions exist",
                f"{len(missing)} declared dimension(s) not on their binding: {missing[:5]}"
                + (" …" if len(missing) > 5 else ""),
                "A dimension's `source` must name one the binding exposes. "
                "Without this check the failure is a 500 on the first slice.",
            )
        )
    else:
        results.append(CheckResult.ok("declared dimensions exist", "all declared slices resolve"))

    # 5. the grain claim
    fanned, errors = [], []
    for query_name in sorted(wanted):
        try:
            rows, distinct = bridged.check_grain(
                query_name, start_date=start_date, end_date=end_date
            )
        except Exception as e:
            errors.append(f"{query_name}: {type(e).__name__}")
            continue
        if rows != distinct:
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

    results.append(_check_filters(bridged, wanted, filtered, start_date, end_date))
    results.append(_check_entity_grain(config, bridged, wanted, start_date, end_date))
    bridged.close()
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

        inner = build_fetcher(cfg, parser.dag, parser.config.metrics)
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
    from breakdown.data_fetch import provider_query_name
    from breakdown.engine.model import MIN_FIT_PERIODS
    from breakdown.loading import build_fetcher, wrap_snapshots

    cfg = parser.config.provider
    try:
        fetcher = build_fetcher(cfg, parser.dag, parser.config.metrics)
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
            df = fetcher.fetch_metric(query_name, start_date, end_date, grain=m.grain, kind=m.kind)
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


# What each provider's own checks would have reported, had its SDK been there.
# Listed so a missing extra reads as one fixable failure plus skips, instead of
# a cascade of connectivity failures with misleading remediations.
_DOWNSTREAM_CHECKS = {
    "warehouse": ["auth configured", "warehouse connection", "metric sql runs"],
    "cloud": ["cloud config", "semantic layer reachable", "tree metrics exist"],
    "local": ["dbt project", "metrics listable", "dbt provider migration"],
    "dbt": _DBT_CHECKS,
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
        results += check_dbt(tree.config, start_date, end_date)
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
    if provider not in ("mock", "none"):
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
