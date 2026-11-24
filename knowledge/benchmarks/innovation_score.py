"""Prototype: rank root causes by each node's own *innovation*, not by its share
of a window-over-window gap.

Design and findings: `knowledge/innovation_rca_design.md`. This is a
measurement script, not engine code — it reuses the engine's fits
(`fit_rca_node`) and data plumbing unchanged and adds only the scoring.

What it computes, per node in the target's ancestor scope:

1. **The node's own window.** The target's analysis window, shifted back by
   the lag on every edge between the node and the target (the engine's
   `parent_windows`, composed along the path).
2. **A fit that ends where that window starts.** Not at the target's
   `analysis_start`: a lagged ancestor's window precedes it, and a fit that
   ends at `analysis_start` has trained on the periods it is asked to judge.
3. **The innovation.** `actual − predicted`, per period, where `predicted` is
   the fitted model's posterior predictive *given the parents' actual values*:
   `alpha + seasonal + forward-simulated level + beta_raw · parents + noise`.
   A source node's prediction is its forecast; a formula node's is the
   identity plus its (usually zero) forecast residual. Summarized as `z` —
   the window-mean innovation over the predictive sd of that mean — and a
   two-sided predictive tail probability.
4. **Its contribution to the target.** Remove that one node's innovation
   (set the node to what was predicted), push the change down through the
   structural equations — formulas exactly, learned edges through the
   posterior-mean `beta_raw`, every other node keeping its own innovation —
   and read how far the target moves. `departure` is the same with every
   innovation removed: actual minus what the whole tree expected.

Then it scores the ranking against the planted root cause, beside the two
rankings available today: the engine's `ranked_causes` and the naive "largest
relative change among the ancestors".

Usage (needs the demo snapshots — `make -C demo snapshots`):
    uv run python knowledge/benchmarks/innovation_score.py            # every story
    uv run python knowledge/benchmarks/innovation_score.py A B-onset  # some

Null calibration (design doc §6.1) — every node over every anomaly-free
window, ~500 fits and ~40 minutes, resumable from the CSV it appends to:
    uv run python knowledge/benchmarks/innovation_score.py null out.csv
    uv run python knowledge/benchmarks/innovation_score.py null-summary out.csv

Restricting the null run, and forcing an observation model per node without
editing the tree (roadmap S20). `--likelihood` may repeat; a node fitted under
two likelihoods lands in the same CSV as two sets of rows, told apart by the
`likelihood` column, so one file holds the comparison:
    uv run python knowledge/benchmarks/innovation_score.py null out.csv \
        --nodes reactivations,sessions --horizons 7,28
    uv run python knowledge/benchmarks/innovation_score.py null out.csv \
        --nodes reactivations,sessions --horizons 7,28 \
        --likelihood reactivations=negative_binomial \
        --likelihood sessions=negative_binomial

Trend experiments (roadmap S8), also without editing the tree. `--trend
level|local_linear` forces that trend kind on every scored node; `--trend role`
applies the role rule instead (a slope on nodes with no parents that are flows
or stocks, a level elsewhere). `--slope-sigma X` sets the slope innovation's
prior scale (`0` = fixed drift). The planted roots are always scored. One CSV
per arm, then compare them:
    ... null arm4.csv --nodes a,b --horizons 7,28 --likelihood a=negative_binomial \
        --trend role
    ... null-compare gauss_level=arm1.csv declared_slope=arm4.csv

A slope arm takes several times as long as the level arm. `--shard k/n` scores
every n-th job, starting at the k-th, into its own CSV, so n processes split an
arm; `null-compare` takes `name=a.csv,b.csv` and concatenates (the first file
wins where two hold the same window, so an arm that refits only some nodes is
`name=refit.csv,baseline.csv`). `--stride n` keeps every n-th weekly start.

A second company (design doc §6.4). `--company alpenglow` (or
`BENCH_COMPANY=alpenglow`) points every mode at Alpenglow Supply Co., the
retail vertical, instead of White Cube. Its clean windows are not written
down here: they are derived from the planted-anomaly key
(`<slug>.ground_truth.json`, which `python demo/fetch_demo_data.py alpenglow`
downloads to `demo/.data/alpenglow/`). `describe` prints the per-node table
that says which claims the company can test, and `windows` the derived gaps:
    ... --company alpenglow describe
    ... --company alpenglow windows
    ... --company alpenglow null out.csv --horizons 7,28
Its series are committed snapshots (`demo/.breakdown/alpenglow_snapshots`),
so the duckdb is not read. The `dbt` provider still has to be constructed,
and unlike `local` it reads its project to do that, so a fake_companies
checkout is needed for `dbt/retail_dtc` (its `dbt_project.yml`, `profiles.yml`
and `target/semantic_manifest.json`): beside this repo, as `demo/Makefile`
assumes, or named in `ALPENGLOW_DBT_PROJECT`. `demo/alpenglow_tree.yml` says
how to refetch the snapshots. `<COMPANY>_TREE`, `<COMPANY>_SNAPSHOT_DIR` and
`<COMPANY>_GROUND_TRUTH` in the environment override where each is read from.
"""

import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
DEMO = os.path.join(REPO, "demo")


def _company_from_argv():
    """`--company X` has to be read before breakdown is imported, because the
    tree and window reach the app through the environment."""
    for i, a in enumerate(sys.argv):
        if a == "--company" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--company="):
            return a.split("=", 1)[1]
    return os.environ.get("BENCH_COMPANY", "white_cube")


# What differs between companies and is not in the ground-truth file: where
# the tree and its data live, and the window the tree is loaded over. `env`
# is a default, not an override: a path already in the environment wins, so a
# first Alpenglow load can name the real dbt project.
COMPANIES = {
    "white_cube": dict(
        tree=os.path.join(DEMO, "white_cube_tree.yml"),
        start="2024-06-01",
        end="2026-07-30",
        snapshots=os.path.join(DEMO, ".breakdown", "snapshots"),
        ground_truth=os.path.join(DEMO, ".data", "white_cube", "white_cube.ground_truth.json"),
        env={"WHITE_CUBE_DBT_PROJECT": "/nonexistent/white-cube-has-no-provider"},
    ),
    "alpenglow": dict(
        tree=os.path.join(DEMO, "alpenglow_tree.yml"),
        start="2024-06-01",
        end="2026-05-31",
        snapshots=os.path.join(DEMO, ".breakdown", "alpenglow_snapshots"),
        ground_truth=os.path.join(DEMO, ".data", "alpenglow", "alpenglow.ground_truth.json"),
        env={
            "ALPENGLOW_DBT_PROJECT": os.path.join(
                os.path.dirname(REPO), "fake_companies", "dbt", "retail_dtc"
            )
        },
        profiles_from="ALPENGLOW_DBT_PROJECT",
    ),
}
COMPANY = _company_from_argv()
if COMPANY not in COMPANIES:
    raise SystemExit(f"unknown company {COMPANY!r}; known: {sorted(COMPANIES)}")
CONFIG = COMPANIES[COMPANY]
_PREFIX = COMPANY.upper()
os.environ.update(
    BREAKDOWN_TREE=os.environ.get(f"{_PREFIX}_TREE", CONFIG["tree"]),
    BREAKDOWN_START_DATE=CONFIG["start"],
    BREAKDOWN_END_DATE=CONFIG["end"],
    BREAKDOWN_SNAPSHOT_DIR=os.environ.get(f"{_PREFIX}_SNAPSHOT_DIR", CONFIG["snapshots"]),
)
for _name, _value in CONFIG["env"].items():
    os.environ.setdefault(_name, _value)
if "profiles_from" in CONFIG:  # the project's own profiles.yml, not ~/.dbt
    os.environ["DBT_PROFILES_DIR"] = os.environ[CONFIG["profiles_from"]]
GROUND_TRUTH = os.environ.get(f"{_PREFIX}_GROUND_TRUTH", CONFIG["ground_truth"])
os.environ.pop("BREAKDOWN_WARM", None)

import networkx as nx  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The scoring itself lives in the evaluation runner's package, so the two
# cannot drift; this script keeps the hard-coded companies and its own modes.
from evalrunner.scoring import (  # noqa: E402, F401
    MAX_DRAWS,
    SEED,
    _pit_ks,
    defn_of,
    force_likelihoods,
    force_trend,
    innovation,
    node_role,
    node_windows,
    propagate,
    rank_of,
    takes_slope_by_role,
)

from breakdown.engine.rca import run_rca  # noqa: E402

# The planted anomalies, from fake_companies' white_cube.ground_truth.json
# (demo/demos.yaml). `truth` is where each one *enters* this tree: the first
# tree metric in the anomaly's `affected_metrics`. The "tour" windows are the
# ones knowledge/demo_guided_tour.md walks a prospect through; the "-onset"
# variants start the analysis window on the anomaly's own first period, which
# is the window a monitor would hand over.
WHITE_CUBE_STORIES = {
    "A": dict(
        target="new_mrr",
        ana=("2026-02-09", "2026-03-08"),
        ref=("2026-01-05", "2026-02-01"),
        truth={"visit_signup_rate"},  # mobile_signup_cta_regression, from 2026-02-02
    ),
    "A-onset": dict(
        target="signups",
        ana=("2026-02-02", "2026-03-01"),
        ref=None,
        truth={"visit_signup_rate"},
    ),
    "B": dict(
        target="net_new_mrr",
        ana=("2026-05-11", "2026-06-07"),
        ref=("2026-03-16", "2026-04-12"),
        truth={"customer_churn_rate"},  # professional_churn_spike, from 2026-04-27
    ),
    "B-onset": dict(
        target="net_new_mrr",
        ana=("2026-04-27", "2026-05-24"),
        ref=None,
        truth={"customer_churn_rate"},
    ),
    "C": dict(
        target="signups",
        ana=("2025-03-10", "2025-04-06"),
        ref=("2025-02-03", "2025-03-02"),
        # latam_campaign_spend_ramp + latam_campaign_brazil_lift_*, from 2025-03-03
        truth={"marketing_spend", "visit_signup_rate"},
    ),
    "C-onset": dict(
        target="signups",
        ana=("2025-03-03", "2025-03-30"),
        ref=None,
        truth={"marketing_spend", "visit_signup_rate"},
    ),
    "D": dict(
        target="new_mrr",
        ana=("2025-08-11", "2025-09-07"),
        ref=("2025-07-07", "2025-08-03"),
        truth={"trial_activation_rate", "trial_days_active"},  # onboarding_revamp, 2025-08-04
    ),
    "D-onset": dict(
        target="trial_conversion_rate",
        ana=("2025-08-04", "2025-08-31"),
        ref=None,
        truth={"trial_activation_rate", "trial_days_active"},
    ),
}

# Alpenglow's planted anomalies (alpenglow.ground_truth.json), each over its
# own span from its first period. `truth` is the first metric of
# demo/alpenglow_tree.yml in the anomaly's `affected_metrics`; the two demand
# anomalies enter the tree twice, through `units` and through `aov`.
ALPENGLOW_STORIES = {
    "checkout": dict(
        target="gross_revenue",
        ana=("2024-09-12", "2024-09-23"),
        ref=None,
        truth={"conversion_rate"},  # mobile_checkout_bug
    ),
    "recall": dict(
        target="net_revenue",
        ana=("2024-10-07", "2024-11-10"),
        ref=None,
        truth={"return_rate"},  # jackets_sizing_recall
    ),
    "budget": dict(
        target="gross_revenue",
        ana=("2025-02-03", "2025-02-23"),
        ref=None,
        truth={"marketing_spend"},  # paid_social_budget_cut
    ),
    "basket": dict(
        target="net_revenue",
        ana=("2025-04-07", "2025-04-20"),
        ref=None,
        truth={"units", "aov"},  # surprise level shift in items_per_order
    ),
    "tees": dict(
        target="net_revenue",
        ana=("2025-05-05", "2025-05-18"),
        ref=None,
        truth={"units", "aov"},  # tees_demand_slump
    ),
}
STORIES = {"white_cube": WHITE_CUBE_STORIES, "alpenglow": ALPENGLOW_STORIES}[COMPANY]


def load():
    from fastapi.testclient import TestClient

    from breakdown.api.main import app

    client = TestClient(app)
    client.__enter__()
    client.get("/meta")
    st = app.state.trees[next(iter(app.state.trees))]
    assert st.loaded, st.load_error
    return st.parser.dag, st.data


def run_story(name, dag, data, fits):
    story = STORIES[name]
    target, ana, truth = story["target"], story["ana"], story["truth"]
    t0 = time.perf_counter()
    scope, windows = node_windows(dag, data, target, ana)
    inn = {n: innovation(dag, data, fits, n, windows[n]) for n in scope}
    usable = {n for n in scope if inn[n].get("e") is not None}
    departure = propagate(dag, data, scope, windows, inn, usable)[target]
    contrib = {n: propagate(dag, data, scope, windows, inn, {n})[target] for n in usable}
    elapsed = time.perf_counter() - t0

    ranked = sorted(usable, key=lambda n: -abs(contrib[n]))
    print(f"\n=== story {name}: {target}  analysis {ana[0]}..{ana[1]}  truth {sorted(truth)}")
    print(
        f"target actual {inn[target]['actual']:.4g}; departure from the tree's expectation "
        f"{departure:+.4g}; sum of single-node contributions {sum(contrib.values()):+.4g} "
        f"({elapsed:.0f}s incl. fits)"
    )
    print(
        f"{'node':28s} {'grain':5s} {'window':23s} {'innov':>10s} {'z':>7s} {'tail':>6s} "
        f"{'contrib':>10s} {'share':>7s}  status"
    )
    for n in ranked + sorted(scope - usable):
        i = inn[n]
        w = f"{windows[n][0].date()}..{windows[n][1].date()}"
        c = contrib.get(n)
        share = c / departure if c is not None and abs(departure) > 1e-12 else None
        f = lambda v, s: "" if v is None else format(v, s)  # noqa: E731
        mark = "*" if n in truth else " "
        print(
            f"{mark}{n:27s} {i['grain']:5s} {w:23s} {f(i.get('innovation'), '10.4g'):>10s} "
            f"{f(i.get('z'), '7.2f'):>7s} {f(i.get('tail'), '6.3f'):>6s} "
            f"{f(c, '10.4g'):>10s} {f(share, '7.1%'):>7s}  {i['status']}"
        )

    # Today's rankings, over the same analysis window.
    kwargs = dict(analysis_start=ana[0], analysis_end=ana[1], reference_sensitivity=False)
    if story["ref"]:
        kwargs.update(reference_start=story["ref"][0], reference_end=story["ref"][1])
    rca = run_rca(dag, data, fits, target, **kwargs)
    engine_rank = [c["metric"] for c in rca["ranked_causes"]]
    rel = {
        n: abs(v["relative_change"])
        for n, v in rca["nodes"].items()
        if n != target and v.get("relative_change") is not None
    }
    naive_rank = sorted(rel, key=lambda n: -rel[n])
    innov_rank = [n for n in ranked if n != target]
    z_rank = sorted(
        (n for n in usable if n != target and inn[n].get("z") is not None),
        key=lambda n: -abs(inn[n]["z"]),
    )
    result = {
        "innovation_contribution": rank_of(innov_rank, truth),
        "innovation_z": rank_of(z_rank, truth),
        "engine_ranked_causes": rank_of(engine_rank, truth),
        "naive_relative_change": rank_of(naive_rank, truth),
    }
    print("rank of the planted root (1 = first):", result)
    print("  innovation top 3:", innov_rank[:3])
    print("  engine     top 3:", engine_rank[:3])
    print("  naive      top 3:", naive_rank[:3])
    return result


# ---------------------------------------------------------------------------
# Null calibration: is `z` standard normal where nothing was planted?
#
# The planted anomalies (white_cube.ground_truth.json) occupy these spans. A
# window is "clean" when it sits in a gap between them, starting at least two
# weeks after the previous one ended — a level shift's end is itself a change,
# and a lagged edge carries it a week further. Every node is scored as its own
# target over every clean window, at several horizons from the same fit, so
# the horizon's effect on the predictive width is read off directly. A fit's
# *history* still contains earlier anomalies, as a real tree's would.
# ---------------------------------------------------------------------------
WHITE_CUBE_CLEAN_GAPS = [
    ("2024-12-02", "2025-03-02"),  # after the 2024 surprises, before the LatAm campaign
    ("2025-06-23", "2025-08-03"),  # after the campaign + null spike, before onboarding
    ("2025-12-15", "2026-02-01"),  # after onboarding + the dupes, before the CTA regression
    ("2026-03-30", "2026-04-26"),  # after the CTA regression, before the churn spike
    ("2026-07-13", "2026-07-26"),  # after the churn spike
]
# Where each planted anomaly enters the tree, and when: the positive class.
WHITE_CUBE_PLANTED = [
    ("visit_signup_rate", "2026-02-02"),
    ("customer_churn_rate", "2026-04-27"),
    ("marketing_spend", "2025-03-03"),
    ("visit_signup_rate", "2025-03-03"),
    ("trial_activation_rate", "2025-08-04"),
    ("trial_days_active", "2025-08-04"),
]
# Alpenglow's. `return_rate` is dated by the return request, which trails the
# order by delivery and request time (about two weeks), so the recall is also
# scored from the week its first returns arrive.
ALPENGLOW_PLANTED = [
    ("conversion_rate", "2024-09-12"),  # mobile_checkout_bug
    ("return_rate", "2024-10-07"),  # jackets_sizing_recall, as dated
    ("return_rate", "2024-10-21"),  # ... and when its returns reach the metric
    ("marketing_spend", "2025-02-03"),  # paid_social_budget_cut
    ("units", "2025-04-07"),  # surprise level shift in items_per_order
    ("aov", "2025-04-07"),
    ("units", "2025-05-05"),  # tees_demand_slump
    ("aov", "2025-05-05"),
    ("units", "2025-12-25"),  # surprise two-day spike in shorts demand
    ("aov", "2025-12-25"),
]
PLANTED = {"white_cube": WHITE_CUBE_PLANTED, "alpenglow": ALPENGLOW_PLANTED}[COMPANY]

BUFFER_DAYS = 14  # a clean window starts at least this long after an anomaly ends
MIN_HISTORY_DAYS = 182  # ... and this long after the data starts


def derive_clean_gaps(ground_truth, data_start, data_end):
    """The spans with nothing planted, from a fake_companies ground-truth list.

    A gap opens on the first Monday at least `BUFFER_DAYS` after one anomaly
    ends (and `MIN_HISTORY_DAYS` after the data starts) and closes the day
    before the next anomaly begins, or on the last day of data. Every entry
    counts, data-quality events included. Gaps too short to hold one 7-day
    window are dropped."""
    spans = sorted((pd.Timestamp(a["start"]), pd.Timestamp(a["end"])) for a in ground_truth)
    data_start, data_end = pd.Timestamp(data_start), pd.Timestamp(data_end)
    floor = data_start + pd.Timedelta(days=MIN_HISTORY_DAYS)
    gaps = []
    for i in range(len(spans) + 1):
        prev_end = max((e for _, e in spans[:i]), default=None)
        lo = floor if prev_end is None else max(floor, prev_end + pd.Timedelta(days=BUFFER_DAYS))
        lo += pd.Timedelta(days=(7 - lo.dayofweek) % 7)  # the next Monday
        hi = spans[i][0] - pd.Timedelta(days=1) if i < len(spans) else data_end
        if lo + pd.Timedelta(days=6) <= hi:
            gaps.append((str(lo.date()), str(hi.date())))
    return gaps


def clean_gaps():
    """White Cube's gaps are pinned as §6.1 to §6.3 ran them: they were
    written by hand and do not follow one rule (`windows` prints what the
    rule above gives instead), so deriving them would change those sections'
    numbers. Every other company's are derived."""
    if COMPANY == "white_cube":
        return WHITE_CUBE_CLEAN_GAPS
    with open(GROUND_TRUTH) as f:
        return derive_clean_gaps(json.load(f), CONFIG["start"], CONFIG["end"])


def clean_starts(gaps):
    """`(start, limit)` per weekly window. In a derived gap a start is kept
    only if a whole 7-day window fits; a pinned gap is taken as written."""
    out = []
    for lo, hi in gaps:
        for s in pd.date_range(lo, hi, freq="W-MON"):
            if COMPANY == "white_cube" or s + pd.Timedelta(days=6) <= pd.Timestamp(hi):
                out.append((str(s.date()), hi))
    return out


def show_windows():
    gaps = clean_gaps()
    print(f"{COMPANY}: {len(clean_starts(gaps))} weekly starts in {len(gaps)} gaps")
    for lo, hi in gaps:
        print(f"  {lo} .. {hi}  ({len(clean_starts([(lo, hi)]))} starts)")
    if os.path.exists(GROUND_TRUTH):
        with open(GROUND_TRUTH) as f:
            derived = derive_clean_gaps(json.load(f), CONFIG["start"], CONFIG["end"])
        if derived != gaps:
            print("derived from the ground truth instead (not used):")
            for lo, hi in derived:
                print(f"  {lo} .. {hi}")


HORIZON_DAYS = (1, 3, 7, 14, 28)  # 1 and 3 apply to day-grain nodes only
NULL_FIELDS = (
    "label node grain start horizon_days n z pit tail innovation pred_sd ppc_status status "
    "likelihood trend slope beta fit_seconds fit_quality min_ess divergences max_rhat"
).split()


def score_windows(dag, data, node, start, limit, label, writer, horizons=HORIZON_DAYS):
    """Score `node` over every horizon from `start` that ends by `limit`, from
    one fit, and drop the fit afterwards (a daily trace is ~10 MB)."""
    fits = {}
    start = pd.Timestamp(start)
    likelihood = defn_of(dag, node).likelihood
    fit_seconds = None
    for h in horizons:
        if h < 7 and data.grain_of[node] != "day":
            continue
        end = start + pd.Timedelta(days=h - 1)
        if end > pd.Timestamp(limit):
            continue
        try:
            r = innovation(dag, data, fits, node, (start, end))
        except Exception as exc:
            r = {"status": f"error: {exc}"[:100], "grain": data.grain_of[node]}
        # The one fit is paid for by the first horizon scored; every row of
        # the start carries its wall time so no horizon filter can lose it.
        fit_seconds = r.get("fit_seconds", fit_seconds)
        row = {k: r.get(k) for k in NULL_FIELDS}
        row.update(
            label=label,
            node=node,
            start=str(start.date()),
            horizon_days=h,
            likelihood=likelihood,
            fit_seconds=None if fit_seconds is None else round(fit_seconds, 2),
        )
        writer.writerow(row)


def null_run(
    out_path,
    nodes=None,
    forced=None,
    horizons=HORIZON_DAYS,
    trend=None,
    slope_sigma=None,
    shard=None,
    stride=1,
):
    """`shard=(k, n)` scores jobs `k, k + n, k + 2n, ...` only, so `n`
    processes with their own CSVs split one arm. `stride=n` keeps every n-th
    weekly start, for an arm whose fits are too slow to run on all of them."""
    import csv

    dag, data = load()
    force_likelihoods(dag, forced or {})
    nodes = nodes or list(nx.topological_sort(dag))
    unknown = [n for n in nodes if n not in dag]
    assert not unknown, f"not in the tree: {unknown}"
    force_trend(dag, list(dict.fromkeys(nodes + [n for n, _ in PLANTED])), trend, slope_sigma)
    done = set()
    fresh = not (os.path.exists(out_path) and os.path.getsize(out_path))
    if not fresh:
        with open(out_path) as f:
            reader = csv.DictReader(f)
            assert reader.fieldnames == NULL_FIELDS, (
                f"{out_path} was written with other columns ({reader.fieldnames}); start a new file"
            )
            done = {(r["label"], r["node"], r["start"], r["likelihood"]) for r in reader}
    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, NULL_FIELDS)
        if fresh:
            writer.writeheader()
            f.flush()
        # The planted roots are always scored, whatever `--nodes` says: they
        # are the positive class every arm is read against.
        last = "2026-07-26" if COMPANY == "white_cube" else CONFIG["end"]
        jobs = [("planted", n, s, last) for n, s in PLANTED]
        for s, hi in clean_starts(clean_gaps())[::stride]:
            jobs += [("null", n, s, hi) for n in nodes]
        for i, (label, node, start, limit) in enumerate(jobs):
            if shard and i % shard[1] != shard[0]:
                continue
            if (label, node, start, defn_of(dag, node).likelihood) in done:
                continue
            score_windows(dag, data, node, start, limit, label, writer, horizons)
            f.flush()
            print(f"[{i + 1}/{len(jobs)}] {label} {node} {start}", flush=True)


def null_summary(out_path):
    # `keep_default_na=False`: pandas reads the literal label "null" as missing.
    df = pd.read_csv(out_path, keep_default_na=False, na_values=[""])
    df = df[df["z"].notna() & (df["status"] != "exact_identity")]
    if "likelihood" not in df:  # a CSV from before the column existed
        df = df.assign(likelihood="gaussian", fit_seconds=np.nan)
    null, planted = df[df.label == "null"], df[df.label == "planted"]

    def table(frame, by):
        g = frame.groupby(by)
        return pd.DataFrame(
            {
                "n": g["z"].size(),
                "mean_z": g["z"].mean(),
                "sd_z": g["z"].std(),
                "|z|>2": g["z"].apply(lambda z: (z.abs() > 2).mean()),
                "|z|>3": g["z"].apply(lambda z: (z.abs() > 3).mean()),
                "tail<.05": g["tail"].apply(lambda t: (t < 0.05).mean()),
                "pit<.1|>.9": g["pit"].apply(lambda u: ((u < 0.1) | (u > 0.9)).mean()),
            }
        ).round(3)

    def calibration(frame):
        """Per node, likelihood and horizon. `sd_z` is about the node's own mean
        `z`, so a forecast that is biased (roadmap S8) and one that is too
        narrow (S20) read as different numbers; `rms_z` is the two together.
        `pit_ks` is the largest gap between the PIT's empirical distribution
        and the uniform's, 0 when flat; `pit_hist` its five equal bins."""
        rows = []
        for (node, lik, h), g in frame.groupby(["node", "likelihood", "horizon_days"]):
            u = np.sort(g["pit"].to_numpy(float))
            n = len(u)
            ks = max(np.max(np.arange(1, n + 1) / n - u), np.max(u - np.arange(0, n) / n))
            hist = np.histogram(u, bins=5, range=(0, 1))[0]
            fits = g.drop_duplicates("start")["fit_seconds"]
            rows.append(
                dict(
                    node=node,
                    likelihood=lik,
                    h=h,
                    n=n,
                    mean_z=g["z"].mean(),
                    sd_z=g["z"].std(),
                    rms_z=float(np.sqrt((g["z"] ** 2).mean())),
                    over2=(g["z"].abs() > 2).mean(),
                    over2_centred=((g["z"] - g["z"].mean()).abs() > 2).mean(),
                    pit_ks=ks,
                    pit_hist="/".join(str(c) for c in hist),
                    ppc="/".join(f"{k}:{v}" for k, v in g["ppc_status"].value_counts().items()),
                    fit_s_median=fits.median(),
                )
            )
        return pd.DataFrame(rows).round(3)

    pd.set_option("display.width", 250)
    if null["likelihood"].nunique() > 1 or null["fit_seconds"].notna().any():
        print("=== null windows, by node, likelihood and horizon")
        print(calibration(null).to_string(index=False))
        print()
    print("=== null windows, by horizon (calibrated: sd_z 1, |z|>2 0.046, pit tails 0.2)")
    print(table(null, "horizon_days").to_string())
    print("\n=== null windows, by node (all horizons)")
    print(table(null, "node").sort_values("sd_z").to_string())
    print("\n=== null windows, by node, 7-day horizon (non-overlapping windows)")
    print(table(null[null.horizon_days == 7], "node").sort_values("sd_z").to_string())
    print("\n=== planted roots at onset, against the same node's null z at the same horizon")
    rows = []
    for _, r in planted.iterrows():
        ref = null[
            (null.node == r.node)
            & (null.horizon_days == r.horizon_days)
            & (null.likelihood == r.likelihood)
        ]["z"]
        allz = null[null.horizon_days == r.horizon_days]["z"]
        rows.append(
            dict(
                node=r.node,
                likelihood=r.likelihood,
                onset=r.start,
                h=r.horizon_days,
                z=round(r.z, 2),
                node_null_sd=round(ref.std(), 2),
                z_over_null_sd=round(r.z / ref.std(), 2) if len(ref) > 2 else None,
                node_null_exceeding=f"{(ref.abs() >= abs(r.z)).sum()}/{len(ref)}",
                all_null_exceeding=f"{(allz.abs() >= abs(r.z)).mean():.3f}",
            )
        )
    print(pd.DataFrame(rows).to_string(index=False))


def null_compare(specs, horizons=(7, 28)):
    """Arms side by side: `name=a.csv[,b.csv...]` each (shards concatenate).

    Per node and pooled by role, at each horizon: `mean_z` (bias); `sd_z`
    about the node's own mean, so a biased forecast and a too-narrow one read
    as different numbers (pooled over a role, it is the within-node sd);
    `|z|>2` raw and with each node's mean removed; the PIT's KS distance from
    uniform; `ppc_status` counts; and per fit, the median wall time, the share
    with minimum bulk ESS under 400, the share with any divergence and their
    mean count, and the share marked `suspect`. Then the planted roots.
    """
    dag, _ = load()
    key = ["label", "node", "start", "horizon_days"]
    arms = {}
    for spec in specs:
        name, _, paths = spec.partition("=")
        df = pd.concat(
            pd.read_csv(path, keep_default_na=False, na_values=[""]) for path in paths.split(",")
        ).drop_duplicates(key)
        df = df[df["z"].notna() & (df["status"] != "exact_identity")]
        for col in ("divergences", "min_ess", "fit_quality", "fit_seconds", "likelihood"):
            if col not in df:  # a CSV from before the column existed
                df[col] = np.nan
        df["role"] = [node_role(dag, n) for n in df["node"]]
        arms[name] = df
    pd.set_option("display.width", 280)
    pd.set_option("display.max_rows", 1000)
    pd.set_option("display.max_columns", 40)

    def stats(g):
        centred = g["z"] - g.groupby("node")["z"].transform("mean")
        # One row per fit: the diagnostics are a property of the fit, and a
        # CSV from before `fit_seconds` rode on every row has it on one.
        fits = g.sort_values("fit_seconds").drop_duplicates(["node", "start"])
        div = pd.to_numeric(fits["divergences"], errors="coerce")
        dof = len(g) - g["node"].nunique()
        return {
            "n": len(g),
            "mean_z": g["z"].mean(),
            "sd_z": float(np.sqrt((centred**2).sum() / dof)) if dof > 0 else np.nan,
            "|z|>2": (g["z"].abs() > 2).mean(),
            "|z|>2 centred": (centred.abs() > 2).mean(),
            "pit_ks": _pit_ks(g["pit"]),
            "ppc ok/mod/sev": "/".join(
                str(int((g["ppc_status"] == k).sum())) for k in ("ok", "moderate", "severe")
            ),
            "fit_s": fits["fit_seconds"].median(),
            "ess<400": (fits["min_ess"] < 400).mean() if fits["min_ess"].notna().any() else np.nan,
            "div>0": (div > 0).mean() if div.notna().any() else np.nan,
            "div_mean": div.mean(),
            "suspect": (fits["fit_quality"] == "suspect").mean(),
        }

    for h in horizons:
        for by in ("node", "role"):
            rows = {}
            for name, df in arms.items():
                null = df[(df.label == "null") & (df.horizon_days == h)]
                for group, g in null.groupby(by):
                    rows[(group, name)] = stats(g)
            print(f"\n=== null windows, {h}-day horizon, by {by}")
            print(
                pd.DataFrame(rows)
                .T.sort_index(level=0, sort_remaining=False)
                .to_string(float_format=lambda v: f"{v:.3f}")
            )

    print("\n=== planted roots at onset: z, and clean windows on the same node as extreme")
    rows = {}
    for name, df in arms.items():
        null = df[df.label == "null"]
        for _, r in df[(df.label == "planted") & df.horizon_days.isin(horizons)].iterrows():
            ref = null[(null.node == r.node) & (null.horizon_days == r.horizon_days)]["z"]
            rows[(r.node, r.start, int(r.horizon_days), name)] = {
                "z": round(r.z, 2),
                "pred_sd": r.pred_sd,
                "z/null_sd": round(r.z / ref.std(), 2) if len(ref) > 2 else None,
                "null_as_extreme": f"{(ref.abs() >= abs(r.z)).sum()}/{len(ref)}",
            }
    if rows:
        print(pd.DataFrame(rows).T.sort_index(sort_remaining=False).to_string())


def describe(out_path=None):
    """The company in numbers: per fetched node, what the calibration claims
    need to know before a single fit (design doc §6.4).

    Model-free on purpose. The level is a centred rolling mean over four weeks
    and the residual is what is left after that level and one additive
    weekday effect, which is the shape of the engine's default model (a
    level, additive seasonality, constant Gaussian noise) without its fit.

    - `growth`: mean of the last third of the history over the first third.
    - `fall_13w`: the largest fall of the level over any 13 weeks, as a share
      of where it started. A business that only grows has 0.
    - `annual_swing`: range of the level about its straight-line trend, over
      the mean. `weekday_share`: share of the detrended variance that the
      weekday effect explains (day grain only).
    - `zeros`, `zero_run`: share of periods exactly zero; longest run of them.
    - `var_power`: slope of log residual variance on log level over eight
      level bins. 0 is constant noise, 1 a Poisson-like count, 2 noise
      proportional to the level.
    - `late_sd`: residual sd in the last third over the pooled residual sd.
      Above 1, one noise level is too narrow where an analysis window sits.
    - `dispersion`: residual variance over the mean level, for counts. 1 is a
      Poisson; below 1 a negative binomial cannot represent it.
    """
    dag, data = load()
    rows = []
    for node in nx.topological_sort(dag):
        defn = defn_of(dag, node)
        if defn.formula:
            continue
        grain = data.grain_of[node]
        frame = data.fit_frame(node, [], grain)
        y = pd.Series(frame[node].to_numpy(float), index=pd.DatetimeIndex(frame["date"]))
        y = y.dropna()
        n = len(y)
        per_week = 7 if grain == "day" else 1
        level = y.rolling(4 * per_week, center=True, min_periods=2 * per_week).mean()
        detr = y - level
        weekday = detr.groupby(y.index.dayofweek).transform("mean") if grain == "day" else 0.0
        resid = (detr - weekday).dropna()
        third = n // 3
        t = np.arange(n)
        line = np.polyval(np.polyfit(t, level.bfill().ffill().to_numpy(), 1), t)
        span = 13 * per_week
        lv = level.dropna()
        fall = max(0.0, float(((lv - lv.shift(-span)) / lv).max())) if len(lv) > span else np.nan
        is_zero = (y == 0).to_numpy()
        run = longest = 0
        for z in is_zero:
            run = run + 1 if z else 0
            longest = max(longest, run)
        bins = pd.qcut(level[resid.index], 8, duplicates="drop")
        by_bin = pd.DataFrame({"level": level[resid.index], "r": resid}).groupby(
            bins, observed=True
        )
        lev, var = by_bin["level"].mean(), by_bin["r"].var()
        usable = (lev > 0) & (var > 0)
        power = (
            float(np.polyfit(np.log(lev[usable]), np.log(var[usable]), 1)[0])
            if usable.sum() >= 4
            else np.nan
        )
        whole = bool(np.all(np.abs(y - y.round()) < 1e-9))
        rows.append(
            dict(
                node=node,
                role=node_role(dag, node),
                parents=",".join(dag.predecessors(node)) or "-",
                grain=grain,
                n=n,
                mean=float(y.mean()),
                growth=float(y.iloc[-third:].mean() / y.iloc[:third].mean()),
                fall_13w=fall,
                annual_swing=float((level - line).max() - (level - line).min()) / float(y.mean()),
                weekday_share=(
                    float(1 - resid.var() / detr.dropna().var()) if grain == "day" else np.nan
                ),
                zeros=float(is_zero.mean()),
                zero_run=longest,
                whole=whole,
                var_power=power,
                late_sd=float(resid.iloc[-third:].std() / resid.std()),
                dispersion=float(resid.var() / y.mean())
                if whole and defn.kind == "flow"
                else np.nan,
            )
        )
    table = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 40)
    print(table.to_string(index=False, float_format=lambda v: f"{v:.3g}"))
    if out_path:
        table.to_csv(out_path, index=False)


def main():
    cli = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    cli.add_argument("args", nargs="*")
    cli.add_argument("--company", default=COMPANY, help="white_cube (default) or alpenglow")
    cli.add_argument("--nodes", help="null: comma-separated nodes to restrict the null windows to")
    cli.add_argument("--horizons", help="null: comma-separated horizons in days")
    cli.add_argument(
        "--likelihood", action="append", default=[], help="null: node=family, repeatable"
    )
    cli.add_argument(
        "--trend",
        choices=("level", "local_linear", "role"),
        help="force this trend kind; `role` = slope on parentless flows and stocks only",
    )
    cli.add_argument("--slope-sigma", type=float, help="with a slope; 0 = fixed drift")
    cli.add_argument("--shard", help="null: k/n — score every n-th job starting at the k-th")
    cli.add_argument("--stride", type=int, default=1, help="null: keep every n-th weekly start")
    opts = cli.parse_args()
    argv = opts.args
    if argv[:1] == ["null"]:
        shard = tuple(int(x) for x in opts.shard.split("/")) if opts.shard else None
        return null_run(
            argv[1],
            nodes=opts.nodes.split(",") if opts.nodes else None,
            forced=dict(v.split("=", 1) for v in opts.likelihood),
            horizons=(
                tuple(int(h) for h in opts.horizons.split(",")) if opts.horizons else HORIZON_DAYS
            ),
            trend=opts.trend,
            slope_sigma=opts.slope_sigma,
            shard=shard,
            stride=opts.stride,
        )
    if argv[:1] == ["null-summary"]:
        return null_summary(argv[1])
    if argv[:1] == ["null-compare"]:
        return null_compare(argv[1:])
    if argv[:1] == ["windows"]:
        return show_windows()
    if argv[:1] == ["describe"]:
        return describe(argv[1] if len(argv) > 1 else None)
    names = argv or list(STORIES)
    dag, data = load()
    force_trend(dag, list(dag.nodes), opts.trend, opts.slope_sigma)
    fits = {}
    results = {n: run_story(n, dag, data, fits) for n in names}
    print("\n=== summary: rank of the planted root cause")
    methods = list(next(iter(results.values())))
    print(f"{'story':10s} " + " ".join(f"{m:>24s}" for m in methods))
    for n, r in results.items():
        print(f"{n:10s} " + " ".join(f"{str(r[m]):>24s}" for m in methods))


if __name__ == "__main__":
    main()
