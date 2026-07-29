"""CLI wiring: flag -> env-var handoff and uvicorn invocation, doctor dispatch."""

import pytest

import breakdown.doctor
from breakdown import cli

TREE = """
provider:
  type: mock
metrics:
  - name: revenue
    source: my.metrics.revenue
"""


@pytest.fixture
def handoff_env():
    """serve() writes real env vars (the reload-subprocess handoff); undo
    them so they don't redefine the window for later lifespan tests."""
    import os

    names = (
        "BREAKDOWN_TREE", "BREAKDOWN_START_DATE", "BREAKDOWN_END_DATE",
        "BREAKDOWN_PORT", "BREAKDOWN_HOST",
    )
    saved = {name: os.environ.get(name) for name in names}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_serve_passes_flags_and_env(tmp_path, monkeypatch, handoff_env):
    import os

    import uvicorn

    tree = tmp_path / "tree.yml"
    tree.write_text(TREE)
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw, app=app))

    cli.main([
        "serve", "--host", "0.0.0.0", "--port", "1234", "--tree", str(tree),
        "--start-date", "2024-01-01", "--end-date", "2024-02-01",
    ])

    assert captured["app"] == "breakdown.api.main:app"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 1234
    assert captured["reload"] is False
    assert os.environ["BREAKDOWN_TREE"] == str(tree)
    assert os.environ["BREAKDOWN_START_DATE"] == "2024-01-01"
    assert os.environ["BREAKDOWN_END_DATE"] == "2024-02-01"
    assert os.environ["BREAKDOWN_PORT"] == "1234"
    assert os.environ["BREAKDOWN_HOST"] == "0.0.0.0"


def test_serve_reload_flag(monkeypatch, handoff_env):
    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    cli.main(["serve", "--reload"])
    assert captured["reload"] is True
    assert captured["host"] == "127.0.0.1"


def test_serve_rejects_bad_date(handoff_env):
    with pytest.raises(SystemExit, match="valid YYYY-MM-DD"):
        cli.main(["serve", "--start-date", "not-a-date"])


def test_serve_rejects_missing_tree(handoff_env):
    with pytest.raises(SystemExit, match="not found"):
        cli.main(["serve", "--tree", "/nope/does_not_exist.yml"])


def test_doctor_dispatch_exits_with_report_code(monkeypatch):
    monkeypatch.setattr(breakdown.doctor, "run_doctor", lambda tree, **kw: [])
    monkeypatch.setattr(breakdown.doctor, "print_report", lambda results: 1)
    with pytest.raises(SystemExit) as exc:
        cli.main(["doctor", "--tree", "whatever.yml"])
    assert exc.value.code == 1
