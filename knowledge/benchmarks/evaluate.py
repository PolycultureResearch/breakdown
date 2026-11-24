"""Score breakdown against a world set of generated companies.

The runner of `knowledge/evaluation_spec.md` (see its "How to run" section):

    uv run python knowledge/benchmarks/evaluate.py run \
        --worlds /path/to/worldset --tree demo/white_cube_tree.yml \
        --tier smoke --out /path/outside/the/repo/run

The code is the `evalrunner` package beside this file.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))  # the repo, for `breakdown`
sys.path.insert(0, HERE)

from evalrunner.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
