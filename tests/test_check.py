"""`breakdown check`: every refusal `serve` makes before it contacts a provider,
without serving (issue #117 — the C12 rejection was found by restarting a
production server)."""

import pytest

from breakdown import cli
from breakdown.check import run_check

GOOD = """
provider:
  type: mock
metrics:
  - name: revenue
    source: m.revenue
    kind: flow
    grain: day
    parents: [orders]
  - name: orders
    source: m.orders
    kind: flow
    grain: day
"""

# C12: a week-grain rate sliced with a day-grain weight. The parser refuses
# it since 0.2.0; the reporter's tree hit exactly this on upgrade.
C12 = """
provider:
  type: mock
metrics:
  - name: orders
    source: m.orders
    kind: flow
    grain: day
  - name: aov
    source: m.aov
    kind: rate
    grain: week
    dimensions:
      addon_presence: { source: order__addon_presence, weight: orders }
"""

WAREHOUSE_NO_SQL = """
provider:
  type: warehouse
  host: h
  http_path: p
  token: t
metrics:
  - name: revenue
    source: m.revenue
"""

COLD_START_MISSING = """
provider:
  type: none
metrics:
  - name: revenue
    source: m.revenue
    parents: [orders]
  - name: orders
    source: m.orders
"""


def _by_status(results, status):
    return [r for r in results if r.status == status]


def test_a_clean_tree_passes_with_a_one_line_summary(tmp_path):
    tree = tmp_path / "shop.yml"
    tree.write_text(GOOD)
    results = run_check(str(tree))
    assert [r.status for r in results] == ["pass"]
    assert results[0].name == "tree 'shop'"
    assert "2 metrics" in results[0].detail
    assert "grain day" in results[0].detail
    assert "provider 'mock'" in results[0].detail


def test_the_c12_slice_weight_grain_mismatch_is_named(tmp_path):
    tree = tmp_path / "nn.yml"
    tree.write_text(C12)
    results = run_check(str(tree))
    fails = _by_status(results, "fail")
    assert len(fails) == 1 and fails[0].name == "tree 'nn'"
    detail = fails[0].detail
    # The same sentence serve logs: metric, slice, weight, both grains, and
    # what would satisfy it.
    for token in ("'aov'", "'addon_presence'", "'orders'", "'week'", "'day'", "Provide"):
        assert token in detail, detail


def test_a_directory_with_one_bad_tree_fails_and_names_it(tmp_path):
    (tmp_path / "good.yml").write_text(GOOD)
    (tmp_path / "bad.yml").write_text(C12)
    results = run_check(str(tmp_path))
    by_name = {r.name: r for r in results}
    assert by_name["tree 'good'"].status == "pass"
    assert by_name["tree 'bad'"].status == "fail"
    assert "'aov'" in by_name["tree 'bad'"].detail
    # Two trees: serve would pick a default, so check says which.
    assert by_name["default tree"].status == "pass"
    assert "'bad'" in by_name["default tree"].detail  # alphabetically first


def test_a_default_tree_that_is_not_discovered_fails(tmp_path):
    (tmp_path / "good.yml").write_text(GOOD)
    results = run_check(str(tmp_path), default_tree="missing")
    by_name = {r.name: r for r in results}
    assert by_name["default tree"].status == "fail"
    assert "'missing'" in by_name["default tree"].detail
    assert "good" in by_name["default tree"].detail


def test_a_missing_path_is_a_discovery_failure(tmp_path):
    results = run_check(str(tmp_path / "nope.yml"))
    assert [r.status for r in results] == ["fail"]
    assert results[0].name == "tree discovery"
    assert "not found" in results[0].detail


def test_an_empty_directory_is_a_discovery_failure(tmp_path):
    results = run_check(str(tmp_path))
    assert [r.status for r in results] == ["fail"]
    assert "No metric trees found" in results[0].detail


def test_a_pre_fetch_load_refusal_is_reported_as_serve_would(tmp_path):
    """`load_tree` refuses a warehouse tree with no `sql` before it connects;
    a tree that parses clean but cannot load is still a failed check."""
    tree = tmp_path / "wh.yml"
    tree.write_text(WAREHOUSE_NO_SQL)
    results = run_check(str(tree))
    fails = _by_status(results, "fail")
    assert len(fails) == 1
    assert "serve would refuse it at load" in fails[0].detail
    # Either the extra is absent (that is the first refusal serve hits) or
    # the tree's own defect is named — never a pass.
    assert "['revenue']" in fails[0].detail or "extra" in fails[0].detail


def test_a_cold_start_tree_missing_declarations_fails(tmp_path):
    tree = tmp_path / "cold.yml"
    tree.write_text(COLD_START_MISSING)
    results = run_check(str(tree))
    fails = _by_status(results, "fail")
    assert len(fails) == 1
    assert "not cold-start ready" in fails[0].detail


def test_an_unanswered_rate_denominator_warns_rather_than_fails(tmp_path):
    """Serve starts with a warning; doctor fails. Check follows serve's exit
    code and says so."""
    tree = tmp_path / "rate.yml"
    tree.write_text(
        GOOD
        + """
  - name: aov
    source: m.aov
    kind: rate
    grain: day
"""
    )
    results = run_check(str(tree))
    assert not _by_status(results, "fail")
    warns = _by_status(results, "warn")
    assert len(warns) == 1 and "aov" in warns[0].detail
    assert "doctor" in warns[0].detail


def test_the_bundled_examples_pass():
    from importlib.resources import files

    results = run_check(str(files("breakdown").joinpath("examples")))
    assert not _by_status(results, "fail"), [r.detail for r in results]


def test_cli_exit_code_follows_the_report(tmp_path, capsys):
    good = tmp_path / "good.yml"
    good.write_text(GOOD)
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "--tree", str(good)])
    assert exc.value.code == 0
    assert "[PASS] tree 'good'" in capsys.readouterr().out

    bad = tmp_path / "bad.yml"
    bad.write_text(C12)
    with pytest.raises(SystemExit) as exc:
        cli.main(["check", "--tree", str(bad)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "[FAIL] tree 'bad'" in out and "'addon_presence'" in out


def test_check_never_opens_a_connection(tmp_path, monkeypatch):
    """The whole point: a production tree can be checked from anywhere,
    including a box with no warehouse credentials."""
    import breakdown.loading as loading

    def boom(*a, **kw):  # pragma: no cover - the assertion is that it is not called
        raise AssertionError("check built a fetcher")

    monkeypatch.setattr(loading, "build_fetcher", boom)
    monkeypatch.setattr(loading, "fetch_all_metrics", boom)
    (tmp_path / "wh.yml").write_text(WAREHOUSE_NO_SQL)
    (tmp_path / "good.yml").write_text(GOOD)
    run_check(str(tmp_path))
