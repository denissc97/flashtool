"""Restricted expression evaluator for templates (no Python eval).

Templates can come from third parties, so expressions are interpreted from the AST
with a whitelist of node types, functions and attribute owners.
"""

from __future__ import annotations

import ast
import datetime
import operator
import re
import zlib
from collections.abc import Mapping
from typing import Any

from ..firmware.model import FirmwareImage, LogicalBlock, Segment, SourceFile, normalize_block_type
from ..uds.client import encode_address_and_size


class ExprError(Exception):
    pass


MAX_INT_BITS = 4096
MAX_SEQ_LEN = 1 << 20


def _int_bytes(n: int):
    def conv(x: Any) -> bytes:
        x = int(x)
        if not 0 <= x < (1 << (8 * n)):
            raise ExprError(f"value 0x{x:X} does not fit in {n} byte(s)")
        return x.to_bytes(n, "big")
    return conv


def _be(x: Any, n: int) -> bytes:
    return _int_bytes(int(n))(x)


def _le(x: Any, n: int) -> bytes:
    return _int_bytes(int(n))(x)[::-1]


def _hexbytes(s: str) -> bytes:
    return bytes.fromhex(str(s).replace(" ", ""))


def _date_bcd() -> bytes:
    """Today as BCD YY MM DD (common fingerprint format)."""
    d = datetime.date.today()
    return bytes(int(f"{v:02d}", 16) for v in (d.year % 100, d.month, d.day))


def _of_type(blocks: Any, *types: str) -> list:
    """Blocks of the given type(s), e.g. of_type(image.blocks, 'BOOT')."""
    wanted = {normalize_block_type(t) for t in types}
    return [b for b in blocks if getattr(b, "type", None) in wanted]


def _to_int(b: Any) -> int:
    if isinstance(b, (bytes, bytearray)):
        return int.from_bytes(b, "big")
    return int(b)


FUNCTIONS: dict[str, Any] = {
    "u8": _int_bytes(1),
    "u16": _int_bytes(2),
    "u24": _int_bytes(3),
    "u32": _int_bytes(4),
    "be": _be,
    "le": _le,
    "hexbytes": _hexbytes,
    "hex": lambda b: bytes(b).hex().upper() if isinstance(b, (bytes, bytearray)) else f"0x{int(b):X}",
    "int": _to_int,
    "len": len,
    "min": min,
    "max": max,
    "crc32": lambda b: zlib.crc32(bytes(b)) & 0xFFFFFFFF,
    "addr_size": lambda alfid, address, size: encode_address_and_size(int(alfid), int(address), int(size)),
    "date_bcd": _date_bcd,
    "of_type": _of_type,
    "str": str,
}

ATTRIBUTE_OWNERS = (LogicalBlock, Segment, FirmwareImage, SourceFile)

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.BitAnd: operator.and_, ast.BitOr: operator.or_, ast.BitXor: operator.xor,
    ast.LShift: operator.lshift, ast.RShift: operator.rshift,
}
_UNARY = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert, ast.Not: operator.not_}
_CMP = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge, ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b, ast.Is: operator.is_, ast.IsNot: operator.is_not,
}


def _check_size(value: Any) -> Any:
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise ExprError("integer result too large")
    if isinstance(value, (bytes, bytearray, str, list, tuple)) and len(value) > MAX_SEQ_LEN:
        raise ExprError("result too large")
    return value


class _Evaluator:
    def __init__(self, names: Mapping[str, Any]):
        self.names = names

    def eval(self, node: ast.AST) -> Any:
        method = getattr(self, f"_{type(node).__name__}", None)
        if method is None:
            raise ExprError(f"'{type(node).__name__}' is not allowed in expressions")
        return _check_size(method(node))

    def _Expression(self, n: ast.Expression) -> Any:
        return self.eval(n.body)

    def _Constant(self, n: ast.Constant) -> Any:
        if not isinstance(n.value, (int, str, bytes, bool, type(None))):
            raise ExprError(f"constant {n.value!r} is not allowed")
        return n.value

    def _Name(self, n: ast.Name) -> Any:
        if n.id in self.names:
            return self.names[n.id]
        if n.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[n.id]
        raise ExprError(f"unknown name '{n.id}'")

    def _Attribute(self, n: ast.Attribute) -> Any:
        if n.attr.startswith("_"):
            raise ExprError(f"attribute '{n.attr}' is not allowed")
        obj = self.eval(n.value)
        if isinstance(obj, Mapping):
            if n.attr in obj:
                return obj[n.attr]
            raise ExprError(f"no field '{n.attr}'")
        if not isinstance(obj, ATTRIBUTE_OWNERS):
            raise ExprError(f"attributes of {type(obj).__name__} are not accessible")
        try:
            value = getattr(obj, n.attr)
        except AttributeError:
            raise ExprError(f"{type(obj).__name__} has no attribute '{n.attr}'") from None
        if callable(value):
            raise ExprError(f"'{n.attr}' is a method, not a value")
        return value

    def _Subscript(self, n: ast.Subscript) -> Any:
        obj = self.eval(n.value)
        if not isinstance(obj, (bytes, bytearray, str, list, tuple, Mapping)):
            raise ExprError(f"{type(obj).__name__} is not indexable")
        key = self.eval(n.slice)
        try:
            return obj[key]
        except (IndexError, KeyError, TypeError) as exc:
            raise ExprError(f"index error: {exc}") from None

    def _Slice(self, n: ast.Slice) -> slice:
        return slice(*(self.eval(x) if x is not None else None for x in (n.lower, n.upper, n.step)))

    def _Tuple(self, n: ast.Tuple) -> tuple:
        return tuple(self.eval(e) for e in n.elts)

    def _List(self, n: ast.List) -> list:
        return [self.eval(e) for e in n.elts]

    def _Call(self, n: ast.Call) -> Any:
        if not isinstance(n.func, ast.Name) or n.func.id not in FUNCTIONS:
            name = n.func.id if isinstance(n.func, ast.Name) else "?"
            raise ExprError(f"function '{name}' is not allowed (allowed: {', '.join(sorted(FUNCTIONS))})")
        if n.keywords:
            raise ExprError("keyword arguments are not allowed")
        try:
            return FUNCTIONS[n.func.id](*(self.eval(a) for a in n.args))
        except ExprError:
            raise
        except Exception as exc:
            raise ExprError(f"{n.func.id}(): {exc}") from None

    def _BinOp(self, n: ast.BinOp) -> Any:
        op = _BINOPS.get(type(n.op))
        if op is None:
            raise ExprError(f"operator {type(n.op).__name__} is not allowed")
        left, right = self.eval(n.left), self.eval(n.right)
        if isinstance(n.op, ast.LShift) and isinstance(right, int) and right > MAX_INT_BITS:
            raise ExprError("shift too large")
        if isinstance(n.op, ast.Mult) and any(isinstance(v, (bytes, str, list, tuple)) for v in (left, right)):
            count = right if isinstance(right, int) else left
            if isinstance(count, int) and count > MAX_SEQ_LEN:
                raise ExprError("repetition too large")
        try:
            return op(left, right)
        except Exception as exc:
            raise ExprError(str(exc)) from None

    def _UnaryOp(self, n: ast.UnaryOp) -> Any:
        return _UNARY[type(n.op)](self.eval(n.operand))

    def _BoolOp(self, n: ast.BoolOp) -> Any:
        if isinstance(n.op, ast.And):
            result: Any = True
            for v in n.values:
                result = self.eval(v)
                if not result:
                    return result
            return result
        result = False
        for v in n.values:
            result = self.eval(v)
            if result:
                return result
        return result

    def _Compare(self, n: ast.Compare) -> bool:
        left = self.eval(n.left)
        for op, comp in zip(n.ops, n.comparators):
            right = self.eval(comp)
            if not _CMP[type(op)](left, right):
                return False
            left = right
        return True

    def _IfExp(self, n: ast.IfExp) -> Any:
        return self.eval(n.body) if self.eval(n.test) else self.eval(n.orelse)


def evaluate(expr: str, names: Mapping[str, Any]) -> Any:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"syntax error in '{expr}': {exc.msg}") from None
    try:
        return _Evaluator(names).eval(tree)
    except ExprError as exc:
        raise ExprError(f"{exc} (in '{expr}')") from None


_PLACEHOLDER = re.compile(r"\$\{(.+?)\}")


def render_value(value: Any, names: Mapping[str, Any]) -> Any:
    """`"${expr}"` -> evaluated object; text with placeholders -> string; anything else unchanged."""
    if not isinstance(value, str):
        return value
    m = _PLACEHOLDER.fullmatch(value.strip())
    if m:
        return evaluate(m.group(1), names)
    if "${" in value:
        return _PLACEHOLDER.sub(lambda mm: _as_text(evaluate(mm.group(1), names)), value)
    return value


def _as_text(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex(" ").upper()
    return str(v)


def render_bytes(template: Any, names: Mapping[str, Any]) -> bytes:
    """Build request bytes from a hex template like `"31 01 ${u16(rid)} ${data}"`."""
    if isinstance(template, (bytes, bytearray)):
        return bytes(template)
    if isinstance(template, list):
        return b"".join(_one_byte_or_bytes(render_value(t, names), str(t)) for t in template)
    if not isinstance(template, str):
        raise ExprError(f"cannot build bytes from {template!r}")
    out = bytearray()
    pos = 0
    for m in _PLACEHOLDER.finditer(template):
        out += _parse_hex(template[pos : m.start()])
        out += _one_byte_or_bytes(evaluate(m.group(1), names), m.group(1))
        pos = m.end()
    out += _parse_hex(template[pos:])
    return bytes(out)


def _parse_hex(text: str) -> bytes:
    cleaned = text.replace(" ", "").replace("\n", "").replace("\t", "")
    if not cleaned:
        return b""
    if len(cleaned) % 2:
        raise ExprError(f"odd number of hex digits in '{text.strip()}'")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ExprError(f"invalid hex '{text.strip()}'") from None


def _one_byte_or_bytes(v: Any, src: str) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, bool) or v is None:
        raise ExprError(f"'{src}' gives {v!r}, expected bytes")
    if isinstance(v, int):
        if 0 <= v <= 0xFF:
            return bytes([v])
        raise ExprError(f"'{src}' = 0x{v:X} does not fit in one byte; use u16()/u32()/be()")
    raise ExprError(f"'{src}' gives {type(v).__name__}, expected bytes")
