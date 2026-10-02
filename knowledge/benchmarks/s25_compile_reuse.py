"""Design §2.6: is reusing the compiled model worth building?

`knowledge/speed_and_warm_analyses_design.md` §2.6 proposed building each
node's model once with its data as `pm.Data`, caching the compiled logp/dlogp
on `TreeState`, and swapping data per `fit_end`. This measures what that would
save, against what PyTensor's own on-disk numba cache (`numba__cache`, on by
default) already saves for nothing. Three suites:

- **phases NODE FIT_END...** — fit a White Cube node once per `fit_end` in one
  process, exactly as `run_rca` does (`fit_rca_node`), and split each fit into
  the logp/dlogp compile, sampling, FFBS level recovery, PPC and diagnostics.
  Numba jits lazily on first call, so on a cold cache most of the compile
  shows up inside *sampling*; compare totals across cache states, not the
  compile column alone.
- **reuse today|reuse** — a synthetic `sessions`-shaped node (one parent,
  weekly Fourier term, ~700 periods, the S25 Kalman potential) fitted at three
  lengths, either rebuilt per fit (today) or built once with `pm.Data` and one
  `pm.NUTS` step reused under `pm.set_data` (the §2.6 prototype).
- **warmpass** — the fits roadmap 3.10's background warm makes on White Cube
  (`plan_warm_fits`), end to end.

Control the cache with `PYTENSOR_FLAGS=base_compiledir=<dir>`: a fresh dir is
a cold cache (a new container), and running the same command again against
that dir is a warm one.

Usage:
    PYTENSOR_FLAGS=base_compiledir=/tmp/pt uv run python \
        knowledge/benchmarks/s25_compile_reuse.py phases sessions 2026-05-11 2026-05-04
    ... s25_compile_reuse.py reuse today        # then: reuse reuse
    ... s25_compile_reuse.py warmpass

Needs the demo snapshots (`make -C demo snapshots`). Findings are in the
design doc's §2.6.
"""

import os
import sys
import time
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)  # runnable from anywhere, without an editable install
DEMO = os.path.join(REPO, "demo")
os.environ.update(
    BREAKDOWN_TREE=os.path.join(DEMO, "white_cube_tree.yml"),
    BREAKDOWN_START_DATE="2024-06-01",
    BREAKDOWN_END_DATE="2026-07-30",
    BREAKDOWN_SNAPSHOT_DIR=os.path.join(DEMO, ".breakdown", "snapshots"),
    WHITE_CUBE_DBT_PROJECT="/nonexistent/white-cube-has-no-provider",
)
os.environ.pop("BREAKDOWN_WARM", None)

import numpy as np  # noqa: E402
import pymc as pm  # noqa: E402
import pymc.model.core as pmcore  # noqa: E402
import pytensor.tensor as pt  # noqa: E402

import breakdown.engine.model as M  # noqa: E402

T: dict = defaultdict(float)


def _timed(label, fn):
    def wrapper(*a, **k):
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            T[label] += time.perf_counter() - t0

    return wrapper


def _instrument():
    pmcore.Model.logp_dlogp_function = _timed("compile", pmcore.Model.logp_dlogp_function)
    pm.sample = _timed("sample", pm.sample)
    M._attach_recovered_level = _timed("ffbs", M._attach_recovered_level)
    M._posterior_predictive_draws = _timed("ppc", M._posterior_predictive_draws)
    M._nuts_diagnostics = _timed("diag", M._nuts_diagnostics)


def _white_cube():
    from fastapi.testclient import TestClient

    from breakdown.api.main import app

    client = TestClient(app)
    client.__enter__()
    client.get("/meta")  # the data loads lazily, on first request
    st = app.state.trees[next(iter(app.state.trees))]
    assert st.loaded, st.load_error
    return st.parser.dag, st.data


def phases(node, fit_ends):
    from breakdown.engine.rca import fit_rca_node

    _instrument()
    dag, data = _white_cube()
    for fe in fit_ends:
        T.clear()
        t0 = time.perf_counter()
        fit_rca_node(dag, data, node, fe)
        total = time.perf_counter() - t0
        print(
            f"{node} fit_end={fe} total={total:.2f}s compile={T['compile']:.2f} "
            f"sample_ex_compile={T['sample'] - T['compile']:.2f} ffbs={T['ffbs']:.2f} "
            f"ppc={T['ppc']:.2f} diag={T['diag']:.2f}",
            flush=True,
        )


def warmpass():
    from breakdown.engine.rca import fit_rca_node
    from breakdown.engine.warm import plan_warm_fits

    _instrument()
    dag, data = _white_cube()
    fits, _ = plan_warm_fits(dag, data, {})
    t00 = time.perf_counter()
    for f in fits:
        T.clear()
        t0 = time.perf_counter()
        fit_rca_node(dag, data, f.node, f.fit_end)
        print(
            f"  {f.node:24s} {f.fit_end} {time.perf_counter() - t0:6.2f}s "
            f"(logp+dlogp compile {T['compile']:.2f})",
            flush=True,
        )
    print(f"warm pass: {len(fits)} fits in {time.perf_counter() - t00:.1f}s")


# --- the synthetic §2.6 prototype -------------------------------------------

_rng = np.random.default_rng(0)
_N = 709
_x = _rng.normal(size=_N)
_y = 0.8 * _x + np.cumsum(_rng.normal(scale=0.02, size=_N)) + _rng.normal(scale=0.1, size=_N)
_t = np.arange(_N, dtype=float)
LENGTHS = [680, 673, 652]  # three fit_ends, three series lengths
SAMPLE = dict(draws=M.NUTS_DRAWS, tune=M.NUTS_TUNE, chains=M.NUTS_CHAINS, progressbar=False)
NUMBA = {"mode": "NUMBA"}


def _build(n, data=False):
    with pm.Model() as m:
        if data:
            y, x, t = (pm.Data(k, v[:n]) for k, v in (("y", _y), ("x", _x), ("t", _t)))
        else:
            y, x, t = _y[:n], _x[:n], _t[:n]
        st = pm.HalfNormal("sigma_trend", 0.05)
        a, b = pm.Normal("sin_w", 0, 1), pm.Normal("cos_w", 0, 1)
        beta, alpha = pm.Normal("beta", 0, 1), pm.Normal("alpha", 0, 1)
        so = pm.HalfNormal("sigma_obs", 1.0)
        mu = alpha + a * pt.sin(2 * np.pi * t / 7) + b * pt.cos(2 * np.pi * t / 7) + beta * x
        pm.Potential("ll", M._kalman_local_level_loglik(pt.as_tensor(y) - mu, st, so))
    return m


def reuse(which):
    times = []
    if which == "today":
        for i, n in enumerate(LENGTHS):
            t0 = time.perf_counter()
            with _build(n):
                pm.sample(target_accept=0.9, random_seed=i, compile_kwargs=NUMBA, **SAMPLE)
            times.append(time.perf_counter() - t0)
        print("today (rebuild per fit):", " ".join(f"{s:.2f}s" for s in times))
        return
    t0 = time.perf_counter()
    m = _build(LENGTHS[0], data=True)
    with m:
        step = pm.NUTS(target_accept=0.9, compile_kwargs=NUMBA)
    setup = time.perf_counter() - t0
    for i, n in enumerate(LENGTHS):
        t0 = time.perf_counter()
        with m:
            pm.set_data({"y": _y[:n], "x": _x[:n], "t": _t[:n]})
            pm.sample(step=step, random_seed=i, **SAMPLE)
        times.append(time.perf_counter() - t0)
    print(f"reuse (build once, setup {setup:.2f}s):", " ".join(f"{s:.2f}s" for s in times))


if __name__ == "__main__":
    suite, args = sys.argv[1], sys.argv[2:]
    if suite == "phases":
        phases(args[0], args[1:])
    elif suite == "reuse":
        reuse(args[0])
    elif suite == "warmpass":
        warmpass()
    else:
        sys.exit(f"unknown suite {suite!r}: phases | reuse | warmpass")
