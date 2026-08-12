"""Optional progress reporting for the long engine calls.

RCA and simulation can spend a minute or more fitting ancestor models, and a
spinner that says "simulating" for the whole of it tells the operator nothing
about whether it is working or wedged. The engine knows the answer — it has the
work list — so it says so.

Two rules keep this from becoming hidden state:

- The callback is **passed in explicitly**, never read from a global. `run_rca`
  and `run_simulation` stay pure functions of their arguments; with no callback
  they behave exactly as before, which is what the test suite exercises.
- Reporting is **advisory and never load-bearing**. `_report` swallows
  exceptions from the callback: a progress consumer that breaks must not be
  able to fail an analysis. Nothing downstream reads what was reported.
"""

from typing import Any, Callable, Dict, Optional

# stage: which phase of the run; other keys are stage-specific (`metric`,
# `current`, `total`). Callers replace the whole dict rather than mutating it —
# see the API's registry, where a worker thread writes what the event loop reads.
ProgressFn = Callable[[Dict[str, Any]], None]


def report(progress: Optional[ProgressFn], **fields: Any) -> None:
    """Send one progress update, if anyone is listening."""
    if progress is None:
        return
    try:
        progress(dict(fields))
    except Exception:  # noqa: BLE001 - progress must never break an analysis
        pass
