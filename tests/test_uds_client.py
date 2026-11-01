import collections
import time

import pytest

from flashtool.transport.base import Transport
from flashtool.uds.client import NegativeResponse, UdsClient, UdsTimeout, UdsTiming

pytestmark = [pytest.mark.req("REQ-03"), pytest.mark.req("REQ-08"), pytest.mark.req("REQ-13")]


class ScriptedTransport(Transport):
    """Replies to each request with a scripted list of (delay_s, response)."""

    name = "scripted"

    def __init__(self, script):
        self.script = collections.deque(script)
        self.sent = []
        self.rx = collections.deque()

    def open(self): ...
    def close(self): ...

    def send(self, payload, functional=False):
        self.sent.append((bytes(payload), functional))
        if self.script:
            now = time.monotonic()
            for delay, data in self.script.popleft():
                now += delay
                self.rx.append((now, data))

    def recv(self, timeout):
        end = time.monotonic() + timeout
        while True:
            if self.rx and self.rx[0][0] <= time.monotonic():
                return self.rx.popleft()[1]
            if time.monotonic() >= end:
                return None
            time.sleep(0.001)


def client(script, **timing):
    return UdsClient(ScriptedTransport(script), UdsTiming(**timing))


def test_positive_response():
    c = client([[(0, b"\x50\x03\x00\x32\x01\xf4")]])
    assert c.request(b"\x10\x03").data == b"\x50\x03\x00\x32\x01\xf4"


def test_response_pending_extends_timeout():
    c = client([[(0, b"\x7f\x31\x78"), (0.12, b"\x7f\x31\x78"), (0.12, b"\x71\x01\xff\x00\x00")]], p2_ms=50)
    assert c.request(b"\x31\x01\xff\x00").positive


def test_negative_response_raises_and_can_be_allowed():
    c = client([[(0, b"\x7f\x22\x31")], [(0, b"\x7f\x22\x31")]])
    with pytest.raises(NegativeResponse) as exc:
        c.request(b"\x22\xf1\x90")
    assert exc.value.nrc == 0x31
    assert c.request(b"\x22\xf1\x90", allow_nrc=[0x31]).nrc == 0x31


def test_suppressed_positive_response_returns_none():
    c = client([[]], p2_ms=30)
    assert c.request(b"\x3e\x80") is None


def test_suppressed_request_still_reports_nrc():
    c = client([[(0, b"\x7f\x85\x22")]], p2_ms=30)
    with pytest.raises(NegativeResponse):
        c.request(b"\x85\x82")


def test_timeout():
    c = client([[]], p2_ms=30)
    with pytest.raises(UdsTimeout):
        c.request(b"\x22\xf1\x90")


def test_busy_repeat_request_is_retried():
    c = client([[(0, b"\x7f\x22\x21")], [(0, b"\x62\xf1\x90\x41")]], busy_delay_ms=1)
    assert c.read_data_by_identifier(0xF190) == b"\x41"
    assert len(c.transport.sent) == 2


def test_session_timing_adopted_only_if_longer():
    c = client([[(0, b"\x50\x02\x01\x2c\x03\xe8")]], p2_ms=50, p2_star_ms=2000)
    c.diagnostic_session_control(0x02)
    assert c.p2_ms == 300 + 50 and c.p2_star_ms == 10000 + 50


def test_request_download_parses_block_length():
    c = client([[(0, b"\x74\x20\x08\x02")]])
    assert c.request_download(0x00, 0x44, 0x1000, 0x200) == 0x0802
    assert c.transport.sent[0][0] == bytes.fromhex("34 00 44 00001000 00000200")


def test_stale_response_ignored():
    c = client([[(0, b"\x62\x00\x00"), (0, b"\x71\x01\x02\x02\x00")]])
    assert c.routine_control(0x01, 0x0202).positive
