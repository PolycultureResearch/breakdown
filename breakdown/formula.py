"""Shared helpers for validating and evaluating metric formulas.

Formulas are restricted Python arithmetic expressions: only +, -, *, /, **,
unary +/-, numeric constants, and bare names are allowed. Everything else
(calls, attributes, subscripts, comprehensions) is rejected before eval.
"""
import ast
from typing import Dict, Set

import numpy as np

ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.Name, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
    ast.UnaryOp, ast.USub, ast.UAdd,
    ast.Load,  # context node on Name nodes
)


def _parse(formula: str) -> ast.Expression:
    try:
        return ast.parse(formula, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid formula syntax: {e}") from e


def validate_formula(formula: str) -> None:
    tree = _parse(formula)
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_NODES):
            raise ValueError(f"Formula contains unsupported operation: {type(node).__name__}")


def referenced_names(formula: str) -> Set[str]:
    return {node.id for node in ast.walk(_parse(formula)) if isinstance(node, ast.Name)}


def eval_formula(formula: str, values: Dict[str, np.ndarray]) -> np.ndarray:
    validate_formula(formula)
    return eval(formula, {"__builtins__": {}}, values)  # noqa: S307
