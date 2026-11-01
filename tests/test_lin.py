"""LIN-TP (ISO 17987-2): framing, the transport against a virtual bus, and a flash over LIN."""

import ctypes
import sys
import time
from collections import deque

import pytest

from flashtool.firmware.loaders import FirmwareSpec, load_firmware
from flashtool.hal.backends import HardwareError
from flashtool.hal.lin import (
    VectorXlLinMaster,
    VirtualLinBus,
    VirtualLinMaster,
    XLevent,
    XLlinStatPar,
    create_lin_master,
)
from flashtool.sequence.events import Progress
from flashtool.sim.ecu import SimulatedEcu
from flashtool.sim.lin_server import LinEcuServer
from flashtool.transport.base import TransportError
from flashtool.transport.lin_tp import LinTpConfig, LinTpTransport, Reassembler, segment

from .test_flash_sim import assert_written, flash

pytestmark = [pytest.mark.req("REQ-02"), pytest.mark.req("REQ-08")]


class ScriptedSlave:
    """Records master requests and answers 0x3D headers from a list of frames."""

    def __init__(self, frames=()):
        self.requests: list[tuple[float, bytes]] = []
        self.responses = deque(frames)

    def on_master_request(self, data):
        self.requests.append((time.monotonic(), bytes(data)))

    def slave_response(self):
        return self.responses.popleft() if self.responses else None

    @property
    def frames(self):
        return [f for _, f in self.requests]


@pytest.fixture
def bus():
    return VirtualLinBus("unit")


@pytest.fixture
def lin(bus):
    """lin(*slaves, **config) -> an open LinTpTransport on a private virtual bus."""
    opened = []

    def make(*slaves, **cfg):
        for slave in slaves:
            bus.attach(slave)
        cfg.setdefault("poll_interval_ms", 1)
        transport = LinTpTransport(LinTpConfig(**cfg), master=VirtualLinMaster(bus=bus))
        transport.open()
        opened.append(transport)
        return transport

    yield make
    for transport in opened:
        transport.close()


def reassemble(frames):
    rx = Reassembler()
    results = [rx.feed(f) for f in frames]
    assert all(r is None for r in results[:-1])
    return results[-1]


# -- framing ----------------------------------------------------------------------


def test_single_frame():
    assert segment(0x01, b"\x10\x03") == [bytes.fromhex("01 02 10 03 FF FF FF FF")]
    six = bytes(range(1, 7))
    assert segment(0x01, six) == [b"\x01\x06" + six]
    assert reassemble(segment(0x01, six)) == six


def test_first_frame_and_consecutive_frames():
    payload = bytes(range(20))
    frames = segment(0x05, payload)
    assert frames == [
        bytes.fromhex("05 10 14 00 01 02 03 04"),
        bytes.fromhex("05 21 05 06 07 08 09 0A"),
        bytes.fromhex("05 22 0B 0C 0D 0E 0F 10"),
        bytes.fromhex("05 23 11 12 13 FF FF FF"),
    ]
    assert reassemble(frames) == payload


def test_sequence_number_wraps(lin):
    payload = bytes(i & 0xFF for i in range(5 + 6 * 20))  # FF + 20 CFs
    frames = segment(0x01, payload)
    assert [f[1] for f in frames[1:]] == [0x20 | (n & 0x0F) for n in range(1, 21)]  # ...2E 2F 20 21...
    assert lin(ScriptedSlave(frames)).recv(1.0) == payload


def test_maximum_pdu_length(lin):
    payload = bytes(i & 0xFF for i in range(4095))
    frames = segment(0x01, payload)
    assert frames[0][:3] == b"\x01\x1f\xff"
    assert lin(ScriptedSlave(frames)).recv(1.0) == payload
    with pytest.raises(TransportError):
        segment(0x01, bytes(4096))
    with pytest.raises(TransportError):
        segment(0x01, b"")


# -- transport ----------------------------------------------------------------------


def test_send_segments_and_paces_frames_with_n_cs(lin):
    slave = ScriptedSlave()
    payload = bytes(range(20))
    lin(slave, n_cs_ms=5).send(payload)
    assert slave.frames == segment(0x01, payload)
    times = [t for t, _ in slave.requests]
    assert all(b - a >= 0.0045 for a, b in zip(times, times[1:]))


def test_functional_request_uses_functional_nad(lin):
    slave = ScriptedSlave()
    transport = lin(slave)
    transport.send(b"\x3e\x80", functional=True)
    assert slave.frames == [bytes.fromhex("7E 02 3E 80 FF FF FF FF")]


def test_functional_request_too_long_for_a_single_frame(lin):
    slave = ScriptedSlave()
    with pytest.raises(TransportError, match="single frame"):
        lin(slave).send(bytes(7), functional=True)
    assert slave.frames == []


def test_frames_for_other_nads_are_ignored(lin):
    ours = bytes(range(0x40, 0x54))
    first, *rest = segment(0x01, ours)
    other = segment(0x02, b"\x62\xf1\x90\x01\x02\x03\x04\x05")
    frames = [segment(0x02, b"\x7f\x22\x31")[0], first, other[0], *rest[:1], other[1], *rest[1:]]
    assert lin(ScriptedSlave(frames)).recv(1.0) == ours


def test_wrong_sequence_number_raises(lin):
    frames = segment(0x01, bytes(20))
    frames[2] = frames[2][:1] + b"\x23" + frames[2][2:]
    with pytest.raises(TransportError, match="sequence number 3, expected 2"):
        lin(ScriptedSlave(frames)).recv(1.0)


def test_missing_consecutive_frame_raises_after_n_cr(lin):
    transport = lin(ScriptedSlave(segment(0x01, bytes(20))[:2]), n_cr_ms=30)
    t0 = time.monotonic()
    with pytest.raises(TransportError, match="N_Cr"):
        transport.recv(0.01)  # the timeout covers the first frame only
    assert time.monotonic() - t0 >= 0.03


def test_recv_returns_none_when_no_slave_answers(lin):
    transport = lin(ScriptedSlave())
    assert transport.recv(0) is None
    t0 = time.monotonic()
    assert transport.recv(0.02) is None
    assert time.monotonic() - t0 >= 0.02


def test_set_params_returns_previous_values_for_restore():
    transport = LinTpTransport(LinTpConfig(n_cs_ms=2, poll_interval_ms=10))
    previous = transport.set_params(n_cs_ms=0, poll_interval_ms=1)
    assert previous == {"n_cs_ms": 2, "poll_interval_ms": 10}
    assert (transport.config.n_cs_ms, transport.config.poll_interval_ms) == (0, 1)
    assert transport.set_params(**previous) == {"n_cs_ms": 0, "poll_interval_ms": 1}
    assert (transport.config.n_cs_ms, transport.config.poll_interval_ms) == (2, 10)
    with pytest.raises(TransportError, match="stmin_rx"):
        transport.set_params(stmin_rx=0)
    with pytest.raises(TransportError):
        transport.set_params(n_cs_ms=-1)
    assert transport.config.n_cs_ms == 2


def test_config_validation():
    for bad in ({"nad": 0x7E}, {"nad": 0x00}, {"functional_nad": 0x01}, {"baudrate": 115200}, {"poll_interval_ms": -1}):
        with pytest.raises(TransportError):
            LinTpTransport(LinTpConfig(**bad))


def test_describe():
    d = LinTpTransport(LinTpConfig(nad=0x22)).describe()
    assert d["type"] == "lin" and d["nad"] == "0x22" and d["baudrate"] == 19200


# -- virtual bus, simulator --------------------------------------------------------------


def test_virtual_bus_delivers_to_all_slaves_and_detects_collisions(bus):
    a, b = ScriptedSlave([bytes(8)]), ScriptedSlave([bytes(8)])
    bus.attach(a)
    bus.attach(b)
    with VirtualLinMaster(bus=bus) as master:
        master.send_master_request(bytes.fromhex("01 02 10 03 FF FF FF FF"))
        assert a.frames == b.frames == [bytes.fromhex("01 02 10 03 FF FF FF FF")]
        assert master.poll_slave_response(0.01) is None  # both answered: frame destroyed
        assert master.poll_slave_response(0.01) is None  # nobody answers
        with pytest.raises(ValueError):
            master.send_master_request(b"\x01\x02")
    assert VirtualLinBus.get("shared-name") is VirtualLinBus.get("shared-name")


def test_server_answers_physical_and_only_processes_functional_requests(bus, lin):
    ecu = SimulatedEcu()
    with LinEcuServer(ecu, bus=bus):
        transport = lin()
        transport.send(b"\x10\x03", functional=True)
        assert transport.recv(0.1) is None  # LIN slaves stay silent on functional requests
        assert ecu.session == 0x03
        transport.send(b"\x22\xf1\x90")
        assert transport.recv(1.0) == b"\x62\xf1\x90" + ecu.cfg.vin.encode()  # FF + CFs


def test_server_new_request_discards_pending_response(bus, lin):
    ecu = SimulatedEcu()
    with LinEcuServer(ecu, bus=bus):
        transport = lin()
        transport.send(b"\x22\xf1\x90")  # response never collected
        time.sleep(0.02)
        transport.send(b"\x22\xf1\x89")
        assert transport.recv(1.0) == b"\x62\xf1\x89" + ecu.cfg.sw_version.encode()
        assert transport.recv(0.05) is None


# -- hardware backends ------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows error path")
def test_vector_backend_needs_windows():
    master = create_lin_master("vector", 0, 19200, "FlashTool")
    assert isinstance(master, VectorXlLinMaster)
    with pytest.raises(HardwareError, match="Windows"):
        master.open()
    with pytest.raises(HardwareError, match="Unknown LIN backend"):
        create_lin_master("kvaser", 0)


def test_xl_struct_sizes():
    assert ctypes.sizeof(XLlinStatPar) == 16
    assert ctypes.sizeof(XLevent) == 48 and XLevent.tagData.offset == 16


# -- end to end ----------------------------------------------------------------------------


@pytest.fixture
def image(project_dir):
    return load_firmware([FirmwareSpec(file="app.hex")], str(project_dir.dir))


def test_lin_flash_uds_standard(image):
    ecu = SimulatedEcu()
    # uds_standard expects answers to its functional session requests, as on CAN.
    with LinEcuServer(ecu, channel="t-lin", respond_to_functional=True):
        transport = LinTpTransport(LinTpConfig(channel="t-lin", poll_interval_ms=1))
        ok, err, engine, events = flash(transport, image)
    assert ok, err
    assert_written(ecu, image)
    assert ecu.session == 0x01
    progress = [e for e in events if isinstance(e, Progress)]
    assert progress[-1].done == image.total_size
