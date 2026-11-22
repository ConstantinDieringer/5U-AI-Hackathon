"""Bounded, unit-aware expressions over real prices; no generated or executable code."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.agents.case_models import Citation, ToolError, Workspace
from app.agents.case_sources import cited
from app.pricing.amounts import AMOUNT, parse_decimal


@dataclass(frozen=True)
class Value:
    value: Decimal | bool
    unit: str


def evaluate(
    expression: object, values: dict[str, Value], workspace: Workspace, *, depth: int = 0
) -> Value:
    if depth > 10 or not isinstance(expression, dict):
        raise ToolError("Expression is invalid or exceeds the depth limit")
    if set(expression) == {"ref"}:
        key = expression["ref"]
        if not isinstance(key, str) or key not in values:
            raise ToolError("Expression references an unknown priced value")
        return values[key]
    if set(expression) == {"bool"} and type(expression["bool"]) is bool:
        return Value(expression["bool"], "bool")
    if set(expression) == {"value", "unit", "evidence"}:
        passage = cited(workspace, [Citation.model_validate(expression["evidence"])])
        if not isinstance(expression["value"], str):
            raise ToolError("Expression constants must be decimal strings")
        try:
            number = Decimal(expression["value"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ToolError("Expression constant must be a finite decimal string") from exc
        if not number.is_finite() or abs(number) > Decimal(1000000000):
            raise ToolError("Expression constant exceeds the numeric limit")
        unit = expression["unit"]
        if unit == "EUR":
            amounts = []
            for match in AMOUNT.finditer(passage):
                currency = (match["cur1"] or match["cur2"]).upper()
                if currency in ("EUR", "€", "EURO", "EUROS"):
                    amounts.append(parse_decimal(match["num1"] or match["num2"]))
            supported = number in amounts
        elif unit in ("days", "scalar"):
            supported = any(
                parse_decimal(token) == number for token in re.findall(r"\d+(?:[.,]\d+)?", passage)
            )
        else:
            raise ToolError("Expression constants support EUR, days or dimensionless scalar")
        if not supported:
            raise ToolError("Expression constant is not evidenced in its original units")
        return Value(number, unit)
    if set(expression) != {"op", "args"} or not isinstance(expression["args"], list):
        raise ToolError("Use ref, evidenced value, bool or op/args expression nodes")
    op, args = expression["op"], expression["args"]
    if not 1 <= len(args) <= 10:
        raise ToolError("Expression operators need 1 to 10 arguments")
    operands = [evaluate(arg, values, workspace, depth=depth + 1) for arg in args]
    if op in ("and", "or", "not"):
        if any(value.unit != "bool" for value in operands) or (op == "not" and len(operands) != 1):
            raise ToolError("Boolean operators require boolean arguments")
        result = (
            all(value.value for value in operands)
            if op == "and"
            else any(value.value for value in operands)
            if op == "or"
            else not operands[0].value
        )
        return Value(result, "bool")
    if len(operands) != 2 or any(value.unit == "bool" for value in operands):
        raise ToolError("Numeric operators require exactly two numeric arguments")
    left, right = operands
    a, b = left.value, right.value
    assert isinstance(a, Decimal) and isinstance(b, Decimal)
    if op in ("eq", "ne", "lt", "le", "gt", "ge", "add", "sub"):
        if left.unit != right.unit:
            raise ToolError("Cannot combine quantities with different units")
        if op == "add":
            return Value(a + b, left.unit)
        if op == "sub":
            return Value(a - b, left.unit)
        return Value(
            {"eq": a == b, "ne": a != b, "lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op],
            "bool",
        )
    if op == "mul" and "scalar" in (left.unit, right.unit):
        return Value(a * b, right.unit if left.unit == "scalar" else left.unit)
    if op == "div" and b != 0 and (left.unit == right.unit or right.unit == "scalar"):
        return Value(a / b, "scalar" if left.unit == right.unit else left.unit)
    raise ToolError("Unsupported operation, invalid units or division by zero")
