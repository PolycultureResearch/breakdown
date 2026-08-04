"""breakdown: open-source Bayesian metric trees and root-cause analysis.

The distribution is published as `metric-breakdown` (the name `breakdown` is
taken on PyPI); the import package and the CLI stay `breakdown`.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from installed metadata rather than a literal, so `pyproject.toml`
    # stays the single source of truth and `breakdown --version` can never
    # disagree with what pip resolved.
    __version__ = version("metric-breakdown")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
