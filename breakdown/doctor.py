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
    status: Literal["pass", "fail", "skip"]
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
    return out


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
    missing_sql = [m.name for m in config.metrics if not m.sql]
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

    missing_metrics = sorted({m.source.split(".")[-1] for m in config.metrics} - available)
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
    return results


def check_fit_readiness(parser, start_date: str, end_date: str) -> CheckResult:
    """Per-metric whole periods over the window vs the fit minimum — the
    graduation check for a tree migrating from cold start to fitted mode.
    Fetches through the real provider path (never a lookalike), so it also
    exercises every metric's query end to end."""
    from breakdown.api.main import _build_fetcher  # lazy: pulls FastAPI
    from breakdown.data_fetch import provider_query_name
    from breakdown.engine.model import MIN_FIT_PERIODS

    cfg = parser.config.provider
    try:
        fetcher = _build_fetcher(cfg, parser.dag, parser.config.metrics)
    except Exception as e:
        return CheckResult.fail("fit readiness", f"could not build fetcher: {e}")

    lines, short = [], []
    for m in parser.config.metrics:
        query_name = provider_query_name(cfg.type, m)
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
        return CheckResult.fail(
            "fit readiness",
            detail,
            f"Metrics below {MIN_FIT_PERIODS} periods cannot be fitted: "
            f"{', '.join(short)}. Widen --start-date/--end-date to cover more "
            "history, or wait for it to accumulate — what-if and RCA fit on "
            "demand and will fail on these nodes until then.",
        )
    return CheckResult.ok("fit readiness", detail)


# What each provider's own checks would have reported, had its SDK been there.
# Listed so a missing extra reads as one fixable failure plus skips, instead of
# a cascade of connectivity failures with misleading remediations.
_DOWNSTREAM_CHECKS = {
    "warehouse": ["auth configured", "warehouse connection", "metric sql runs"],
    "cloud": ["cloud config", "semantic layer reachable", "tree metrics exist"],
    "local": ["dbt project", "metrics listable"],
}


def run_doctor(
    tree_path: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> List[CheckResult]:
    explicit_window = start_date is not None and end_date is not None
    start_date, end_date = _probe_window(start_date, end_date)

    tree = _check_tree(tree_path)
    results = tree.results
    if tree.config is None:
        return results

    provider = tree.config.provider.type
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

    # Fit readiness: periods-per-metric vs the fit minimum. Only meaningful
    # over the tree's real analysis window (the default probe window is a
    # deliberately tiny 7 days), so it runs when both dates are explicit.
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
        results.append(check_fit_readiness(tree.parser, start_date, end_date))
    return results


def _probe_window(start_date: Optional[str], end_date: Optional[str]) -> Tuple[str, str]:
    """A small recent window: live-warehouse probes should scan days, not the
    tree's whole (possibly multi-year) analysis window."""
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


_TAGS = {"pass": "[PASS]", "fail": "[FAIL]", "skip": "[SKIP]"}


def print_report(results: List[CheckResult]) -> int:
    for r in results:
        line = f"{_TAGS[r.status]} {r.name}"
        if r.detail:
            line += f" — {r.detail}"
        print(line)
        if r.remediation:
            for rem_line in r.remediation.splitlines():
                print(f"       {rem_line}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("pass", "fail", "skip")}
    print(f"\n{counts['pass']} passed, {counts['fail']} failed, {counts['skip']} skipped")
    return 1 if counts["fail"] else 0
