"""VBF (Volvo Binary Format) loading (flashtool/firmware/vbf.py).

Files are built here with an independent bitwise CRC-16/CCITT-FALSE and zlib's CRC-32,
following the public descriptions cited in flashtool/firmware/vbf.py.
"""

import struct
import zlib

import pytest

from flashtool.app.job import run_job
from flashtool.cli import main
from flashtool.firmware.loaders import FirmwareError, FirmwareSpec, detect_format, load_firmware
from flashtool.firmware.validate import validate_image
from flashtool.firmware.vbf import crc16_ccitt_false, crc32, parse_vbf
from flashtool.sequence.events import JobFinished

pytestmark = [pytest.mark.req("REQ-01")]


def crc16_reference(data: bytes) -> int:
    """Bitwise CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, MSB first, no final XOR."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
    return crc


EXE_FIELDS = {
    "sw_part_number": '"32290001"',
    "sw_version": '"AA"',
    "sw_part_type": "EXE",
    "data_format_identifier": "0x00",
    "ecu_address": "0x1A01",
    "erase": "{ { 0x00010000, 0x00010000 },\r\n                 { 0x00030000, 0x00001000 }\r\n               }",
}

BANNER = """    //**********************************************************
    //*        Generated for FlashTool tests - DO NOT EDIT !
    //**********************************************************
"""


def build_vbf(blocks, fields=None, *, version="2.6", checksum=None, crc16=None, banner=BANNER) -> bytes:
    """blocks: [(address, data)]. crc16: {block index: stored CRC16} to store wrong values."""
    binary = b""
    for i, (address, data) in enumerate(blocks):
        crc = crc16_reference(data) if crc16 is None or i not in crc16 else crc16[i]
        binary += struct.pack(">II", address, len(data)) + data + struct.pack(">H", crc)
    lines = [f"vbf_version = {version};", "", "header {", banner]
    for key, value in (EXE_FIELDS if fields is None else fields).items():
        lines.append(f"    // {key}")
        lines.append(f"       {key} = {value};")
    lines.append(f"    file_checksum = 0x{(zlib.crc32(binary) if checksum is None else checksum):08X};")
    return "\r\n".join(lines).encode() + b"\r\n}" + binary


def write(tmp_path, data, name="app.vbf") -> str:
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def load(path, **options):
    return load_firmware([FirmwareSpec(file=path, **options)])


# --- checksums ----------------------------------------------------------------------------------------
def test_checksum_algorithms(rand):
    assert crc16_ccitt_false(b"123456789") == 0x29B1  # CRC-16/CCITT-FALSE check value
    assert crc32(b"123456789") == 0xCBF43926  # CRC-32/IEEE 802.3 check value
    data = rand.randbytes(5000)
    assert crc16_ccitt_false(data) == crc16_reference(data)


# --- mapping to the firmware model -----------------------------------------------------------------
def test_blocks_become_segments_of_one_logical_block(tmp_path, rand):
    blocks = [(0x00030000, rand.randbytes(300)), (0x00010000, rand.randbytes(4096)), (0x00012000, rand.randbytes(17))]
    path = write(tmp_path, build_vbf(blocks))
    assert detect_format(path) == "vbf"
    image = load(path)
    (block,) = image.blocks
    assert block.name == "app"
    assert [(s.address, s.data) for s in block.segments] == blocks  # file order = download order
    assert block.data_format_identifier == 0x00 and block.address_and_length_format == 0x44
    assert block.metadata["format"] == "vbf" and block.metadata["vbf_version"] == "2.6"
    assert block.metadata["type"] == "EXE"
    assert block.metadata["own_idents"] == {"sw_part_number": "32290001", "sw_version": "AA", "sw_part_type": "EXE"}
    assert block.metadata["ecu_address"] == 0x1A01
    assert block.metadata["crc16"] == [crc16_reference(d) for _, d in blocks]
    assert image.sources[0].path == path and image.sources[0].size == len(open(path, "rb").read())
    assert validate_image(image) == []


def test_erase_list(tmp_path, rand):
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(64))]))
    assert load(path).blocks[0].erase_ranges == [(0x10000, 0x10000), (0x30000, 0x1000)]
    # the project wins over the file
    assert load(path, erase=[(0x10000, 0x100)]).blocks[0].erase_ranges == [(0x10000, 0x100)]
    # a single pair without the outer braces
    fields = dict(EXE_FIELDS, erase="{ 0x00010000, 0x00000800 }")
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(64))], fields), "single.vbf")
    assert load(path).blocks[0].erase_ranges == [(0x10000, 0x800)]


def test_no_erase_key_means_nothing_is_erased(tmp_path, rand):
    fields = {k: v for k, v in EXE_FIELDS.items() if k != "erase"}
    block = load(write(tmp_path, build_vbf([(0x10000, rand.randbytes(64))], fields))).blocks[0]
    assert block.erase_ranges == [] and block.effective_erase_ranges == []


def test_compressed_data_is_sent_as_stored(tmp_path, rand):
    stored = rand.randbytes(700)  # stands for LZSS output: never decompressed
    fields = dict(EXE_FIELDS, data_format_identifier="0x10")
    path = write(tmp_path, build_vbf([(0x10000, stored)], fields))
    image = load(path)
    (block,) = image.blocks
    assert block.data_format_identifier == 0x10 and block.data == stored
    assert "crc16_unchecked" not in block.metadata  # CRC16 over the stored bytes: checked
    assert any("without memory_size" in str(i) for i in validate_image(image))
    block = load(path, dfi=0x11, memory_size=0x2000).blocks[0]
    assert block.data_format_identifier == 0x11 and block.download_regions() == [(0x10000, 0x2000, stored)]


def test_compressed_block_crc16_over_uncompressed_data_is_accepted(tmp_path, rand):
    """Ford documents the block CRC16 over the uncompressed data: not checkable without decompressing."""
    stored, original = rand.randbytes(200), rand.randbytes(900)
    fields = dict(EXE_FIELDS, data_format_identifier="0x10")
    path = write(tmp_path, build_vbf([(0x10000, stored)], fields, crc16={0: crc16_reference(original)}))
    block = load(path).blocks[0]
    assert block.data == stored
    assert block.metadata["crc16_unchecked"] == [0x10000] and "uncompressed" in block.metadata["crc16_note"]


def test_max_gap_refuses_compressed_data(tmp_path, rand):
    fields = dict(EXE_FIELDS, data_format_identifier="0x10")
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(16)), (0x10020, rand.randbytes(16))], fields))
    with pytest.raises(FirmwareError, match="max_gap"):
        load(path, max_gap=0x100)


def test_max_gap_merges_plain_data(tmp_path, rand):
    a, b = rand.randbytes(16), rand.randbytes(16)
    path = write(tmp_path, build_vbf([(0x10000, a), (0x10020, b)]))
    (seg,) = load(path, max_gap=0x100, fill_gaps=0xFF).blocks[0].segments
    assert seg.address == 0x10000 and seg.data == a + b"\xFF" * 16 + b


def test_sbl_metadata(tmp_path, rand):
    fields = {
        "description": '{ "Secondary bootloader",\r\n "for } tests; really" }',
        "sw_part_number": '"31327215"',
        "sw_version": '"B"',
        "sw_part_type": "SBL",
        "network": "CAN_HS",
        "ecu_address": "0x736",
        "frame_format": "CAN_STANDARD",
        "call": "0x20000100",
    }
    sbl = rand.randbytes(2048)
    path = write(tmp_path, build_vbf([(0x20000000, sbl)], fields, version="2.2"), "sbl.vbf")
    block = load(path, name="sbl").blocks[0]
    assert block.name == "sbl" and block.data == sbl
    assert block.erase_ranges == []  # SBLs are downloaded to RAM: never erased
    meta = block.metadata
    assert meta["type"] == "SBL" and meta["call"] == 0x20000100 and meta["ecu_address"] == 0x736
    assert meta["network"] == "CAN_HS" and meta["frame_format"] == "CAN_STANDARD"
    assert meta["description"] == "Secondary bootloader\nfor } tests; really"
    assert meta["own_idents"] == {"sw_part_number": "31327215", "sw_version": "B", "sw_part_type": "SBL"}


def test_signatures_and_verification_block(tmp_path, rand):
    vbt = rand.randbytes(44)
    fields = dict(
        EXE_FIELDS,
        verification_block_start="0x0003F000",
        verification_block_length="0x0000002C",
        verification_block_root_hash="0x003E263C8F66129C46B39D59E5964D7F3254FFA272AD3A3E70FC57ED95D73E12",
        sw_signature_dev="0x00C0FFEE",
        sw_signature="0x0001020304",
    )
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(64)), (0x3F000, vbt)], fields))
    block = load(path).blocks[0]
    assert block.signature == bytes.fromhex("0001020304")  # leading zero bytes kept
    assert block.segments[1].data == vbt  # the verification block table is downloaded like the data
    meta = block.metadata
    assert meta["signature_source"] == "sw_signature" and meta["sw_signature_dev"] == "00C0FFEE"
    assert meta["verification_block"] == {
        "start": 0x3F000, "length": 0x2C,
        "root_hash": "003E263C8F66129C46B39D59E5964D7F3254FFA272AD3A3E70FC57ED95D73E12",
    }
    del fields["sw_signature"]  # only the development signature: used, and said so
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(64))], fields), "dev.vbf")
    block = load(path).blocks[0]
    assert block.signature == bytes.fromhex("00C0FFEE") and block.metadata["signature_source"] == "sw_signature_dev"


def test_comments_in_header(tmp_path, rand):
    header = (
        b"vbf_version=2.6;\n"
        b"/* a comment before the header } ; = */\n"
        b"header{//} not the end\n"
        b"  sw_part_number=\"A}B;C\"; // quoted } and ;\n"
        b"  /* multi-line\n     comment with } and \"quotes\n  */ sw_version = \"A\" ;\n"
        b"\tsw_part_type\t=\tDATA;erase={{0x7D0A0000,0x10},};\n"
    )
    data = rand.randbytes(16)
    # the binary section starts right after '}': an address starting with '}' + LF must not be eaten
    binary = struct.pack(">II", 0x7D0A0000, len(data)) + data + struct.pack(">H", crc16_reference(data))
    raw = header + f"  file_checksum = 0x{zlib.crc32(binary):08X}; // end\n}}".encode() + binary
    vbf = parse_vbf(raw)
    assert vbf.header["sw_part_number"] == "A}B;C" and vbf.sw_version == "A" and vbf.sw_part_type == "DATA"
    assert vbf.erase == [(0x7D0A0000, 0x10)]
    assert [(b.address, b.data) for b in vbf.blocks] == [(0x7D0A0000, data)]
    assert load(write(tmp_path, raw)).blocks[0].metadata["type"] == "DATA"


def test_split_segments_assigns_each_erase_range_once(tmp_path, rand):
    blocks = [(0x10000, rand.randbytes(32)), (0x30000, rand.randbytes(32)), (0x30100, rand.randbytes(32))]
    fields = dict(EXE_FIELDS, erase="{ {0x30000, 0x1000}, {0x10000, 0x8000}, {0x50000, 0x100} }")
    image = load(write(tmp_path, build_vbf(blocks, fields)), split="segments")
    assert [b.name for b in image.blocks] == ["app#0", "app#1", "app#2"]
    # first overlapping block; ranges overlapping none go to the first block
    assert [b.erase_ranges for b in image.blocks] == [[(0x10000, 0x8000), (0x50000, 0x100)], [(0x30000, 0x1000)], []]


# --- errors ------------------------------------------------------------------------------------------
def test_corrupted_block_crc16(tmp_path, rand):
    blocks = [(0x10000, rand.randbytes(64)), (0x20000, rand.randbytes(64))]
    wrong = crc16_reference(blocks[1][1]) ^ 0x0100
    path = write(tmp_path, build_vbf(blocks, crc16={1: wrong}))  # file_checksum covers the wrong CRC: still valid
    with pytest.raises(FirmwareError, match=r"CRC16 mismatch in data block #1 at 0x00020000"):
        load(path)


def test_wrong_file_checksum(tmp_path, rand):
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(64))], checksum=0x12345678))
    with pytest.raises(FirmwareError, match=r"file_checksum mismatch: header says 0x12345678"):
        load(path)


def test_modified_data_byte_breaks_file_checksum(tmp_path, rand):
    raw = bytearray(build_vbf([(0x10000, rand.randbytes(64))]))
    raw[-10] ^= 0x01
    with pytest.raises(FirmwareError, match="corrupted or was modified"):
        load(write(tmp_path, bytes(raw)))


def _binary(*blocks):
    return b"".join(struct.pack(">II", a, len(d)) + d + struct.pack(">H", crc16_reference(d)) for a, d in blocks)


def _raw(header_body: bytes, binary: bytes = b"", version: bytes = b"2.6") -> bytes:
    checksum = f"file_checksum = 0x{zlib.crc32(binary):08X};".encode()
    return b"vbf_version = " + version + b";\nheader {\n" + header_body + checksum + b"\n}" + binary


@pytest.mark.parametrize("raw, message", [
    (b"\x00\x01binary", "not a VBF file"),
    (b'version = 2.6; header { file_checksum = 0x0; }', "not a VBF file"),
    (_raw(b"", _binary((0x0, b"x")), b"1.0"), "VBF version 1.0 is not supported"),
    (b"vbf_version = 2.6;\nheader {\n sw_part_type = EXE;\n}" + _binary((0x0, b"x")), "no file_checksum"),
    (_raw(b"sw_part_type = EXE; sw_part_type = SBL;", _binary((0x0, b"x"))), "defined twice"),
    (_raw(b"/* never closed ", _binary((0x0, b"x"))), "unterminated /\\* comment"),
    (_raw(b"sw_part_type = EXE\n", _binary((0x0, b"x"))), "expected ';' after the value of 'sw_part_type'"),
    (_raw(b"erase = { { 0x10 } };", _binary((0x0, b"x"))), "'erase' entries must be"),
    (b"vbf_version = 2.6;\nheader {\n sw_part_type = EXE;\n", "not closed"),
    (_raw(b""), "no data blocks"),
    (_raw(b"", _binary((0x0, b"abc"))[:-1]), "claims 3 bytes"),
    (_raw(b"", _binary((0x0, b"abc")) + b"\x00\x01"), "truncated data block #1"),
])
def test_format_errors(raw, message):
    with pytest.raises(FirmwareError, match=message):
        parse_vbf(raw)


def test_session_option_is_refused(tmp_path, rand):
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(8))]))
    with pytest.raises(FirmwareError, match="'session' only applies to PDX/ODX-F"):
        load(path, session="S")


# --- CLI and flashing --------------------------------------------------------------------------------
def test_cli_inspect_prints_part_number(tmp_path, rand, capsys):
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(256)), (0x30000, rand.randbytes(16))]))
    assert main(["inspect", path]) == 0
    out = capsys.readouterr().out
    assert "block app [APP (EXE)]: 0x00010000 - 0x0003000F" in out
    assert "sw_part_number=32290001, sw_version=AA, sw_part_type=EXE" in out


def test_cli_inspect_reports_bad_file(tmp_path, rand, capsys):
    path = write(tmp_path, build_vbf([(0x10000, rand.randbytes(16))], checksum=0))
    assert main(["inspect", path]) == 2
    assert "file_checksum mismatch" in capsys.readouterr().err


def test_flash_vbf_on_simulator(project_dir, rand):
    blocks = [(0x10000, rand.randbytes(5000)), (0x30000, rand.randbytes(700))]
    (project_dir.dir / "app.vbf").write_bytes(build_vbf(blocks))
    path = project_dir("""
    - id: ecu1
      simulate: true
      can: {backend: virtual, channel: t-vbf}
      firmware: [app.vbf]
      seed_key: {provider: python, path: seedkey.py}
    """)
    events = []
    assert run_job(path, "ecu1", events.append), events[-1].error
    finished = events[-1]
    assert isinstance(finished, JobFinished) and finished.success
