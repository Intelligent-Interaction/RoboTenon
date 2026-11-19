"""Restricted graph expressions; no calls or arbitrary evaluation."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from enum import Enum
from functools import lru_cache
from typing import Any

from ..contracts.context import GraphContext


def condition_node_references(expression):
    """Only statically named results/result_versions in the current graph."""
    if expression is None or not expression.strip():
        return set()
    references = set()
    for node in ast.walk(parse_condition(expression)):
        base = node.value if isinstance(node, (ast.Subscript, ast.Attribute)) else None
        if not (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                and base.value.id == "ctx" and base.attr in {"results", "result_versions"}):
            continue
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            references.add(node.slice.value)
        elif isinstance(node, ast.Attribute):
            references.add(node.attr)
    return references


def validate_condition_references(expression, nodes, location):
    unknown = condition_node_references(expression) - set(nodes)
    if unknown:
        raise ValueError(f"{location}: condition references unknown result nodes {sorted(unknown)}")


@lru_cache(maxsize=512)
def parse_condition(expression):
    tree = ast.parse(expression, mode="eval")
    allowed = (
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Subscript,
        ast.List,
        ast.Tuple,
        ast.Set,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Gt,
        ast.GtE,
        ast.Lt,
        ast.LtE,
        ast.In,
        ast.NotIn,
    )
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError(f"unsupported expression node {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in {
            "ctx",
            "true",
            "false",
            "null",
        }:
            raise ValueError(f"unknown name {node.id!r}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("private attributes are not available in graph conditions")
    return tree


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _lookup(base: Any, key: Any) -> Any:
    base = _enum_value(base)
    if isinstance(base, Mapping):
        return base.get(key)
    if isinstance(base, (list, tuple)) and isinstance(key, int):
        return base[key]
    return getattr(base, str(key), None)


class _ConditionEvaluator:
    """Small safe expression evaluator for graph edge conditions.

    Only literals, ``ctx`` lookups, boolean operators and comparisons are
    accepted. Calls, comprehensions and arbitrary Python execution are not.
    """

    def __init__(self, context: GraphContext):
        self.context = context

    def evaluate(self, expression: str | None) -> bool:
        if expression is None or not expression.strip():
            return True
        try:
            tree = parse_condition(expression)
            return bool(self._eval(tree.body))
        except (SyntaxError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise ValueError(
                f"invalid graph edge condition {expression!r}: {exc}"
            ) from exc

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "ctx":
                return self.context
            if node.id == "true":
                return True
            if node.id == "false":
                return False
            if node.id == "null":
                return None
            raise ValueError(f"unknown name {node.id!r}")
        if isinstance(node, ast.Attribute):
            return _lookup(self._eval(node.value), node.attr)
        if isinstance(node, ast.Subscript):
            base = self._eval(node.value)
            key_node = node.slice
            key = self._eval(key_node)
            return _lookup(base, key)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self._eval(item) for item in node.elts]
            if isinstance(node, ast.List):
                return values
            if isinstance(node, ast.Tuple):
                return tuple(values)
            return set(values)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(self._eval(item)) for item in node.values)
            if isinstance(node.op, ast.Or):
                return any(bool(self._eval(item)) for item in node.values)
            raise ValueError("unsupported boolean operator")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(self._eval(node.operand))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = self._eval(node.operand)
            return -value if isinstance(node.op, ast.USub) else +value
        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for operator, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator)
                a, b = _enum_value(left), _enum_value(right)
                if isinstance(operator, ast.Eq):
                    result = a == b
                elif isinstance(operator, ast.NotEq):
                    result = a != b
                elif isinstance(operator, ast.Gt):
                    result = a > b
                elif isinstance(operator, ast.GtE):
                    result = a >= b
                elif isinstance(operator, ast.Lt):
                    result = a < b
                elif isinstance(operator, ast.LtE):
                    result = a <= b
                elif isinstance(operator, ast.In):
                    result = a in b
                elif isinstance(operator, ast.NotIn):
                    result = a not in b
                else:
                    raise ValueError("unsupported comparison operator")
                if not result:
                    return False
                left = right
            return True
        raise ValueError(f"unsupported expression node {type(node).__name__}")


def evaluate_condition(expression: str | None, context: GraphContext) -> bool:
    """Evaluate one JSON edge condition against the shared context."""
    return _ConditionEvaluator(context).evaluate(expression)
