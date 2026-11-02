"""算式引擎：以前挂在地址栏上，现在归「计算器」页用。

安全求值走 AST 白名单（不用 eval）。只认数字、四则运算、括号、常用函数，
指数上限 512，算式长度上限 300 —— 够日常用，也不至于把面板算死。

历史记录存在配置的 ``expr_history`` 键下，一条一个 dict：
    {"expr": "1+2*3", "result": "7", "note": "买菜"}
"""

from __future__ import annotations

import ast
import math

_MATH_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max, "sum": sum,
    "sqrt": math.sqrt, "cbrt": lambda x: x ** (1 / 3), "pow": pow,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
}
_MATH_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

_EXPR_MARKS = set("+-*/=×÷＋－")


def looks_like_expression(text: str) -> bool:
    """敲进来的东西像不像算式（含运算符、括号或函数名）。"""
    text = (text or "").strip()
    if not text:
        return False
    if any(ch in _EXPR_MARKS for ch in text):
        return True
    if "(" in text or ")" in text:
        return True
    lowered = text.lower()
    return any(name in lowered for name in ("sqrt", "log", "sin", "cos", "tan", "pi"))


def eval_expression(text: str) -> tuple[str | None, str | None]:
    """求值，返回 ``(结果文本, 失败原因)``，失败时结果为空。"""
    expr = (text or "").strip()
    for src, dst in (("×", "*"), ("÷", "/"), ("，", ","), ("（", "("),
                     ("）", ")"), ("－", "-"), ("＋", "+")):
        expr = expr.replace(src, dst)
    expr = expr.replace("^", "**")
    if not expr or len(expr) > 300:
        return None, "空表达式"

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None, "看不懂这个算式"

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError
            return node.value
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return left + right
            if isinstance(op, ast.Sub):
                return left - right
            if isinstance(op, ast.Mult):
                return left * right
            if isinstance(op, ast.Div):
                return left / right
            if isinstance(op, ast.FloorDiv):
                return left // right
            if isinstance(op, ast.Mod):
                return left % right
            if isinstance(op, ast.Pow):
                if abs(right) > 512:
                    raise ValueError
                return left ** right
            raise ValueError
        if isinstance(node, ast.UnaryOp):
            value = walk(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
            raise ValueError
        if isinstance(node, ast.Name):
            if node.id in _MATH_CONSTS:
                return _MATH_CONSTS[node.id]
            raise ValueError
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _MATH_FUNCS:
                raise ValueError
            args = [walk(a) for a in node.args]
            return _MATH_FUNCS[node.func.id](*args)
        raise ValueError

    try:
        value = walk(tree)
    except ZeroDivisionError:
        return None, "除数是 0"
    except (ValueError, TypeError, OverflowError):
        return None, "看不懂这个算式"

    if isinstance(value, complex):
        return None, "结果是复数"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None, "结果超出范围"
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value)), None
        return f"{value:.10g}", None
    return str(value), None


def format_result(text: str) -> str:
    """给结果加千分位（只处理纯整数，小数原样）。"""
    try:
        number = int(text)
    except (TypeError, ValueError):
        return text or ""
    if abs(number) < 1000:
        return text
    return f"{number:,}"


# ---------------------------------------------------------------- 历史记录
LIMIT = 500


def load_history(config) -> list[dict]:
    """读历史；兼容早期存成字符串的那批数据。"""
    data = config.value("expr_history", [])
    if isinstance(data, str):
        import json
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    # 复制成新 dict：共享对象会让配置层的「值没变就跳过」误判
    return [{**item} for item in data
            if isinstance(item, dict) and item.get("expr") and item.get("result")][:LIMIT]


def save_history(config, records: list[dict]) -> None:
    config.setValue("expr_history", [dict(item) for item in records])


def push_history(records: list[dict], expr: str, result: str) -> list[dict]:
    """把一条结果插到最前面（同样内容不重复），返回新列表。"""
    rest = [item for item in records
            if not (item.get("expr") == expr and item.get("result") == result)]
    return ([{"expr": expr, "result": result}] + rest)[:LIMIT]
