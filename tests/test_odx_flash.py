"""PDX / ODX-F loading (flashtool/firmware/odx_flash.py).

Most archives are built from odxtools objects and written with odxtools' own PDX writer;
one test uses a hand-written ODX 2.2 document to check the XML structure independently.
"""

import hashlib
import io
import itertools
import zipfile
import zlib

import bincopy
import pytest

pytest.importorskip("odxtools")

from odxtools.database import Database  # noqa: E402
from odxtools.datablock import Datablock  # noqa: E402
from odxtools.datafile import Datafile  # noqa: E402
from odxtools.dataformat import Dataformat  # noqa: E402
from odxtools.dataformatselection import DataformatSelection  # noqa: E402
from odxtools.ecumem import EcuMem  # noqa: E402
from odxtools.encryptcompressmethod import EncryptCompressMethod  # noqa: E402
from odxtools.encryptcompressmethodtype import EncryptCompressMethodType  # noqa: E402
from odxtools.externflashdata import ExternFlashdata  # noqa: E402
from odxtools.flash import Flash  # noqa: E402
from odxtools.identvalue import IdentValue  # noqa: E402
from odxtools.identvaluetype import IdentValueType  # noqa: E402
from odxtools.internflashdata import InternFlashdata  # noqa: E402
from odxtools.mem import Mem  # noqa: E402
from odxtools.nameditemlist import NamedItemList  # noqa: E402
from odxtools.odxlink import DocType, OdxDocFragment, OdxLinkId, OdxLinkRef  # noqa: E402
from odxtools.ownident import OwnIdent  # noqa: E402
from odxtools.posoffset import PosOffset  # noqa: E402
from odxtools.security import Security  # noqa: E402
from odxtools.segment import Segment as OdxSegment  # noqa: E402
from odxtools.session import Session  # noqa: E402
from odxtools.sessionsubelemtype import SessionSubElemType  # noqa: E402
from odxtools.validityfor import ValidityFor  # noqa: E402
from odxtools.writepdxfile import write_pdx_file  # noqa: E402

from flashtool.cli import main  # noqa: E402
from flashtool.firmware.loaders import FirmwareError, FirmwareSpec, load_firmware  # noqa: E402
from flashtool.firmware.validate import validate_image  # noqa: E402

pytestmark = [pytest.mark.req("REQ-01")]

DOC = (OdxDocFragment("FL_TEST", DocType.FLASH),)
_ids = itertools.count()


def _ecm(raw):
    return None if raw is None else EncryptCompressMethod(value_raw=raw, value_type=EncryptCompressMethodType.A_UINT32)


def _sub(kind, raw):
    return ValidityFor(value_raw=raw, value_type=SessionSubElemType(kind))


def segment(start, end=None, *, uncompressed=None, compressed=None, ecm=None):
    n = next(_ids)
    return OdxSegment(short_name=f"SEG_{n}", odx_id=OdxLinkId(f"SEG.{n}", DOC), source_start_address=start,
                      source_end_address=end, uncompressed_size=uncompressed, compressed_size=compressed,
                      encrypt_compress_method=_ecm(ecm))


def security(signature=None, checksum=None, method=None):
    return Security(fw_signature=signature and _sub("A_BYTEFIELD", signature),
                    fw_checksum=checksum and _sub("A_UINT32", checksum),
                    security_method=method and _sub("A_ASCIISTRING", method))


def hex_text(*regions):
    bf = bincopy.BinFile()
    for address, data in regions:
        bf.add_binary(data, address=address)
    return bf.as_ihex()


class PdxBuilder:
    """One FLASH document with one ECU-MEM, written with odxtools' PDX writer."""

    def __init__(self):
        self.sessions, self.datablocks, self.flashdatas, self.files = [], [], [], {}

    def flashdata(self, name, fmt, *, data=None, datafile=None, ecm=None, address_length=None, size_length=None):
        common = dict(short_name=name, odx_id=OdxLinkId(f"FD.{name}", DOC),
                      dataformat=Dataformat(selection=DataformatSelection(fmt)), encrypt_compress_method=_ecm(ecm),
                      address_length=address_length, size_length=size_length)
        if datafile is not None:
            fd = ExternFlashdata(datafile=Datafile(value=datafile, latebound_datafile=False), **common)
        else:
            fd = InternFlashdata(data=data, **common)
        self.flashdatas.append(fd)
        return fd

    def datablock(self, name, fd, segments, *, kind="CODE", own_idents=None, securities=(), offset=None):
        db = Datablock(
            short_name=name, odx_id=OdxLinkId(f"DB.{name}", DOC), data_type=kind,
            flashdata_ref=OdxLinkRef(fd.odx_id.local_id, DOC), segments=NamedItemList(segments),
            own_idents=NamedItemList([
                OwnIdent(short_name=k, odx_id=OdxLinkId(f"OI.{name}.{k}", DOC),
                         ident_value=IdentValue(value_raw=v, value_type=IdentValueType.A_ASCIISTRING))
                for k, v in (own_idents or {}).items()
            ]),
            securities=list(securities), target_addr_offset=offset,
        )
        self.datablocks.append(db)
        return db

    def session(self, name, *blocks, securities=()):
        self.sessions.append(Session(short_name=name, odx_id=OdxLinkId(f"SES.{name}", DOC),
                                     datablock_refs=[OdxLinkRef(b.odx_id.local_id, DOC) for b in blocks],
                                     securities=list(securities)))

    def write(self, path):
        mem = Mem(sessions=NamedItemList(self.sessions), datablocks=NamedItemList(self.datablocks),
                  flashdatas=NamedItemList(self.flashdatas))
        flash = Flash(short_name="FL_TEST", odx_id=OdxLinkId("FL.TEST", DOC),
                      ecu_mems=NamedItemList([EcuMem(short_name="EM_TEST", odx_id=OdxLinkId("EM.TEST", DOC), mem=mem)]))
        db = Database()
        db.short_name = "TEST_PDX"
        db.flashs.append(flash)
        for name, data in self.files.items():
            db.add_auxiliary_file(name, io.BytesIO(data))
        write_pdx_file(str(path), db)
        return str(path)


def load(tmp_path, file="test.pdx", **options):
    return load_firmware([FirmwareSpec(file=file, **options)], str(tmp_path))


def regions(block):
    return [(s.address, s.data) for s in block.segments]


# --- data sources ------------------------------------------------------------------------------


def test_intern_binary_data(tmp_path, rand):
    payload = rand.randbytes(64)
    b = PdxBuilder()
    fd = b.flashdata("FD_APP", "BINARY", data=payload.hex().upper(), address_length=4, size_length=2)
    b.session("SES_APP", b.datablock("APP", fd, [segment(0x8000, 0x8000 + 63)], own_idents={"SW_VERSION": "1.0"}))
    path = b.write(tmp_path / "test.pdx")

    image = load(tmp_path)
    (block,) = image.blocks
    assert block.name == "APP" and regions(block) == [(0x8000, payload)]
    assert block.address_and_length_format == 0x24  # SIZE-LENGTH 2, ADDRESS-LENGTH 4
    assert block.data_format_identifier == 0 and block.memory_size is None and block.signature is None
    assert block.metadata["type"] == "CODE" and block.metadata["own_idents"] == {"SW_VERSION": "1.0"}
    assert block.metadata["session"] == "SES_APP" and block.metadata["dataformat"] == "BINARY"
    (src,) = image.sources
    assert src.path == path and src.sha256 == hashlib.sha256((tmp_path / "test.pdx").read_bytes()).hexdigest()


def test_extern_intel_hex_with_target_offset(tmp_path, rand):
    app1, app2, other = rand.randbytes(200), rand.randbytes(64), rand.randbytes(16)
    b = PdxBuilder()
    b.files["app.hex"] = hex_text((0x10000, app1), (0x20000, app2), (0x30000, other)).encode()
    fd = b.flashdata("FD_APP", "INTEL-HEX", datafile="app.hex")
    b.session("S", b.datablock("APP", fd, [segment(0x10000, 0x10000 + 199), segment(0x20000, 0x20000 + 63)],
                               offset=PosOffset(positive_offset=0x1000)))
    b.write(tmp_path / "test.pdx")

    (block,) = load(tmp_path).blocks
    # only the SEGMENT ranges are taken from the file, moved by TARGET-ADDR-OFFSET
    assert regions(block) == [(0x11000, app1), (0x21000, app2)]
    assert block.metadata["datafile"] == "app.hex" and block.metadata["target_addr_offset"] == 0x1000


def test_hex_gap_inside_segment_is_filled(tmp_path):
    b = PdxBuilder()
    b.files["app.hex"] = hex_text((0x100, b"\x01" * 4), (0x108, b"\x02" * 8)).encode()
    b.session("S", b.datablock("APP", b.flashdata("FD", "INTEL-HEX", datafile="app.hex"), [segment(0x100, 0x10F)]))
    b.write(tmp_path / "test.pdx")
    (block,) = load(tmp_path).blocks
    assert regions(block) == [(0x100, b"\x01" * 4 + b"\xFF" * 4 + b"\x02" * 8)]
    assert block.metadata["fill_bytes"] == 4


def _binary_pdx(tmp_path, data):
    b = PdxBuilder()
    b.files["cal.bin"] = data
    fd = b.flashdata("FD_CAL", "BINARY", datafile="cal.bin")
    b.session("S", b.datablock("CAL", fd, [segment(0x4000, 0x4000 + 99), segment(0x6000, uncompressed=50)],
                               kind="DATA"))
    return b.write(tmp_path / "test.pdx")


def test_extern_binary_two_segments(tmp_path, rand):
    part1, part2 = rand.randbytes(100), rand.randbytes(50)
    _binary_pdx(tmp_path, part1 + part2)
    (block,) = load(tmp_path).blocks
    assert regions(block) == [(0x4000, part1), (0x6000, part2)]  # back to back in the file
    assert block.metadata["type"] == "DATA"

    _binary_pdx(tmp_path, part1 + part2 + b"\x00")  # layout does not match the segments
    with pytest.raises(FirmwareError, match="back to back"):
        load(tmp_path)


def test_compressed_block(tmp_path):
    plain = bytes(range(256)) * 16
    packed = zlib.compress(plain)
    b = PdxBuilder()
    b.files["app.bin"] = packed
    fd = b.flashdata("FD_APP", "BINARY", datafile="app.bin", ecm="0x11")
    b.session("S", b.datablock("APP", fd, [segment(0x8000, compressed=len(packed), uncompressed=len(plain))]))
    b.write(tmp_path / "test.pdx")

    image = load(tmp_path)
    (block,) = image.blocks
    assert block.data == packed  # sent as stored, never decompressed
    assert block.data_format_identifier == 0x11 and block.memory_size == len(plain)
    assert block.download_regions() == [(0x8000, len(plain), packed)]
    assert block.metadata["encrypt_compress_method"] == "0x11"
    assert validate_image(image) == []

    # explicit values in the FirmwareSpec win over the ODX
    (block,) = load(tmp_path, dfi=0x00, memory_size=0x2000, memory_address=0x9000).blocks
    assert block.data_format_identifier == 0x00
    assert block.download_regions() == [(0x9000, 0x2000, packed)]


def test_segment_method_wins_over_flashdata_method(tmp_path):
    b = PdxBuilder()
    b.files["app.hex"] = hex_text((0x100, b"\x9c" * 32)).encode()
    fd = b.flashdata("FD_APP", "INTEL-HEX", datafile="app.hex", ecm="0x11")
    b.session("S", b.datablock("APP", fd, [segment(0x100, compressed=32, uncompressed=4096, ecm="0x21")]))
    b.write(tmp_path / "test.pdx")
    (block,) = load(tmp_path).blocks
    assert block.data_format_identifier == 0x21 and block.memory_size == 4096 and block.size == 32


# --- sessions and securities --------------------------------------------------------------------


def _two_session_pdx(tmp_path, rand):
    b = PdxBuilder()
    boot = rand.randbytes(32)
    packed = zlib.compress(rand.randbytes(512))
    b.files["app.bin"] = packed
    fd_boot = b.flashdata("FD_BOOT", "INTEL-HEX", data=hex_text((0x0, boot)))
    fd_app = b.flashdata("FD_APP", "BINARY", datafile="app.bin", ecm="0x11")
    boot_db = b.datablock("BOOT", fd_boot, [segment(0x0, 0x1F)], kind="BOOT")
    app_db = b.datablock("APP", fd_app, [segment(0x8000, compressed=len(packed), uncompressed=512)],
                         securities=[security(signature="DEADBEEF01", checksum="0x12345678", method="RSA-PSS")])
    b.session("SES_BOOT", boot_db)
    b.session("SES_ALL", boot_db, app_db, securities=[security(signature="CAFE")])
    return b.write(tmp_path / "test.pdx")


def test_two_sessions(tmp_path, rand):
    _two_session_pdx(tmp_path, rand)
    with pytest.raises(FirmwareError, match="2 ODX-F sessions, choose one with 'session': SES_BOOT, SES_ALL"):
        load(tmp_path)
    assert [b.name for b in load(tmp_path, session="SES_BOOT").blocks] == ["BOOT"]
    spec = FirmwareSpec.from_dict({"file": "test.pdx", "session": "SES_ALL"})
    assert [b.name for b in load_firmware([spec], str(tmp_path)).blocks] == ["BOOT", "APP"]
    assert [b.name for b in load(tmp_path, session="EM_TEST/SES_ALL", name="ecu").blocks] == ["ecu.BOOT", "ecu.APP"]
    with pytest.raises(FirmwareError, match="no ODX-F session 'NOPE'"):
        load(tmp_path, session="NOPE")
    with pytest.raises(FirmwareError, match="need a single block"):
        load(tmp_path, session="SES_ALL", memory_size=0x1000)


def test_signature_mapping(tmp_path, rand):
    _two_session_pdx(tmp_path, rand)
    boot, app = load(tmp_path, session="SES_ALL").blocks
    assert app.signature == bytes.fromhex("DEADBEEF01")
    assert app.metadata["fw_checksum"] == {"type": "A_UINT32", "value": 0x12345678}
    assert app.metadata["security_method"] == {"type": "A_ASCIISTRING", "value": "RSA-PSS"}
    # session-level SECURITYS cover the whole session: metadata only, not the block signature
    assert boot.signature is None
    assert boot.metadata["session_securities"] == [{"fw_signature": {"type": "A_BYTEFIELD", "value": "CAFE"}}]


# --- complete image -----------------------------------------------------------------------------


def _realistic_pdx(tmp_path, rand):
    expected = {
        "BOOT": [(0x0, rand.randbytes(256))],
        "APP": [(0x10000, rand.randbytes(5000)), (0x20000, rand.randbytes(3000))],
        "CAL": [(0x40000, rand.randbytes(1024)), (0x41000, rand.randbytes(512))],
    }
    b = PdxBuilder()
    b.files["app.hex"] = hex_text(*expected["APP"]).encode()
    b.files["cal.bin"] = b"".join(d for _, d in expected["CAL"])
    blocks = [
        b.datablock("BOOT", b.flashdata("FD_BOOT", "INTEL-HEX", data=hex_text(*expected["BOOT"])),
                    [segment(0x0, 0xFF)], kind="BOOT"),
        b.datablock("APP", b.flashdata("FD_APP", "INTEL-HEX", datafile="app.hex", address_length=4, size_length=4),
                    [segment(a, a + len(d) - 1) for a, d in expected["APP"]],
                    securities=[security(signature="00" * 32, method="RSA")]),
        b.datablock("CAL", b.flashdata("FD_CAL", "BINARY", datafile="cal.bin"),
                    [segment(a, a + len(d) - 1) for a, d in expected["CAL"]], kind="DATA"),
    ]
    b.session("SES_ALL", *blocks)
    return b.write(tmp_path / "test.pdx"), expected


def test_loaded_pdx_passes_validate_image(tmp_path, rand):
    _realistic_pdx(tmp_path, rand)
    image = load(tmp_path)
    assert [(b.name, b.metadata["type"]) for b in image.blocks] == [("BOOT", "BOOT"), ("APP", "CODE"), ("CAL", "DATA")]
    assert validate_image(image) == []


def test_loaded_bytes_match_input(tmp_path, rand):
    _, expected = _realistic_pdx(tmp_path, rand)
    image = load(tmp_path)
    assert {b.name: regions(b) for b in image.blocks} == expected
    assert image.total_size == sum(len(d) for segs in expected.values() for _, d in segs)


# --- hand-written ODX-F (ASAM MCD-2D 2.2 structure) -----------------------------------------------

HAND_ODX_F = """<?xml version="1.0" encoding="UTF-8"?>
<ODX MODEL-VERSION="2.2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:noNamespaceSchemaLocation="odx.xsd">
  <FLASH ID="FL.HAND">
    <SHORT-NAME>FL_HAND</SHORT-NAME>
    <ECU-MEMS>
      <ECU-MEM ID="EM.HAND">
        <SHORT-NAME>EM_HAND</SHORT-NAME>
        <MEM>
          <SESSIONS>
            <SESSION ID="SES.APP">
              <SHORT-NAME>SES_APP</SHORT-NAME>
              <DATABLOCK-REFS>
                <DATABLOCK-REF ID-REF="DB.APP"/>
              </DATABLOCK-REFS>
            </SESSION>
          </SESSIONS>
          <DATABLOCKS>
            <DATABLOCK ID="DB.APP" TYPE="CODE">
              <SHORT-NAME>APP</SHORT-NAME>
              <LOGICAL-BLOCK-INDEX>01</LOGICAL-BLOCK-INDEX>
              <FLASHDATA-REF ID-REF="FD.APP"/>
              <SEGMENTS>
                <SEGMENT ID="SEG.APP">
                  <SHORT-NAME>SEG_APP</SHORT-NAME>
                  <SOURCE-START-ADDRESS>00018000</SOURCE-START-ADDRESS>
                  <SOURCE-END-ADDRESS>000180FF</SOURCE-END-ADDRESS>
                </SEGMENT>
              </SEGMENTS>
              <TARGET-ADDR-OFFSET xsi:type="NEG-OFFSET">
                <NEGATIVE-OFFSET>00008000</NEGATIVE-OFFSET>
              </TARGET-ADDR-OFFSET>
              <OWN-IDENTS>
                <OWN-IDENT ID="OI.APP">
                  <SHORT-NAME>PART_NUMBER</SHORT-NAME>
                  <IDENT-VALUE TYPE="A_ASCIISTRING">A1234567</IDENT-VALUE>
                </OWN-IDENT>
              </OWN-IDENTS>
              <SECURITYS>
                <SECURITY>
                  <SECURITY-METHOD TYPE="A_ASCIISTRING">ECDSA</SECURITY-METHOD>
                  <FW-SIGNATURE TYPE="A_BYTEFIELD">0102A0FF</FW-SIGNATURE>
                  <FW-CHECKSUM TYPE="A_UINT32">305419896</FW-CHECKSUM>
                </SECURITY>
              </SECURITYS>
            </DATABLOCK>
          </DATABLOCKS>
          <FLASHDATAS>
            <FLASHDATA ID="FD.APP" xsi:type="EXTERN-FLASHDATA">
              <SHORT-NAME>FD_APP</SHORT-NAME>
              <DATAFORMAT SELECTION="MOTOROLA-S"/>
              <DATAFILE LATEBOUND-DATAFILE="false">app.s19</DATAFILE>
            </FLASHDATA>
          </FLASHDATAS>
        </MEM>
      </ECU-MEM>
    </ECU-MEMS>
    <ECU-MEM-CONNECTORS>
      <ECU-MEM-CONNECTOR ID="EMC.HAND">
        <SHORT-NAME>EMC_HAND</SHORT-NAME>
        <ECU-MEM-REF ID-REF="EM.HAND"/>
        <LAYER-REFS>
          <LAYER-REF ID-REF="BV.ECU" DOCREF="BV_ECU" DOCTYPE="LAYER"/>
        </LAYER-REFS>
      </ECU-MEM-CONNECTOR>
    </ECU-MEM-CONNECTORS>
  </FLASH>
</ODX>
"""

HAND_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<CATALOG F-DTD-VERSION="ODX-2.2.0">
  <SHORT-NAME>HAND_PDX</SHORT-NAME>
  <ABLOCKS>
    <ABLOCK>
      <SHORT-NAME>FL_HAND</SHORT-NAME>
      <FILES>
        <FILE MIME-TYPE="application/x-asam.odx.odx-f">FL_HAND.odx-f</FILE>
        <FILE MIME-TYPE="application/octet-stream">data/app.s19</FILE>
      </FILES>
    </ABLOCK>
  </ABLOCKS>
</CATALOG>
"""


@pytest.mark.parametrize("container", ["odx-f", "pdx"])
def test_hand_written_odx_f(tmp_path, rand, container):
    payload = rand.randbytes(256)
    bf = bincopy.BinFile()
    bf.add_binary(rand.randbytes(16), address=0x17000)  # outside the segment: ignored
    bf.add_binary(payload, address=0x18000)
    srec = bf.as_srec()
    if container == "odx-f":  # standalone .odx-f, DATAFILE next to it
        (tmp_path / "FL_HAND.odx-f").write_text(HAND_ODX_F)
        (tmp_path / "app.s19").write_text(srec)
        file, sources = "FL_HAND.odx-f", ["FL_HAND.odx-f", "app.s19"]
    else:  # data file in a sub-folder; the ECU-MEM-CONNECTOR points into an ODX-D that is not loadable
        with zipfile.ZipFile(tmp_path / "hand.pdx", "w") as z:
            z.writestr("index.xml", HAND_INDEX)
            z.writestr("FL_HAND.odx-f", HAND_ODX_F)
            z.writestr("data/app.s19", srec)
            z.writestr("broken.odx-d", "<ODX MODEL-VERSION='2.2.0'><DIAG-LAYER-CONTAINER>")
        file, sources = "hand.pdx", ["hand.pdx"]

    image = load(tmp_path, file=file)
    (block,) = image.blocks
    assert regions(block) == [(0x10000, payload)]  # NEG-OFFSET 0x8000
    assert block.signature == bytes.fromhex("0102A0FF")
    assert block.metadata["fw_checksum"] == {"type": "A_UINT32", "value": 0x12345678}
    assert block.metadata["logical_block_index"] == 1
    assert block.metadata["own_idents"] == {"PART_NUMBER": "A1234567"}
    assert [s.path for s in image.sources] == [str(tmp_path / f) for f in sources]
    assert validate_image(image) == []


# --- errors and CLI -----------------------------------------------------------------------------


def test_missing_datafile(tmp_path):
    b = PdxBuilder()
    b.session("S", b.datablock("APP", b.flashdata("FD", "INTEL-HEX", datafile="missing.hex"), [segment(0, 15)]))
    b.write(tmp_path / "test.pdx")
    with pytest.raises(FirmwareError, match="DATAFILE 'missing.hex' not found in the PDX"):
        load(tmp_path)


def test_session_option_needs_odx(tmp_path):
    (tmp_path / "a.hex").write_text(hex_text((0, b"\x01")))
    with pytest.raises(FirmwareError, match="only applies to PDX"):
        load(tmp_path, file="a.hex", session="S")


def test_cli_inspect(tmp_path, rand, capsys):
    path = _two_session_pdx(tmp_path, rand)
    assert main(["inspect", path]) == 2
    out, err = capsys.readouterr()
    assert "SES_BOOT  (ECU-MEM EM_TEST): BOOT" in out and "SES_ALL  (ECU-MEM EM_TEST): BOOT, APP" in out
    assert "choose one with 'session'" in err

    assert main(["inspect", path, "--session", "SES_ALL"]) == 0
    out, _ = capsys.readouterr()
    assert "* SES_ALL" in out
    assert "block BOOT [BOOT]: 0x00000000 - 0x0000001F, 32 bytes, DFI 0x00" in out
    assert "block APP [APP (CODE)]: 0x00008000" in out
    assert "DFI 0x11, ALFID 0x44, memory_size 512, signature yes" in out
