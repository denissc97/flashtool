"""Generates the demo firmware files (deterministic content) next to this script."""

import os
import random
import zipfile

import bincopy

from flashtool.firmware.vbf import crc16_ccitt_false, crc32

HERE = os.path.dirname(os.path.abspath(__file__))


def blob(seed: int, size: int) -> bytes:
    return random.Random(seed).randbytes(size)


def main() -> None:
    app = bincopy.BinFile()
    app.add_binary(blob(1, 96 * 1024), address=0x0001_0000)  # application
    app.add_binary(blob(2, 8 * 1024), address=0x0003_0000)  # second segment, gap in between
    with open(os.path.join(HERE, "app.hex"), "w") as f:
        f.write(app.as_ihex())

    cal = bincopy.BinFile()
    cal.add_binary(blob(3, 16 * 1024), address=0x0008_0000)
    with open(os.path.join(HERE, "cal.s19"), "w") as f:
        f.write(cal.as_srec())

    with open(os.path.join(HERE, "gateway.bin"), "wb") as f:
        f.write(blob(4, 256 * 1024))

    # PDX container: ODX-F with two sessions referencing the HEX / S-record files above.
    with zipfile.ZipFile(os.path.join(HERE, "body_sw.pdx"), "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.xml", INDEX_XML)
        z.writestr("FL_BODY.odx-f", ODX_F)
        z.write(os.path.join(HERE, "app.hex"), "app.hex")
        z.write(os.path.join(HERE, "cal.s19"), "cal.s19")
    write_vbf(os.path.join(HERE, "seat_sw.vbf"), 0x0008_0000, blob(5, 16 * 1024))
    print("app.hex, cal.s19, gateway.bin, body_sw.pdx, seat_sw.vbf written")


def write_vbf(path: str, address: int, data: bytes) -> None:
    """VBF (Volvo Binary Format): text header, then [address][length][data][CRC16] blocks."""
    body = address.to_bytes(4, "big") + len(data).to_bytes(4, "big") + data + crc16_ccitt_false(data).to_bytes(2, "big")
    header = (
        "vbf_version = 2.6;\n"
        "header {\n"
        '    description = {"FlashTool demo seat module calibration"};\n'
        '    sw_part_number = "32212345";\n'
        '    sw_version = "AA";\n'
        "    sw_part_type = DATA;\n"
        "    data_format_identifier = 0x00;\n"
        "    network = LIN;\n"
        "    ecu_address = 0x0A;\n"
        f"    erase = {{ {{ 0x{address:08X}, 0x{len(data):08X} }} }};\n"
        f"    file_checksum = 0x{crc32(body):08X};\n"
        "}"
    )
    with open(path, "wb") as f:
        f.write(header.encode() + body)


def _datablock(ident: str, kind: str, start: int, end: int, part: str) -> str:
    return f"""
            <DATABLOCK ID="DB.{ident}" TYPE="{kind}">
              <SHORT-NAME>{ident}</SHORT-NAME>
              <FLASHDATA-REF ID-REF="FD.{ident}"/>
              <SEGMENTS>
                <SEGMENT ID="SEG.{ident}">
                  <SHORT-NAME>SEG_{ident}</SHORT-NAME>
                  <SOURCE-START-ADDRESS>{start:08X}</SOURCE-START-ADDRESS>
                  <SOURCE-END-ADDRESS>{end:08X}</SOURCE-END-ADDRESS>
                </SEGMENT>
              </SEGMENTS>
              <OWN-IDENTS>
                <OWN-IDENT ID="OI.{ident}">
                  <SHORT-NAME>PART_NUMBER</SHORT-NAME>
                  <IDENT-VALUE TYPE="A_ASCIISTRING">{part}</IDENT-VALUE>
                </OWN-IDENT>
              </OWN-IDENTS>
            </DATABLOCK>"""


def _flashdata(ident: str, fmt: str, file: str) -> str:
    return f"""
            <FLASHDATA ID="FD.{ident}" xsi:type="EXTERN-FLASHDATA">
              <SHORT-NAME>FD_{ident}</SHORT-NAME>
              <DATAFORMAT SELECTION="{fmt}"/>
              <DATAFILE LATEBOUND-DATAFILE="false">{file}</DATAFILE>
            </FLASHDATA>"""


ODX_F = f"""<?xml version="1.0" encoding="UTF-8"?>
<ODX MODEL-VERSION="2.2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <FLASH ID="FL.BODY">
    <SHORT-NAME>FL_BODY</SHORT-NAME>
    <ECU-MEMS>
      <ECU-MEM ID="EM.BODY">
        <SHORT-NAME>EM_BODY</SHORT-NAME>
        <MEM>
          <SESSIONS>
            <SESSION ID="SES.FULL">
              <SHORT-NAME>FULL</SHORT-NAME>
              <DATABLOCK-REFS><DATABLOCK-REF ID-REF="DB.APP"/><DATABLOCK-REF ID-REF="DB.CAL"/></DATABLOCK-REFS>
            </SESSION>
            <SESSION ID="SES.CAL">
              <SHORT-NAME>CAL_ONLY</SHORT-NAME>
              <DATABLOCK-REFS><DATABLOCK-REF ID-REF="DB.CAL"/></DATABLOCK-REFS>
            </SESSION>
          </SESSIONS>
          <DATABLOCKS>{_datablock("APP", "CODE", 0x10000, 0x27FFF, "A0001112")}{_datablock("CAL", "DATA", 0x80000, 0x83FFF, "C0002223")}
          </DATABLOCKS>
          <FLASHDATAS>{_flashdata("APP", "INTEL-HEX", "app.hex")}{_flashdata("CAL", "MOTOROLA-S", "cal.s19")}
          </FLASHDATAS>
        </MEM>
      </ECU-MEM>
    </ECU-MEMS>
  </FLASH>
</ODX>
"""

INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CATALOG F-DTD-VERSION="ODX-2.2.0">
  <SHORT-NAME>BODY_SW</SHORT-NAME>
  <ABLOCKS>
    <ABLOCK>
      <SHORT-NAME>FL_BODY</SHORT-NAME>
      <FILES>
        <FILE MIME-TYPE="application/x-asam.odx.odx-f">FL_BODY.odx-f</FILE>
        <FILE MIME-TYPE="application/octet-stream">app.hex</FILE>
        <FILE MIME-TYPE="application/octet-stream">cal.s19</FILE>
      </FILES>
    </ABLOCK>
  </ABLOCKS>
</CATALOG>
"""


if __name__ == "__main__":
    main()
