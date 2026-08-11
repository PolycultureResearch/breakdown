"""What ships in the wheel.

`[tool.hatch.build.targets.wheel]` packages the whole `breakdown/` directory, so
anything that lands there is published — including files nobody imported and
nobody meant to add.
"""

import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "breakdown"


def _modules():
    return [p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_editor_duplicate_modules_ship_in_the_wheel():
    # macOS and several editors make copies named `mod 2.py` / `mod copy.py`.
    # One reached main: a stale `dbt_bridge 2.py`, 38 lines behind the real
    # module, swept in by `git add -A` and published in the wheel. Nothing
    # imported it, so nothing failed — which is exactly why it needs a test
    # rather than review.
    suspicious = [
        p.name
        for p in _modules()
        if " " in p.name or p.stem.endswith(("copy", "orig", "bak", "old"))
    ]
    assert not suspicious, f"stray module file(s) in breakdown/: {suspicious}"


def test_every_shipped_module_imports():
    # A duplicate that shadows nothing still ships; one that fails to parse
    # would break `python -m compileall` on install.
    import ast

    for path in _modules():
        ast.parse(path.read_text(), filename=str(path))
