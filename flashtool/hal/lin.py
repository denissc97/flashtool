"""LIN master access for diagnostics (ISO 17987): only the frames 0x3C / 0x3D are used.

- `VirtualLinMaster` talks to simulated slave nodes in the same process (tests, macOS).
- `VectorXlLinMaster` drives a Vector VN channel as LIN master through the XL Driver
  Library (Windows only). It has not been run against hardware yet (risk R-06).
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from .backends import HardwareError

log = logging.getLogger(__name__)

MASTER_REQUEST_ID = 0x3C
SLAVE_RESPONSE_ID = 0x3D
DIAG_FRAME_LEN = 8  # diagnostic frames always carry 8 data bytes, classic checksum
LIN_BACKENDS = ("virtual", "vector")


def frame_time_max(baudrate: int, data_bytes: int = DIAG_FRAME_LEN) -> float:
    """Longest allowed frame duration in seconds: 1.4 x (34 + 10 x (N + 1)) bit times (ISO 17987-3)."""
    return 1.4 * (34 + 10 * (data_bytes + 1)) / baudrate


class LinMaster(ABC):
    """The tester as LIN master node. One instance per channel; not thread-safe on its own."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def send_master_request(self, data: bytes) -> None:
        """Send a master request frame (ID 0x3C) with exactly 8 data bytes."""

    @abstractmethod
    def poll_slave_response(self, timeout: float) -> bytes | None:
        """Send the 0x3D header; return the 8 data bytes, or None when no slave answers."""

    def wake_up(self) -> None:
        """Send a wake-up signal on the LIN bus (ISO 17987-2)."""
        raise HardwareError(f"{type(self).__name__} cannot send a LIN wake-up")

    def __enter__(self) -> LinMaster:
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _check_frame(data: bytes) -> bytes:
    data = bytes(data)
    if len(data) != DIAG_FRAME_LEN:
        raise ValueError(f"LIN diagnostic frames carry exactly {DIAG_FRAME_LEN} bytes, got {len(data)}")
    return data


# -- virtual bus -------------------------------------------------------------


@runtime_checkable
class LinSlave(Protocol):
    """A simulated slave node attached to a VirtualLinBus."""

    def on_master_request(self, data: bytes) -> None: ...

    def slave_response(self) -> bytes | None:
        """Data for the next 0x3D header, or None to stay silent."""


class VirtualLinBus:
    """In-process LIN bus, shared by channel name like python-can's `virtual` interface.

    Frames are delivered synchronously in the caller's thread; there is no bit timing.
    """

    _channels: dict[str, VirtualLinBus] = {}
    _channels_lock = threading.Lock()

    def __init__(self, name: str = "private"):
        self.name = name
        self._slaves: list[LinSlave] = []
        self._lock = threading.Lock()

    @classmethod
    def get(cls, channel: str) -> VirtualLinBus:
        """The bus shared by everyone that uses `channel`."""
        with cls._channels_lock:
            bus = cls._channels.get(channel)
            if bus is None:
                bus = cls._channels[channel] = cls(channel)
            return bus

    def attach(self, slave: LinSlave) -> None:
        with self._lock:
            if slave not in self._slaves:
                self._slaves.append(slave)

    def detach(self, slave: LinSlave) -> None:
        with self._lock:
            if slave in self._slaves:
                self._slaves.remove(slave)

    def master_request(self, data: bytes) -> None:
        with self._lock:
            slaves = list(self._slaves)
        for slave in slaves:
            slave.on_master_request(data)

    def wake_up(self) -> None:
        with self._lock:
            slaves = list(self._slaves)
        for slave in slaves:
            hook = getattr(slave, "on_wakeup", None)
            if hook is not None:
                hook()

    def slave_response(self) -> bytes | None:
        with self._lock:
            slaves = list(self._slaves)
        answers = [a for a in (s.slave_response() for s in slaves) if a is not None]
        if len(answers) > 1:
            # Two slaves driving the bus at once: the frame is destroyed (bit / checksum error).
            log.warning("LIN %s: %d slaves answered the same 0x3D header (collision)", self.name, len(answers))
            return None
        return _check_frame(answers[0]) if answers else None


class VirtualLinMaster(LinMaster):
    def __init__(self, channel: str = "sim0", bus: VirtualLinBus | None = None):
        self.channel = channel
        self.bus = bus or VirtualLinBus.get(channel)
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def _require_open(self) -> None:
        if not self._open:
            raise HardwareError(f"virtual LIN channel {self.channel} is not open")

    def wake_up(self) -> None:
        self._require_open()
        self.bus.wake_up()

    def send_master_request(self, data: bytes) -> None:
        self._require_open()
        self.bus.master_request(_check_frame(data))

    def poll_slave_response(self, timeout: float) -> bytes | None:
        self._require_open()
        return self.bus.slave_response()


# -- Vector XL Driver Library ------------------------------------------------
# Values and prototypes compared with a vxlapi.h copy (2014 revision); the ones marked
# "verify against vxlapi.h" could not be confirmed there.

XL_SUCCESS = 0
XL_ERR_QUEUE_IS_EMPTY = 10
XL_BUS_TYPE_LIN = 0x00000002
XL_INTERFACE_VERSION = 3  # XL_INTERFACE_VERSION_V3, used for LIN
XL_ACTIVATE_RESET_CLOCK = 8
XL_LIN_MASTER = 1
XL_LIN_VERSION_2_1 = 0x03  # newest LIN version the 2014 header knows; verify against vxlapi.h for 2.2 / ISO
XL_LIN_CALC_CHECKSUM = 0x100  # xlLinSetSlave: driver computes the classic checksum
XL_LIN_UNDEFINED_DLC = 0xFF
XL_LIN_MSG = 20  # event tags
XL_LIN_ERRMSG = 21
XL_LIN_SYNCERR = 22  # verify against vxlapi.h
XL_LIN_NOANS = 23
XL_LIN_MSGFLAG_CRCERROR = 0x81

XLstatus = ctypes.c_short
XLaccess = ctypes.c_uint64
XLportHandle = ctypes.c_long


class XLlinStatPar(ctypes.Structure):
    _fields_ = [
        ("LINMode", ctypes.c_uint),
        ("baudrate", ctypes.c_int),
        ("LINVersion", ctypes.c_uint),
        ("reserved", ctypes.c_uint),
    ]


class s_xl_lin_msg(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("id", ctypes.c_ubyte),
        ("dlc", ctypes.c_ubyte),
        ("flags", ctypes.c_ushort),
        ("data", ctypes.c_ubyte * 8),
        ("crc", ctypes.c_ubyte),
    ]


class s_xl_lin_no_ans(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("id", ctypes.c_ubyte)]


class s_xl_tag_data(ctypes.Union):
    # The header's union also holds CAN, DAIO, transceiver ... records; 32 bytes is its size
    # as used by python-can's XLevent (verify against vxlapi.h).
    _pack_ = 1
    _fields_ = [("linMsg", s_xl_lin_msg), ("linNoAns", s_xl_lin_no_ans), ("raw", ctypes.c_ubyte * 32)]


class XLevent(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("tag", ctypes.c_ubyte),
        ("chanIndex", ctypes.c_ubyte),
        ("transId", ctypes.c_ushort),
        ("portHandle", ctypes.c_ushort),
        ("flags", ctypes.c_ubyte),
        ("reserved", ctypes.c_ubyte),
        ("timeStamp", ctypes.c_uint64),
        ("tagData", s_xl_tag_data),
    ]


_U8P = ctypes.POINTER(ctypes.c_ubyte)
_UINTP = ctypes.POINTER(ctypes.c_uint)
_PROTOTYPES: dict[str, tuple] = {
    "xlOpenDriver": (XLstatus, []),
    "xlCloseDriver": (XLstatus, []),
    "xlGetErrorString": (ctypes.c_char_p, [XLstatus]),
    # appName, appChannel, *hwType, *hwIndex, *hwChannel, busType (verify against vxlapi.h)
    "xlGetApplConfig": (XLstatus, [ctypes.c_char_p, ctypes.c_uint, _UINTP, _UINTP, _UINTP, ctypes.c_uint]),
    "xlGetChannelMask": (XLaccess, [ctypes.c_int, ctypes.c_int, ctypes.c_int]),
    "xlOpenPort": (XLstatus, [ctypes.POINTER(XLportHandle), ctypes.c_char_p, XLaccess, ctypes.POINTER(XLaccess),
                              ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]),
    "xlClosePort": (XLstatus, [XLportHandle]),
    "xlActivateChannel": (XLstatus, [XLportHandle, XLaccess, ctypes.c_uint, ctypes.c_uint]),
    "xlDeactivateChannel": (XLstatus, [XLportHandle, XLaccess]),
    "xlReceive": (XLstatus, [XLportHandle, _UINTP, ctypes.POINTER(XLevent)]),
    "xlLinSetChannelParams": (XLstatus, [XLportHandle, XLaccess, XLlinStatPar]),  # struct passed by value
    "xlLinSetDLC": (XLstatus, [XLportHandle, XLaccess, _U8P]),  # unsigned char dlc[64]
    "xlLinSetSlave": (XLstatus, [XLportHandle, XLaccess, ctypes.c_ubyte, _U8P, ctypes.c_ubyte, ctypes.c_ushort]),
    "xlLinSendRequest": (XLstatus, [XLportHandle, XLaccess, ctypes.c_ubyte, ctypes.c_uint]),
}


def _load_xl_library():
    if sys.platform != "win32":
        raise HardwareError(
            "The Vector LIN backend needs the Vector XL Driver Library, which exists on Windows only. "
            "Use backend 'virtual' with the simulator."
        )
    name = "vxlapi64.dll" if sys.maxsize > 2**32 else "vxlapi.dll"
    try:
        dll = ctypes.WinDLL(name)  # type: ignore[attr-defined]
    except OSError as exc:
        raise HardwareError(f"Cannot load {name} (install the Vector XL Driver Library): {exc}") from exc
    for func, (restype, argtypes) in _PROTOTYPES.items():
        try:
            f = getattr(dll, func)
        except AttributeError as exc:
            raise HardwareError(f"{name} has no function {func} (driver library too old?)") from exc
        f.restype, f.argtypes = restype, argtypes
    return dll


class VectorXlLinMaster(LinMaster):
    """A Vector VN channel (LIN piggyback / LIN-capable channel) as LIN master.

    No schedule table runs: every header is sent on demand with xlLinSendRequest. The
    0x3C payload is loaded into the master's own response with xlLinSetSlave first.
    `channel` is the application channel of `app_name` in Vector Hardware Config, or
    the driver's global channel index when `app_name` is None.
    """

    def __init__(self, channel: int = 0, baudrate: int = 19200, app_name: str | None = "FlashTool",
                 rx_queue_size: int = 256):
        self.channel = channel
        self.baudrate = baudrate
        self.app_name = app_name
        self.rx_queue_size = rx_queue_size  # power of two
        self._xl = None
        self._port = XLportHandle(-1)
        self._mask = 0
        self._driver_open = False
        self._active = False
        # Time to wait for the driver's event about a frame we started (frame slot + USB latency).
        self._event_timeout = frame_time_max(baudrate) + 0.1

    def _check(self, status: int, what: str) -> None:
        if status != XL_SUCCESS:
            text = self._xl.xlGetErrorString(status) if self._xl is not None else None
            raise HardwareError(f"Vector XL {what} failed: {text.decode(errors='replace') if text else status}")

    def open(self) -> None:
        self._xl = _load_xl_library()
        try:
            self._open_channel()
        except Exception:
            self.close()
            raise

    def _open_channel(self) -> None:
        xl = self._xl
        self._check(xl.xlOpenDriver(), "xlOpenDriver")
        self._driver_open = True
        if self.app_name:
            hw_type, hw_index, hw_channel = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
            status = xl.xlGetApplConfig(self.app_name.encode(), self.channel, ctypes.byref(hw_type),
                                        ctypes.byref(hw_index), ctypes.byref(hw_channel), XL_BUS_TYPE_LIN)
            if status != XL_SUCCESS:
                raise HardwareError(
                    f"No LIN channel {self.channel} assigned to application '{self.app_name}' "
                    "in Vector Hardware Config"
                )
            self._mask = xl.xlGetChannelMask(hw_type.value, hw_index.value, hw_channel.value)
        else:
            self._mask = 1 << self.channel
        if not self._mask:
            raise HardwareError(f"Vector LIN channel {self.channel} not found")

        permission = XLaccess(self._mask)
        user = (self.app_name or "FlashTool").encode()
        self._check(xl.xlOpenPort(ctypes.byref(self._port), user, self._mask, ctypes.byref(permission),
                                  self.rx_queue_size, XL_INTERFACE_VERSION, XL_BUS_TYPE_LIN), "xlOpenPort")
        if not permission.value & self._mask:
            raise HardwareError(f"No init access to LIN channel {self.channel} (used by another application?)")
        params = XLlinStatPar(LINMode=XL_LIN_MASTER, baudrate=self.baudrate, LINVersion=XL_LIN_VERSION_2_1)
        self._check(xl.xlLinSetChannelParams(self._port, self._mask, params), "xlLinSetChannelParams")
        dlc = (ctypes.c_ubyte * 64)(*([XL_LIN_UNDEFINED_DLC] * 64))
        dlc[MASTER_REQUEST_ID] = dlc[SLAVE_RESPONSE_ID] = DIAG_FRAME_LEN
        self._check(xl.xlLinSetDLC(self._port, self._mask, dlc), "xlLinSetDLC")
        self._load_master_request(b"\xff" * DIAG_FRAME_LEN)
        self._check(xl.xlActivateChannel(self._port, self._mask, XL_BUS_TYPE_LIN, XL_ACTIVATE_RESET_CLOCK),
                    "xlActivateChannel")
        self._active = True

    def close(self) -> None:
        xl = self._xl
        if xl is None:
            return
        if self._active:
            xl.xlDeactivateChannel(self._port, self._mask)
            self._active = False
        if self._port.value != -1:
            xl.xlClosePort(self._port)
            self._port = XLportHandle(-1)
        if self._driver_open:
            xl.xlCloseDriver()
            self._driver_open = False
        self._xl = None

    def wake_up(self) -> None:
        # XLstatus xlLinWakeUp(XLportHandle, XLaccess)  # verify against vxlapi.h
        fn = getattr(self._xl, "xlLinWakeUp", None)
        if fn is None:
            raise HardwareError("xlLinWakeUp is not available in this XL Driver Library")
        fn.restype, fn.argtypes = XLstatus, [XLportHandle, XLaccess]
        self._check(fn(self._port, self._mask), "xlLinWakeUp")

    def _load_master_request(self, data: bytes) -> None:
        buf = (ctypes.c_ubyte * DIAG_FRAME_LEN)(*data)
        self._check(self._xl.xlLinSetSlave(self._port, self._mask, MASTER_REQUEST_ID, buf, DIAG_FRAME_LEN,
                                           XL_LIN_CALC_CHECKSUM), "xlLinSetSlave")

    def _next_event(self, deadline: float) -> XLevent | None:
        event, count = XLevent(), ctypes.c_uint(1)
        while True:
            count.value = 1
            status = self._xl.xlReceive(self._port, ctypes.byref(count), ctypes.byref(event))
            if status == XL_SUCCESS and count.value:
                return event
            if status not in (XL_SUCCESS, XL_ERR_QUEUE_IS_EMPTY):
                self._check(status, "xlReceive")
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)  # polling; xlSetNotification + WaitForSingleObject would avoid it

    def _drain(self) -> None:
        while self._next_event(0) is not None:
            pass

    def _wait_for(self, frame_id: int, timeout: float) -> XLevent | None:
        """The XL_LIN_MSG / XL_LIN_NOANS event about `frame_id`, other traffic is skipped."""
        deadline = time.monotonic() + timeout
        while (event := self._next_event(deadline)) is not None:
            tag = event.tag
            if tag == XL_LIN_MSG and event.tagData.linMsg.id == frame_id:  # frame ID, not PID: verify against vxlapi.h
                return event
            if tag == XL_LIN_NOANS and event.tagData.linNoAns.id == frame_id:
                return event
            if tag in (XL_LIN_ERRMSG, XL_LIN_SYNCERR):
                log.warning("Vector LIN channel %s: error event %d", self.channel, tag)
        return None

    def send_master_request(self, data: bytes) -> None:
        if not self._active:
            raise HardwareError("Vector LIN channel is not open")
        self._load_master_request(_check_frame(data))
        self._drain()
        self._check(self._xl.xlLinSendRequest(self._port, self._mask, MASTER_REQUEST_ID, 0), "xlLinSendRequest")
        # Assumes the driver reports our own 0x3C as an XL_LIN_MSG with the TX flag (verify with the XL manual).
        event = self._wait_for(MASTER_REQUEST_ID, self._event_timeout)
        if event is None or event.tag != XL_LIN_MSG:
            raise HardwareError("master request frame 0x3C was not transmitted")

    def poll_slave_response(self, timeout: float) -> bytes | None:
        if not self._active:
            raise HardwareError("Vector LIN channel is not open")
        self._drain()
        self._check(self._xl.xlLinSendRequest(self._port, self._mask, SLAVE_RESPONSE_ID, 0), "xlLinSendRequest")
        event = self._wait_for(SLAVE_RESPONSE_ID, max(timeout, self._event_timeout))
        if event is None or event.tag == XL_LIN_NOANS:
            return None
        msg = event.tagData.linMsg
        if (msg.flags & XL_LIN_MSGFLAG_CRCERROR) == XL_LIN_MSGFLAG_CRCERROR:
            log.warning("Vector LIN channel %s: checksum error in slave response", self.channel)
            return None
        if msg.dlc != DIAG_FRAME_LEN:
            return None
        return bytes(msg.data)


def create_lin_master(backend: str, channel: str | int, baudrate: int = 19200,
                      app_name: str | None = "FlashTool") -> LinMaster:
    if backend == "virtual":
        return VirtualLinMaster(str(channel))
    if backend == "vector":
        try:
            index = int(channel)
        except (TypeError, ValueError):
            raise HardwareError(f"Vector LIN channel must be a number, got {channel!r}") from None
        return VectorXlLinMaster(index, baudrate=baudrate, app_name=app_name)
    raise HardwareError(f"Unknown LIN backend '{backend}' (supported: {', '.join(LIN_BACKENDS)})")
