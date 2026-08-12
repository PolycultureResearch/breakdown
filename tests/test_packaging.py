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


def _extras():
    import tomllib

    with open(PACKAGE.parent / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)["project"]["optional-dependencies"]


def test_the_all_extra_degrades_on_314_rather_than_failing():
    # Every `dbt-metricflow` release caps at `<3.14`, as does `dbt-sl-sdk`, so
    # an unmarked `all` is unresolvable there — pip refuses the *whole* install
    # rather than the one extra it cannot satisfy, denying a 3.14 user
    # `databricks` and `dbt-bridge`, which work fine.
    marked = [d for d in _extras()["all"] if "python_version < '3.14'" in d]
    assert len(marked) == 2, "the dbt deps in `all` must stay marked"


def test_the_duplicated_dbt_deps_in_all_match_the_dbt_extra():
    # `all` spells the dbt deps out instead of referencing
    # `metric-breakdown[dbt]`, because hatchling flattens self-referencing
    # extras at build time and *drops the marker* — the metadata came out as a
    # bare `dbt-metricflow>=0.13.0; extra == 'all'` and 3.14 still failed. That
    # duplication is the cost, so it is pinned rather than trusted.
    extras = _extras()
    in_all = {d.split(";")[0].strip() for d in extras["all"] if "python_version" in d}
    assert in_all == set(extras["dbt"]), (
        "the marked dbt deps in `all` have drifted from the `dbt` extra"
    )


def test_extras_that_work_on_314_are_not_marked():
    # `bigquery`, `databricks` and `dbt-bridge` install on 3.14; marking them
    # would deny them to the users who can actually use them.
    unmarked = [d for d in _extras()["all"] if "python_version" not in d]
    assert unmarked == ["metric-breakdown[bigquery,databricks,dbt-bridge]"]
