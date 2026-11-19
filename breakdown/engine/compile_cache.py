"""Keep one fit from ever running another fit's compiled code (roadmap C65).

**The defect.** PyTensor compiles every function on its numba backend from
generated source, so they all carry the same few names
(`numba_funcified_fgraph`, `elemwise`, `subtensor`, ...). numba tells
same-named functions apart in compiled code by a `v<uid>` tag in the symbol
name. Through PyTensor 2.38.2 that `uid` was numba's own counter, which
starts at 1 in every process, and PyTensor cached the compiled code on disk,
symbol names included, under a key that does not contain it. So two processes
that each compiled a *different* graph first wrote two cache entries whose
symbols have the same name. A later process that loads both keeps one
definition for the name (the symbols are weak, `linkonce_odr`; the first one
loaded wins), and the second function silently runs the first one's code.

Measured on release 0.3.0 (PyTensor 2.38.2) from an empty cache: start a
process and fit node A; start another and fit node B; a third that fits A
then B returns **A's posterior for B, to four digits, with R-hat 1.00 and no
divergences**. Where the two models differ in shape it is an `IndexError` or
a non-finite posterior instead. The two writers must also agree on a second
tag that depends on Python's string hash seed, which happens for about 1 pair
of processes in 80 by chance and for every pair when `PYTHONHASHSEED` is
fixed. A cache many processes have written to (a test suite, a container
that restarts, a persisted volume) accumulates such pairs.

**What fixes it, and what this module is left to do.** PyTensor 2.38.3 draws
the uid at random, and `pyproject.toml` requires it. That stops bad entries
being *written*. It does nothing about the ones already on disk: measured,
2.38.3 reading a cache that 2.38.2 wrote returns the wrong function exactly
as 2.38.2 does. An upgraded install with a `~/.pytensor` (or a mounted
volume) from before is therefore still exposed, and nothing tells it so.
This module closes that, by never reading what an older PyTensor wrote.

**Everything this module changes outside itself.** All of it is process-wide,
which the engine otherwise avoids; the thing being protected is a symbol
table shared by every compiled function in the process, so there is no
narrower place to stand. Each is applied once, by
`ensure_safe_compile_cache`, and never undone:

==============================================  ==============================
What is changed                                 Why
==============================================  ==============================
`pytensor.link.numba.cache.NUMBA_CACHE_PATH`    The directory PyTensor's cache
(a private module global) is set to             locator reads at call time.
`<base_compiledir>/numba-unique-ids`.           Pointing it at a directory
                                                only fixed writers have used
                                                is what keeps old entries,
                                                and any other program's,
                                                from being loaded.
`os.environ["PYTENSOR_FLAGS"]` gains            A Python process started from
`numba__cache=False` (other flags are kept).    this one that never imports
                                                this module (a `spawn` chain,
                                                a forkserver without the
                                                preload) would use PyTensor's
                                                own `numba/` directory, old
                                                entries included. With the
                                                flag it compiles what it
                                                needs and reads and writes
                                                nothing shared.
`os.environ["BREAKDOWN_NUMBA_CACHE_HARDENED"]`  So a child that *does* import
is set to `"1"`.                                this module can tell "the
                                                cache is off because my
                                                parent turned it off for
                                                unhardened children" from
                                                "the user turned it off".
`pytensor.config.numba__cache` is set `True`    The public config switch.
after the redirect (or `False`, see below).     `True` in a child that
                                                inherited the flag above and
                                                has now redirected; `False`
                                                only on the fallback path.
The multiprocessing forkserver's preload list   Set in
gains `breakdown.engine.compile_cache_preload`  `model.preload_forkserver_modules`,
(not here; listed for completeness).            so chains forked from the
                                                server run this module and
                                                keep their cache.
==============================================  ==============================

One private attribute is *read* and not written:
`numba.core.bytecode.FunctionIdentity._unique_ids`. If it is still numba's
sequential counter, the installed PyTensor is older than the floor (or has
stopped replacing it), nothing this process compiles is safe to cache, and
the cache is turned off for the process with a warning: a slower fit, never a
wrong one. The same fallback applies if either private attribute is missing.

A user who sets `numba__cache=False` in `PYTENSOR_FLAGS` themselves is left
alone, and nothing above is applied.
"""

import itertools
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: The cache directory hardened processes use, beside PyTensor's own `numba/`.
CACHE_DIRNAME = "numba-unique-ids"

#: See the table in the module docstring.
_MARKER = "BREAKDOWN_NUMBA_CACHE_HARDENED"

#: "hardened" | "disabled" | "user_disabled" once `ensure_safe_compile_cache`
#: has run in this process; None before.
_state: Optional[str] = None


def _function_ids_are_unique_across_processes() -> bool:
    """Whether numba's function ids have been replaced by PyTensor (2.38.3 on).

    numba's own are an `itertools.count` that starts at 1 in every process.
    Raises if the attribute is gone, so the caller falls back to no cache
    rather than assuming.
    """
    import pytensor.link.numba.cache  # noqa: F401  (the import is what replaces them)
    from numba.core.bytecode import FunctionIdentity

    return not isinstance(FunctionIdentity._unique_ids, itertools.count)


def _disable_for_other_children() -> None:
    """Cache off for Python processes started from here that never import this."""
    flags = [f for f in os.environ.get("PYTENSOR_FLAGS", "").split(",") if f.strip()]
    flags = [f for f in flags if not f.strip().startswith("numba__cache=")]
    os.environ["PYTENSOR_FLAGS"] = ",".join([*flags, "numba__cache=False"])
    os.environ[_MARKER] = "1"


def ensure_safe_compile_cache() -> str:
    """Apply the changes listed in the module docstring; idempotent.

    Returns the state: `"hardened"` (own cache directory, cache on),
    `"disabled"` (could not be made safe, cache off for this process) or
    `"user_disabled"` (the user had the cache off already; nothing changed).
    """
    global _state
    if _state is not None:
        return _state

    import pytensor

    config = pytensor.config
    inherited = os.environ.get(_MARKER) == "1"
    if not config.numba__cache and not inherited:
        _state = "user_disabled"
        return _state

    _disable_for_other_children()
    try:
        import pytensor.link.numba.cache as pt_cache

        if not _function_ids_are_unique_across_processes():
            raise RuntimeError(
                "numba's function ids are its own per-process counter, so the "
                "installed PyTensor is older than 2.38.3 or no longer replaces them"
            )
        if not hasattr(pt_cache, "NUMBA_CACHE_PATH"):
            raise AttributeError("pytensor.link.numba.cache has no NUMBA_CACHE_PATH")
        path = config.base_compiledir / CACHE_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        pt_cache.NUMBA_CACHE_PATH = path
        config.numba__cache = True
        _state = "hardened"
    except Exception as exc:
        config.numba__cache = False
        _state = "disabled"
        logger.warning(
            "PyTensor's on-disk numba cache could not be made safe to share between "
            "processes (%s: %s), so it is turned off for this process. Fits are "
            "correct and each one compiles from scratch, which is slower. See "
            "breakdown/engine/compile_cache.py.",
            type(exc).__name__,
            exc,
        )
    return _state
