import pytest

from flashtool.firmware.loaders import FirmwareError, FirmwareSpec, load_firmware
from flashtool.firmware.validate import validate_image

from .conftest import make_hex

pytestmark = [pytest.mark.req("REQ-01"), pytest.mark.req("REQ-11")]


def test_hex_segments_kept_separate(tmp_path):
    make_hex(tmp_path / "a.hex", [(0x1000, b"\x01" * 100), (0x2000, b"\x02" * 50)])
    image = load_firmware([FirmwareSpec(file="a.hex")], str(tmp_path))
    (block,) = image.blocks
    assert [(s.address, len(s.data)) for s in block.segments] == [(0x1000, 100), (0x2000, 50)]
    assert block.effective_erase_ranges == [(0x1000, 0x2032 - 0x1000)]
    assert image.sources[0].sha256 and validate_image(image) == []


def test_srec_and_bin(tmp_path):
    import bincopy

    bf = bincopy.BinFile()
    bf.add_binary(b"\xAA" * 64, address=0x8000)
    (tmp_path / "c.s19").write_text(bf.as_srec())
    (tmp_path / "d.bin").write_bytes(b"\x55" * 32)
    image = load_firmware(
        [FirmwareSpec(file="c.s19"), FirmwareSpec(file="d.bin", base_address=0x9000)], str(tmp_path)
    )
    assert [(b.start, b.size) for b in image.blocks] == [(0x8000, 64), (0x9000, 32)]


def test_bin_needs_base_address(tmp_path):
    (tmp_path / "d.bin").write_bytes(b"\x00" * 4)
    with pytest.raises(FirmwareError, match="base_address"):
        load_firmware([FirmwareSpec(file="d.bin")], str(tmp_path))


def test_merge_gaps_fills(tmp_path):
    make_hex(tmp_path / "a.hex", [(0x100, b"\x01" * 4), (0x108, b"\x02" * 4)])
    image = load_firmware([FirmwareSpec(file="a.hex", max_gap=16, fill_gaps=0xEE)], str(tmp_path))
    (seg,) = image.blocks[0].segments
    assert seg.data == b"\x01" * 4 + b"\xEE" * 4 + b"\x02" * 4


def test_overlap_is_an_error(tmp_path):
    make_hex(tmp_path / "a.hex", [(0x100, b"\x01" * 16)])
    make_hex(tmp_path / "b.hex", [(0x108, b"\x02" * 16)])
    image = load_firmware([FirmwareSpec(file="a.hex"), FirmwareSpec(file="b.hex")], str(tmp_path))
    assert any("overlaps" in i.message for i in validate_image(image))


def test_compressed_block_parameters(tmp_path):
    make_hex(tmp_path / "z.hex", [(0x4000, b"\x9c" * 100)])
    spec = FirmwareSpec.from_dict({"file": "z.hex", "dfi": 0x11, "memory_size": 0x1000,
                                   "memory_address": 0x8000, "erase": [[0x8000, 0x1000]]})
    (block,) = load_firmware([spec], str(tmp_path)).blocks
    assert block.download_regions() == [(0x8000, 0x1000, b"\x9c" * 100)]
    assert block.effective_erase_ranges == [(0x8000, 0x1000)]
    assert not [i for i in validate_image(load_firmware([spec], str(tmp_path))) if i.severity == "error"]


def test_compressed_without_memory_size_warns(tmp_path):
    make_hex(tmp_path / "z.hex", [(0x4000, b"\x01" * 10)])
    image = load_firmware([FirmwareSpec(file="z.hex", dfi=0x10)], str(tmp_path))
    assert [i.severity for i in validate_image(image)] == ["warning"]


def test_address_must_fit_alfid(tmp_path):
    make_hex(tmp_path / "a.hex", [(0x1_0000, b"\x01" * 4)])
    image = load_firmware([FirmwareSpec(file="a.hex", alfid=0x22)], str(tmp_path))
    assert any("does not fit" in i.message for i in validate_image(image))


def test_unknown_option_rejected():
    with pytest.raises(FirmwareError, match="Unknown firmware option"):
        FirmwareSpec.from_dict({"file": "x.hex", "compress": True})
