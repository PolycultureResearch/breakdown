"""The numba compile cache must never hand one fit another fit's compiled code
(roadmap C65).

Through PyTensor 2.38.2, numba-compiled functions were cached on disk under
symbol names that were only unique within the process that compiled them. Two
entries written by different processes could share a name, and a process that
loaded both ran the first one's code for the second. PyTensor 2.38.3 (the
floor in `pyproject.toml`) stopped writing such entries; it still reads them.
`breakdown/engine/compile_cache.py` is what keeps them from being read.

Every process in these tests gets its own `base_compiledir`, so nothing here
reads or writes the developer's real cache.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from breakdown.engine import compile_cache

REPO = Path(__file__).resolve().parent.parent


def run(script, *args, compiledir, env=None):
    """One fresh interpreter with its own compile cache and a fixed hash seed.

    The fixed seed makes a collision between two old-style writers certain
    instead of a 1-in-80 chance: part of the symbol name is built from a
    Python set and so depends on string hashing.
    """
    full_env = {
        **os.environ,
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(REPO),
        "PYTENSOR_FLAGS": f"base_compiledir={compiledir}",
        **(env or {}),
    }
    full_env.pop("BREAKDOWN_NUMBA_CACHE_HARDENED", None)
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        env=full_env,
        # `-c` puts the working directory first on `sys.path`, ahead of
        # PYTHONPATH, so this is what decides which `breakdown` is imported.
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT "))
    return json.loads(line[len("RESULT ") :])


# ---------------------------------------------------------------------------
# A cache written before the fix is on disk. It must not be read.
# ---------------------------------------------------------------------------

# Two different functions of one vector. `old` reproduces what PyTensor 2.38.2
# did: numba's own counter, restarted in each process. `harden` is breakdown.
GRAPH_SCRIPT = """
import itertools, json, sys
import numpy as np
mode, names = sys.argv[1], sys.argv[2:]
if mode == "harden":
    from breakdown.engine.compile_cache import ensure_safe_compile_cache
    ensure_safe_compile_cache()
import pytensor, pytensor.tensor as pt
import pytensor.link.numba.cache
if mode == "old":
    from numba.core.bytecode import FunctionIdentity
    FunctionIdentity._unique_ids = itertools.count(1)
x = pt.dvector("x")
v = np.array([1.0, 2.0])
graphs = {"f": pt.exp(x) + 10.0, "g": pt.log(x) * 2.0}
out = {n: pytensor.function([x], graphs[n], mode="NUMBA")(v).tolist() for n in names}
print("RESULT " + json.dumps(out))
"""
F = [12.718281828459045, 17.38905609893065]  # exp(v) + 10
G = [0.0, 1.3862943611198906]  # log(v) * 2


@pytest.mark.slow
def test_entries_an_older_pytensor_wrote_are_never_loaded(tmp_path):
    """Two processes write `f` and `g` the way PyTensor 2.38.2 did. A process
    that then loads both through PyTensor alone gets `f`'s values for `g`,
    on 2.38.3 as on 2.38.2, because the fix upstream changes what is written
    and not what is read. A process that went through breakdown first does
    not look in that directory and gets both right."""
    assert run(GRAPH_SCRIPT, "old", "f", compiledir=tmp_path)["f"] == pytest.approx(F)
    assert run(GRAPH_SCRIPT, "old", "g", compiledir=tmp_path)["g"] == pytest.approx(G)

    unprotected = run(GRAPH_SCRIPT, "plain", "f", "g", compiledir=tmp_path)
    if unprotected["g"] == pytest.approx(G):
        pytest.skip(
            "this PyTensor no longer loads a colliding entry on its own, so the "
            "premise of the test is gone; the hardening can probably go with it"
        )
    assert unprotected["g"] == pytest.approx(F)  # the defect: g ran f's code

    protected = run(GRAPH_SCRIPT, "harden", "f", "g", compiledir=tmp_path)
    assert protected["f"] == pytest.approx(F)
    assert protected["g"] == pytest.approx(G)
    assert list((tmp_path / compile_cache.CACHE_DIRNAME).glob("*.nbi"))


# ---------------------------------------------------------------------------
# The same thing end to end, through `fit_metric`.
# ---------------------------------------------------------------------------

FIT_SCRIPT = """
import json, logging, sys, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from breakdown.engine.model import fit_metric
from breakdown.parser import Parser
logging.disable(logging.WARNING)

n = 60
rng = np.random.default_rng(3)
data = pd.DataFrame({
    "date": pd.date_range("2024-01-01", periods=n),
    "a": 100.0 + np.cumsum(rng.normal(0, 2.0, n)),   # a random walk
    "b": 20.0 + rng.normal(0, 3.0, n),               # flat and noisy
})
dag = Parser('''
metrics:
  - name: a
    source: dbt.metric.a
  - name: b
    source: dbt.metric.b
''').dag
out = {}
for node in sys.argv[1:]:
    fit = fit_metric(dag, data, node, draws=100, tune=300, chains=2, random_seed=0)
    post = fit.trace.posterior
    out[node] = {k: float(post[k].mean()) for k in ("alpha", "sigma_obs", "sigma_trend")}
print("RESULT " + json.dumps(out))
"""


@pytest.mark.slow
def test_a_fit_never_gets_another_fits_compiled_code_from_the_cache(tmp_path):
    """Three starts sharing one cache: fit `a`; fit `b`; fit `a` then `b`.

    On 0.3.0 (PyTensor 2.38.2) the third process returned `a`'s posterior for
    `b` exactly (alpha 0.38 and sigma_obs 0.55 where `b`'s are -0.10 and
    1.00), converged, with no error.
    """
    cache = tmp_path / "compiledir"
    alone_a = run(FIT_SCRIPT, "a", compiledir=cache)["a"]
    alone_b = run(FIT_SCRIPT, "b", compiledir=cache)["b"]
    # The two posteriors are far apart, so one cannot be mistaken for the other.
    assert abs(alone_a["sigma_obs"] - alone_b["sigma_obs"]) > 0.3

    for order in (("a", "b"), ("b", "a")):
        both = run(FIT_SCRIPT, *order, compiledir=cache)
        for node, alone in (("a", alone_a), ("b", alone_b)):
            for name, value in alone.items():
                assert both[node][name] == pytest.approx(value, abs=0.05), (order, node, name)

    # Nothing was written to the directory PyTensor shares with every other
    # program that uses it, by the fitting process or by its chains.
    assert not list((cache / "numba").glob("*.nb*"))
    assert list((cache / compile_cache.CACHE_DIRNAME).glob("*.nbi"))


# ---------------------------------------------------------------------------
# What `ensure_safe_compile_cache` changes, and when it declines to.
# ---------------------------------------------------------------------------

STATE_SCRIPT = """
import json, os
{before}
from breakdown.engine.compile_cache import ensure_safe_compile_cache
state = ensure_safe_compile_cache()
import pytensor
import pytensor.link.numba.cache as pt_cache
print("RESULT " + json.dumps({{
    "state": state,
    "again": ensure_safe_compile_cache(),
    "cache_on": bool(pytensor.config.numba__cache),
    "cache_dir": pt_cache.NUMBA_CACHE_PATH.name,
    "child_flags": os.environ.get("PYTENSOR_FLAGS", ""),
    "marker": os.environ.get("BREAKDOWN_NUMBA_CACHE_HARDENED"),
}}))
"""
SEQUENTIAL_IDS = (
    "import itertools, pytensor.link.numba.cache\n"
    "from numba.core.bytecode import FunctionIdentity\n"
    "FunctionIdentity._unique_ids = itertools.count(1)"
)
NO_IDS = (
    "import pytensor.link.numba.cache\n"
    "from numba.core.bytecode import FunctionIdentity\n"
    "del FunctionIdentity._unique_ids"
)


def test_a_hardened_process_has_its_own_directory_and_safe_children(tmp_path):
    got = run(STATE_SCRIPT.format(before=""), compiledir=tmp_path)
    assert got["state"] == got["again"] == "hardened"
    assert got["cache_on"] is True
    assert got["cache_dir"] == compile_cache.CACHE_DIRNAME
    # A Python process started from this one that never imports the module
    # gets the cache turned off, and keeps the directory the user chose.
    flags = got["child_flags"].split(",")
    assert "numba__cache=False" in flags and f"base_compiledir={tmp_path}" in flags
    assert got["marker"] == "1"


def test_a_child_that_inherits_the_flag_hardens_itself_and_keeps_its_cache(tmp_path):
    """The forkserver's case: its environment says the cache is off, because
    the parent said so for unhardened children, and the marker says why."""
    # Not through `run`, which strips the marker so other tests start clean.
    inherited = subprocess.run(
        [sys.executable, "-c", STATE_SCRIPT.format(before="")],
        env={
            **os.environ,
            "PYTHONPATH": str(REPO),
            "PYTENSOR_FLAGS": f"base_compiledir={tmp_path},numba__cache=False",
            "BREAKDOWN_NUMBA_CACHE_HARDENED": "1",
        },
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert inherited.returncode == 0, inherited.stderr[-2000:]
    got = json.loads(inherited.stdout.splitlines()[-1][len("RESULT ") :])
    assert got["state"] == "hardened"
    assert got["cache_on"] is True
    assert got["cache_dir"] == compile_cache.CACHE_DIRNAME


@pytest.mark.parametrize("before", [SEQUENTIAL_IDS, NO_IDS], ids=["sequential", "missing"])
def test_the_cache_is_turned_off_when_function_ids_are_not_unique(tmp_path, before):
    """A PyTensor older than the floor, or a numba that has moved the counter:
    nothing this process compiles is safe to cache. Slower, not wrong."""
    got = run(STATE_SCRIPT.format(before=before), compiledir=tmp_path)
    assert got["state"] == got["again"] == "disabled"
    assert got["cache_on"] is False
    assert got["cache_dir"] == "numba"  # not redirected: nothing will be read or written
    assert "numba__cache=False" in got["child_flags"].split(",")


def test_a_user_who_turned_the_cache_off_is_left_alone(tmp_path):
    got = run(
        STATE_SCRIPT.format(before=""),
        compiledir=tmp_path,
        env={"PYTENSOR_FLAGS": f"base_compiledir={tmp_path},numba__cache=False"},
    )
    assert got["state"] == "user_disabled"
    assert got["cache_on"] is False
    assert got["cache_dir"] == "numba"
    assert got["marker"] is None
    assert got["child_flags"] == f"base_compiledir={tmp_path},numba__cache=False"


def test_the_installed_pytensor_makes_function_ids_unique():
    """The floor in `pyproject.toml`, checked where it matters: on the
    interpreter the suite runs on."""
    assert compile_cache._function_ids_are_unique_across_processes()


def test_the_forkserver_preloads_the_hardening():
    """Under forkserver (Linux on Python 3.14) chains are forked from a
    server that never ran `fit_metric`; the preload is what hardens them."""
    import multiprocessing.forkserver as fs

    from breakdown.engine.model import preload_forkserver_modules

    preload_forkserver_modules()
    assert "breakdown.engine.compile_cache_preload" in fs._forkserver._preload_modules
