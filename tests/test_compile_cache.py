"""The numba compile cache must never hand one fit another fit's compiled code.

PyTensor caches numba-compiled functions on disk with symbol names that are
only unique within the process that compiled them. Two cache entries written
by different processes can share a name, and a process that loads both runs
the first one's code for the second (see `breakdown/engine/compile_cache.py`).
The slow test below is that failure, reproduced from an empty cache: before
the fix the last process returns node `a`'s posterior for node `b`.

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


def run(script, *args, compiledir, env=None):
    """One fresh interpreter, with its own compile cache and a fixed hash seed.

    The fixed seed makes the collision certain instead of a 1-in-80 chance
    per pair of processes: part of the symbol name is built from a Python set
    and so depends on string hashing.
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
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT "))
    return json.loads(line[len("RESULT ") :])


@pytest.mark.slow
def test_a_fit_never_gets_another_fits_compiled_code_from_the_cache(tmp_path):
    """Three starts sharing one cache: fit `a`; fit `b`; fit `a` then `b`.

    On 0.3.0 the third process returned `a`'s posterior for `b` exactly
    (alpha 0.38 and sigma_obs 0.55 where `b`'s are -0.10 and 1.00),
    converged, with no error.
    """
    cache = tmp_path / "compiledir"
    alone_a = run(FIT_SCRIPT, "a", compiledir=cache)["a"]
    alone_b = run(FIT_SCRIPT, "b", compiledir=cache)["b"]
    # The two posteriors are far apart, so one cannot be mistaken for the other.
    assert abs(alone_a["sigma_obs"] - alone_b["sigma_obs"]) > 0.3

    both = run(FIT_SCRIPT, "a", "b", compiledir=cache)
    for node, alone in (("a", alone_a), ("b", alone_b)):
        for name, value in alone.items():
            assert both[node][name] == pytest.approx(value, abs=0.05), (node, name, both)

    # And in the other order, which is the half that failed for `a`.
    both = run(FIT_SCRIPT, "b", "a", compiledir=cache)
    for node, alone in (("a", alone_a), ("b", alone_b)):
        for name, value in alone.items():
            assert both[node][name] == pytest.approx(value, abs=0.05), (node, name, both)

    # Nothing was written to the directory PyTensor shares with every other
    # program that uses it.
    assert not list((cache / "numba").glob("*.nb*"))
    assert list((cache / compile_cache.CACHE_DIRNAME).glob("*.nbi"))


STATE_SCRIPT = """
import json, os
{before}
from breakdown.engine.compile_cache import ensure_safe_compile_cache
state = ensure_safe_compile_cache()
import pytensor
import pytensor.link.numba.cache as pt_cache
from numba.core.bytecode import FunctionIdentity
print("RESULT " + json.dumps({{
    "state": state,
    "again": ensure_safe_compile_cache(),
    "cache_on": bool(pytensor.config.numba__cache),
    "cache_dir": pt_cache.NUMBA_CACHE_PATH.name,
    "next_id": next(FunctionIdentity._unique_ids) if hasattr(FunctionIdentity, "_unique_ids") else None,
    "child_flags": os.environ.get("PYTENSOR_FLAGS", ""),
}}))
"""


def test_a_hardened_process_has_unique_ids_its_own_directory_and_safe_children(tmp_path):
    got = run(STATE_SCRIPT.format(before=""), compiledir=tmp_path)
    assert got["state"] == got["again"] == "hardened"
    assert got["cache_on"] is True
    assert got["cache_dir"] == compile_cache.CACHE_DIRNAME
    # numba counts from 1 in every process; a hardened one draws random
    # 128-bit ids instead.
    assert got["next_id"] > 2**64
    # A Python process started from this one that never imports the module
    # gets the cache turned off, and keeps the directory the user chose.
    flags = got["child_flags"].split(",")
    assert "numba__cache=False" in flags and f"base_compiledir={tmp_path}" in flags


def test_two_processes_do_not_hand_out_the_same_id(tmp_path):
    """Unhardened, both would print the same small integer: the count of
    functions numba compiled while importing, plus one."""
    first = run(STATE_SCRIPT.format(before=""), compiledir=tmp_path)["next_id"]
    second = run(STATE_SCRIPT.format(before=""), compiledir=tmp_path)["next_id"]
    assert first != second


def test_the_cache_is_turned_off_when_numba_cannot_be_hardened(tmp_path):
    """A numba that no longer has the counter this relies on: slower, not wrong."""
    before = "from numba.core.bytecode import FunctionIdentity\ndel FunctionIdentity._unique_ids"
    got = run(STATE_SCRIPT.format(before=before), compiledir=tmp_path)
    assert got["state"] == got["again"] == "disabled"
    assert got["cache_on"] is False
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
    assert got["next_id"] < 2**20


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_forked_child_does_not_repeat_its_parents_next_id(tmp_path):
    """Chains are forked from the fitting process (or from a forkserver that
    preloaded the hardening). A child that inherited a counter would hand out
    exactly the ids its parent and its siblings hand out next, so two chains
    of different fits would compile under the same names."""
    script = STATE_SCRIPT.format(before="") + (
        "\nimport os\n"
        "r, w = os.pipe()\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.write(w, str(next(FunctionIdentity._unique_ids)).encode())\n"
        "    os._exit(0)\n"
        "os.waitpid(pid, 0)\n"
        "child = int(os.read(r, 64))\n"
        "parent = next(FunctionIdentity._unique_ids)\n"
        'print("RESULT " + json.dumps({"child": child, "parent": parent}))\n'
    )
    proc_env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "PYTENSOR_FLAGS": f"base_compiledir={tmp_path}",
    }
    proc_env.pop("BREAKDOWN_NUMBA_CACHE_HARDENED", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=proc_env,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.splitlines()[-1][len("RESULT ") :])
    assert got["child"] != got["parent"] and min(got["child"], got["parent"]) > 2**64


def test_the_forkserver_preloads_the_hardening():
    """Under forkserver (Linux on Python 3.14) chains are forked from a
    server that never ran `fit_metric`; the preload is what hardens them."""
    import multiprocessing.forkserver as fs

    from breakdown.engine.model import preload_forkserver_modules

    preload_forkserver_modules()
    assert "breakdown.engine.compile_cache_preload" in fs._forkserver._preload_modules
