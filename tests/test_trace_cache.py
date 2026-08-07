"""Trace cache identity, bounds and thread safety (roadmap C8).

The cache is the one piece of mutable state the engine touches, and it is
mutated from a worker thread while HTTP handlers read it. These tests cover
the three ways that went wrong: a key that was not an identity, no bound, and
no synchronization.
"""
import threading

import pytest

from breakdown.engine.model import FitKey, TraceCache


def test_fit_key_defaults_match_the_engines_on_demand_fit():
    """`FitKey(node, window)` is what run_rca/run_scenario mean: a seeded ADVI
    fit. Call sites rely on these defaults, so pin them."""
    k = FitKey("revenue", "2024-03-01")
    assert k.inference_method == "advi"
    assert k.random_seed == 0
    assert (k.metric, k.fit_end) == ("revenue", "2024-03-01")


def test_analyze_settings_cannot_poison_the_rca_key():
    """The reported defect: `POST /analyze/x?draws=50` wrote the very entry
    run_rca would reuse, so later CIs became percentiles of 50 unseeded
    samples — silently falsifying the promise that a report_url deep link
    reproduces its numbers."""
    engine = FitKey("x", "2024-03-01", draws=500)
    manual = FitKey("x", "2024-03-01", inference_method="advi", draws=50,
                    tune=500, chains=1, random_seed=None)

    assert engine != manual
    cache = TraceCache()
    cache[engine], cache[manual] = "engine-fit", "manual-fit"
    assert cache[engine] == "engine-fit"   # not overwritten
    assert len(cache) == 2


def test_identical_specs_still_share_one_entry():
    """The key must be an identity, not a fingerprint: two requests for the
    same fit should hit, or the cache does nothing."""
    cache = TraceCache()
    cache[FitKey("x", "2024-03-01", draws=300)] = "fit"
    assert FitKey("x", "2024-03-01", draws=300) in cache


def test_cache_evicts_least_recently_used():
    """A 107-metric tree adds ~100 InferenceData objects per RCA and ~100 more
    per additional analysis window. Unbounded, a handful of requests exhausted
    the process."""
    cache = TraceCache(max_entries=3)
    for i in range(3):
        cache[FitKey(f"m{i}", None)] = i
    cache[FitKey("m0", None)]                      # a read counts as a use
    cache[FitKey("m3", None)] = 3                  # evicts the LRU, m1

    assert len(cache) == 3
    assert FitKey("m1", None) not in cache
    assert FitKey("m0", None) in cache and FitKey("m3", None) in cache
    assert cache.evictions == 1


def test_iteration_is_safe_while_another_thread_writes():
    """run_rca mutates from `asyncio.to_thread` while /meta and _pick_fit read.
    This raised `RuntimeError: dictionary changed size during iteration`,
    most likely while the UI polled a long RCA. Note the app's asyncio.Lock
    could never have fixed it — it serializes coroutines, not threads."""
    cache = TraceCache(max_entries=10_000)
    for i in range(200):
        cache[FitKey(f"seed{i}", None)] = i

    errors = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            cache[FitKey(f"w{i}", None)] = i
            i += 1

    def reader():
        try:
            for _ in range(300):
                list(cache)
                cache.snapshot()
                sorted({k[0] for k in cache})    # what /meta does
        except Exception as exc:                 # noqa: BLE001 - recording it is the test
            errors.append(exc)

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    w.join(timeout=5)

    assert not errors, f"concurrent read/write raised: {errors[:3]}"


def test_max_entries_must_be_positive():
    with pytest.raises(ValueError, match="max_entries must be >= 1"):
        TraceCache(max_entries=0)
