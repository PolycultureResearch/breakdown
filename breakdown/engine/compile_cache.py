"""Make PyTensor's on-disk numba cache safe to keep between processes.

**The defect this exists for.** PyTensor compiles every function on its numba
backend from generated source, so they all carry the same few names
(`numba_funcified_fgraph`, `elemwise`, `subtensor`, ...). numba tells
same-named functions apart in compiled code by a `v<uid>` tag in the symbol
name, where `uid` is a counter that starts at 1 in every process. PyTensor
then caches the compiled code on disk, symbol names included, under a key
that does not contain the uid. So two processes that each compile a
*different* graph first write two cache entries whose top-level symbols have
the same name. A later process that loads both gets one definition for that
name (the symbols are weak, `linkonce_odr`; the first one loaded wins), and
the second function silently runs the first one's code.

Measured on release 0.3.0 from an empty cache: start a process and fit node
A; start another and fit node B; a third that fits A then B returns **A's
posterior for B, to four digits, with R-hat 1.00 and no divergences**. Where
the two models differ in shape it is an `IndexError` or a non-finite
posterior instead. The two writers must also agree on a second tag that
depends on Python's string hash seed, which happens for about 1 pair of
processes in 80 by chance and for every pair when `PYTHONHASHSEED` is fixed.
A cache that many processes have written to (a test suite, a container that
restarts, a persisted volume) accumulates such pairs.

**What this does.** Three things, once per process, before anything is
compiled:

1. Replaces numba's function counter with random 128-bit ids, so no two
   processes (and no two forked chains, which would inherit a counter's
   state) hand out the same uid. Cached code written by different processes
   then never shares a symbol name. This is the fix PyTensor itself ships
   from 2.38.3 (`pytensor.link.numba.cache._RandomUidIter`); the lock is on
   2.38.2, and the base install admits it. On a PyTensor that already does
   this, the step is skipped and the other two still run.
2. Points the cache at its own directory beside PyTensor's
   (`<base_compiledir>/numba-unique-ids`), because entries written before
   this, or by any program on an older PyTensor, still carry low uids and two
   of *those* can collide inside this process. Measured: PyTensor 2.38.3
   reading a cache 2.38.2 wrote returns the wrong function exactly as 2.38.2
   does. Upgrading PyTensor does not clean a cache; not reading it does.
3. Turns the cache off for any Python process started from this one that
   does not go through this module (a `spawn` child, a forkserver started
   without the preload), through `PYTENSOR_FLAGS`. Such a process compiles
   what it needs and reads and writes nothing shared.

If numba's internals are not what step 1 expects, the cache is turned off
for this process instead and a warning says so: a slower fit, never a wrong
one.

A user who sets `numba__cache=False` in `PYTENSOR_FLAGS` themselves is left
alone.

This is process-wide state, which the engine otherwise avoids. It has to be:
the thing being protected is a symbol table shared by every compiled
function in the process.
"""

import itertools
import logging
import os
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

#: The cache directory hardened processes use, beside PyTensor's own `numba/`.
CACHE_DIRNAME = "numba-unique-ids"

#: Set in the environment once this process has rewritten `PYTENSOR_FLAGS`,
#: so a child that *does* import this module can tell "the cache is off
#: because my parent turned it off for unhardened children" from "the user
#: turned it off".
_MARKER = "BREAKDOWN_NUMBA_CACHE_HARDENED"

#: "hardened" | "disabled" | "user_disabled" once `ensure_safe_compile_cache`
#: has run in this process; None before.
_state: Optional[str] = None


class _RandomFunctionIds:
    """An iterator of random 128-bit integers, in place of numba's counter.

    numba reads the counter only through `next()`, so any iterator of
    distinct ints serves. `uuid4` draws from the OS on every call, which makes
    it safe across `fork` with no re-seeding: a forked chain that inherited a
    counter would hand out the ids its siblings and its parent hand out next.
    """

    def __iter__(self) -> "_RandomFunctionIds":
        return self

    def __next__(self) -> int:
        return uuid.uuid4().int


def _ids_are_sequential() -> bool:
    """Whether numba's function ids are still its own per-process counter.

    Raises if numba no longer has the attribute at all, so the caller falls
    back to no cache rather than assuming.
    """
    from numba.core.bytecode import FunctionIdentity

    return isinstance(FunctionIdentity._unique_ids, itertools.count)


def _randomize_function_ids() -> None:
    from numba.core.bytecode import FunctionIdentity

    FunctionIdentity._unique_ids = _RandomFunctionIds()


def _disable_for_other_children() -> None:
    """Cache off for Python processes started from here that never import this."""
    flags = [f for f in os.environ.get("PYTENSOR_FLAGS", "").split(",") if f.strip()]
    flags = [f for f in flags if not f.strip().startswith("numba__cache=")]
    os.environ["PYTENSOR_FLAGS"] = ",".join([*flags, "numba__cache=False"])
    os.environ[_MARKER] = "1"


def ensure_safe_compile_cache() -> str:
    """Harden the numba cache for this process; idempotent. Returns the state.

    `"hardened"`: unique function ids, own cache directory, cache on.
    `"disabled"`: hardening failed, cache off for this process.
    `"user_disabled"`: the user had the cache off already; nothing changed.
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

        # False on a PyTensor that already replaced the counter (2.38.3 on).
        if _ids_are_sequential():
            _randomize_function_ids()
        path = config.base_compiledir / CACHE_DIRNAME
        path.mkdir(parents=True, exist_ok=True)
        # Read at call time by PyTensor's cache locator, so this redirects
        # every lookup made after it.
        if not hasattr(pt_cache, "NUMBA_CACHE_PATH"):
            raise AttributeError("pytensor.link.numba.cache has no NUMBA_CACHE_PATH")
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
