import pytest

from flashtool.firmware.model import LogicalBlock, Segment
from flashtool.sequence.expr import ExprError, evaluate, render_bytes, render_value

pytestmark = [pytest.mark.req("REQ-04"), pytest.mark.req("REQ-14")]


@pytest.fixture
def names():
    block = LogicalBlock("app", [Segment(0x1000, b"\x01\x02\x03\x04")])
    return {"rid": 0xFF00, "block": block, "data": b"\xAA\xBB", "level": 1}


def test_render_bytes_mixes_hex_and_expressions(names):
    out = render_bytes("31 01 ${u16(rid)} ${addr_size(block.address_and_length_format, block.start, block.size)}", names)
    assert out == bytes.fromhex("3101FF00 44 00001000 00000004")


def test_single_byte_ints_and_bytes(names):
    assert render_bytes("27 ${level}", names) == b"\x27\x01"
    assert render_bytes("2E F1 5A ${data}", names) == bytes.fromhex("2EF15AAABB")
    with pytest.raises(ExprError, match="one byte"):
        render_bytes("${rid}", names)


def test_render_value_and_crc(names):
    assert render_value("${block.crc32}", names) == names["block"].crc32
    assert render_value("level is ${level}", names) == "level is 1"
    assert render_value(5, names) == 5


@pytest.mark.parametrize("expr", [
    "__import__('os')",
    "block.__class__",
    "block._private",
    "open('x')",
    "(lambda: 1)()",
    "[x for x in data]",
    "1 << 100000",
    "b'a' * 10000000",
    "block.download_regions",
])
def test_unsafe_expressions_rejected(expr, names):
    with pytest.raises(ExprError):
        evaluate(expr, names)


def test_unknown_name(names):
    with pytest.raises(ExprError, match="unknown name"):
        evaluate("missing + 1", names)


def test_comparisons_and_conditionals(names):
    assert evaluate("level == 1 and data[0] == 0xAA", names) is True
    assert evaluate("'x' if level > 1 else 'y'", names) == "y"
    assert evaluate("data is not None", names) is True
