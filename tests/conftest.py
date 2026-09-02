"""Session hooks for the test suite.

``BREAKDOWN_TEST_SAMPLE_KW`` — **measurement only, unset in normal use.** A
JSON object merged into every ``pm.sample(...)`` call the engine makes, e.g.
``'{"mp_ctx": "fork"}'``. It exists so a CI experiment can time a sampler
process configuration without a code change per variant, and it changes no
test's expectations: the posterior is the same whichever process model draws
it. Unset, this file does nothing.
"""

import json
import os

_extra = os.environ.get("BREAKDOWN_TEST_SAMPLE_KW")
if _extra:
    import pymc as pm

    _kwargs = json.loads(_extra)
    _sample = pm.sample

    def _sample_with_overrides(*args, **kwargs):
        return _sample(*args, **{**kwargs, **_kwargs})

    pm.sample = _sample_with_overrides
