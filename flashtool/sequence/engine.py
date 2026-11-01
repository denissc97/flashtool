"""Executes a resolved sequence on top of the UDS client (docs/ARCHITECTURE.md §4.5)."""

from __future__ import annotations

import os
import time
from collections import ChainMap
from dataclasses import dataclass, field
from typing import Any

from ..diagdb.catalog import CatalogError, DiagCatalog
from ..firmware.model import FirmwareImage, LogicalBlock
from ..security.certs import CertificateError, TargetCredentials, describe_certificate
from ..security.seedkey import SeedKeyError, SeedKeyProvider
from ..transport.base import Transport, TransportError
from ..uds.client import UdsClient, UdsError
from ..uds.tester_present import TesterPresent
from .events import Emit, LogEvent, Progress, StepFinished, StepStarted
from .expr import ExprError, evaluate, render_bytes, render_value
from .group import GroupError, GroupSync
from .loader import block_sequence_key, step_kind


class StepError(Exception):
    pass


class Cancelled(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, path: str, cause: Exception):
        self.path, self.cause = path, cause
        super().__init__(f"{path}: {cause}")


RECOVERABLE = (UdsError, TransportError, SeedKeyError, CertificateError, CatalogError, ExprError, StepError,
               GroupError)


@dataclass
class StepRecord:
    path: str
    kind: str
    status: str
    duration_s: float
    attempts: int = 1
    error: str | None = None
    detail: dict = field(default_factory=dict)


class CancelToken:
    """Anything with is_set(): threading.Event or multiprocessing.Event."""

    def __init__(self, event=None):
        self._event = event

    def is_set(self) -> bool:
        return self._event is not None and self._event.is_set()


class Engine:
    def __init__(
        self,
        *,
        job: str,
        client: UdsClient,
        transport: Transport,
        image: FirmwareImage,
        variables: dict,
        seedkey: SeedKeyProvider | None,
        emit: Emit,
        cancel: CancelToken | None = None,
        tester_present: TesterPresent | None = None,
        credentials: TargetCredentials | None = None,
        group: GroupSync | None = None,
        catalog: DiagCatalog | None = None,
        allow_scripts: bool = False,
    ):
        self.job = job
        self.client = client
        self.transport = transport
        self.image = image
        self.vars: dict[str, Any] = dict(variables)
        self.vars["image"] = image
        self.seedkey = seedkey
        self.credentials = credentials
        self.group = group
        self.catalog = catalog
        self.allow_scripts = allow_scripts
        self.emit = emit
        self.cancel = cancel or CancelToken()
        self.tp = tester_present or TesterPresent(client, client.timing.tester_present_s)
        self.records: list[StepRecord] = []
        self._sequence: dict = {}
        self._done_by_block: dict[str, int] = {}
        self._t_transfer0: float | None = None

    # -- entry point -----------------------------------------------------
    def run(self, sequence: dict) -> tuple[bool, str | None]:
        error: str | None = None
        self._sequence = sequence
        try:
            self._run_steps(sequence["steps"], ChainMap({}, self.vars), "")
        except StepFailed as exc:
            error = str(exc)
        except Cancelled:
            error = "cancelled by user"
            self._log("warning", "Cancelled; running final steps")
        finally:
            if self.group is not None:
                self.group.leave("main", self.job)
            for step in sequence.get("finally") or []:
                try:
                    self._exec(step, ChainMap({}, self.vars), "finally", in_finally=True)
                except (StepFailed, Cancelled):
                    pass
            if self.group is not None:
                self.group.leave("finally", self.job)
            self.tp.stop()
        return error is None, error

    # -- generic step handling --------------------------------------------
    def _run_steps(self, steps: list, scope: ChainMap, prefix: str) -> None:
        for step in steps:
            self._exec(step, scope, prefix)

    def _exec(self, step: dict, scope: ChainMap, prefix: str, in_finally: bool = False) -> None:
        if self.cancel.is_set() and not in_finally:
            raise Cancelled()
        kind = step_kind(step)
        sid = step.get("id", kind)
        path = f"{prefix}/{sid}" if prefix else sid
        label = step.get("name") or self._label(kind, step)

        if "when" in step and not self._eval(step["when"], scope):
            self.records.append(StepRecord(path, kind, "skipped", 0.0))
            self.emit(StepFinished(self.job, path, "skipped", 0.0))
            return

        on_fail = "continue" if in_finally else step.get("on_fail", "abort")
        retries = int(step.get("retries", 1)) if on_fail == "retry" else 0
        shared = self.group is not None and step.get("group") == "once"
        attempts_allowed = 1 if kind == "foreach" or shared else 1 + retries

        self.emit(StepStarted(self.job, path, kind, label))
        t0 = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            status = "ok"
            try:
                previous = self._apply_transport(step, scope)
                try:
                    if shared:
                        if prefix not in ("", "finally"):
                            raise StepError("'group: once' is only allowed on top-level steps")
                        here, detail = self.group.run_once(
                            "finally" if in_finally else "main", sid, self.job,
                            lambda: self._dispatch(kind, step, scope, path) or {},
                        )
                        if not here:
                            status, detail = "shared", {"executed_by_other_ecu": True}
                    else:
                        detail = self._dispatch(kind, step, scope, path) or {}
                finally:
                    if previous:
                        self.transport.set_params(**previous)
            except Cancelled:
                self._finish(path, kind, "failed", t0, attempt, "cancelled")
                raise
            except StepFailed as exc:  # nested step already reported its own failure
                self._finish(path, kind, "failed", t0, attempt, str(exc.cause))
                raise
            except RECOVERABLE as exc:
                if attempt < attempts_allowed:
                    self._log("warning", f"{path}: {exc} - retry {attempt}/{retries}")
                    continue
                if on_fail == "continue":
                    self._finish(path, kind, "warning", t0, attempt, str(exc))
                    if not in_finally:
                        self._log("warning", f"{path}: {exc} (continuing)")
                    return
                self._finish(path, kind, "failed", t0, attempt, str(exc))
                raise StepFailed(path, exc) from exc
            self._finish(path, kind, status, t0, attempt, None, detail)
            return

    def _finish(self, path, kind, status, t0, attempts, error, detail=None) -> None:
        duration = time.monotonic() - t0
        self.records.append(StepRecord(path, kind, status, duration, attempts, error, detail or {}))
        self.emit(StepFinished(self.job, path, status, duration, error))

    def _apply_transport(self, step: dict, scope: ChainMap) -> dict:
        overrides = step.get("transport")
        if not overrides:
            return {}
        values = {k: render_value(v, scope) for k, v in overrides.items()}
        return self.transport.set_params(**values)

    def _dispatch(self, kind: str, step: dict, scope: ChainMap, path: str) -> dict | None:
        return getattr(self, f"_k_{kind}")(step, scope, path)

    # -- helpers -----------------------------------------------------------
    def _eval(self, expr: Any, scope: ChainMap) -> Any:
        if isinstance(expr, str):
            expr = expr.strip()
            if expr.startswith("${") and expr.endswith("}"):
                expr = expr[2:-1]
            return evaluate(expr, scope)
        return expr

    def _int(self, value: Any, scope: ChainMap, what: str) -> int:
        v = render_value(value, scope)
        if isinstance(v, (bytes, bytearray)):
            return int.from_bytes(v, "big")
        try:
            return int(v, 0) if isinstance(v, str) else int(v)
        except (TypeError, ValueError):
            raise StepError(f"{what} must be an integer, got {v!r}") from None

    def _req_kwargs(self, step: dict) -> dict:
        kw: dict[str, Any] = {"functional": bool(step.get("functional", False))}
        if "timeout_ms" in step:
            kw["timeout_ms"] = int(step["timeout_ms"])
        if "allow_nrc" in step:
            kw["allow_nrc"] = tuple(int(x) for x in step["allow_nrc"])
        if "expect_response" in step:
            kw["expect_response"] = bool(step["expect_response"])
        return kw

    def _store_and_check(self, step: dict, scope: ChainMap, data: bytes | None) -> dict:
        if step.get("store"):
            self.vars[step["store"]] = data
        if "expect_data" in step:
            expected = render_bytes(step["expect_data"], scope)
            if data is None or not data.startswith(expected):
                got = data.hex(" ").upper() if data else "no response"
                raise StepError(f"response {got} does not start with {expected.hex(' ').upper()}")
        return {"response": data.hex(" ").upper()} if data else {}

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while (remaining := end - time.monotonic()) > 0:
            if self.cancel.is_set():
                raise Cancelled()
            time.sleep(min(remaining, 0.05))

    def _log(self, level: str, message: str) -> None:
        self.emit(LogEvent(self.job, level, message))
        self.client.trace.note(message)

    @staticmethod
    def _label(kind: str, step: dict) -> str:
        value = step.get(kind)
        if kind in ("foreach",):
            return f"foreach {value} in {step.get('in')}"
        if kind == "program_block":
            return f"program {value} (sequence by block type)"
        if kind == "if":
            return f"if {value}"
        if isinstance(value, (dict, list)):
            return kind
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{kind} 0x{value:02X}"
        return f"{kind} {value}"

    # -- step kinds --------------------------------------------------------
    def _k_request(self, step, scope, path):
        payload = render_bytes(step["request"], scope)
        resp = self.client.request(payload, **self._req_kwargs(step))
        return self._store_and_check(step, scope, resp.data if resp else None)

    def _k_service(self, step, scope, path):
        """Named service from the diagnostic catalog (ODX-D or YAML)."""
        if self.catalog is None:
            raise StepError("service steps need a diagnostic catalog ('catalog:' in the template or target)")
        service = self.catalog.get(str(step["service"]))
        params = {k: render_value(v, scope) for k, v in (step.get("params") or {}).items()}
        if service.encoder is not None:
            try:
                payload = service.encoder(**params)
            except Exception as exc:
                raise StepError(f"{service.name}: cannot encode request: {exc}") from exc
        else:
            payload = render_bytes(service.request, ChainMap(params, scope))
        resp = self.client.request(payload, **self._req_kwargs(step))
        detail = self._store_and_check(step, scope, resp.data if resp else None)
        detail["request"] = payload.hex(" ").upper()
        return detail

    def _k_session(self, step, scope, path):
        session = self._int(step["session"], scope, "session")
        resp = self.client.diagnostic_session_control(session, **self._req_kwargs(step))
        detail = self._store_and_check(step, scope, resp.data if resp else None)
        detail.update(p2_ms=self.client.p2_ms, p2_star_ms=self.client.p2_star_ms)
        return detail

    def _k_security(self, step, scope, path):
        cfg = step["security"] if isinstance(step["security"], dict) else {"level": step["security"]}
        level = self._int(cfg.get("level", 0x01), scope, "security level")
        if level % 2 == 0:
            raise StepError("security level must be the odd requestSeed sub-function")
        kw = self._req_kwargs(step)
        seed = self.client.security_access_seed(level, **kw)
        if not any(seed):
            self._log("info", f"Security level 0x{level:02X} already unlocked (zero seed)")
            return {"level": level, "already_unlocked": True}
        if self.seedkey is None:
            raise StepError("ECU requested a key but no seed&key provider is configured")
        variant = str(render_value(cfg.get("variant", self.vars.get("seedkey_variant", "")), scope))
        key = self.seedkey.compute_key(seed, level, variant)
        self.client.security_access_key(level + 1, key, **kw)
        return {"level": level}

    def _k_authenticate(self, step, scope, path):
        """APCE, unidirectional: 29 08 (optional) -> 29 01 certificate -> 29 03 proof of ownership."""
        cfg = step["authenticate"] if isinstance(step["authenticate"], dict) else {}
        if self.credentials is None:
            raise StepError("authenticate: no certificate provider configured for this target")
        kw = self._req_kwargs(step)
        if cfg.get("check_configuration", True):
            method = self.client.authentication_configuration(**kw)
            if method != 0x02:
                raise StepError(f"ECU reports authentication configuration 0x{method:02X}; only APCE (0x02) is supported")
        try:
            chain = self.credentials.certificate_chain()
        except Exception as exc:
            raise StepError(f"certificate provider: {exc}") from exc
        challenge_client = os.urandom(int(cfg.get("challenge_client_length", 0)))
        ret, challenge_server, _ = self.client.verify_certificate_unidirectional(
            chain[0], challenge_client, int(cfg.get("communication_configuration", 0)), **kw
        )
        if ret not in (0x11, 0x12):
            raise StepError(f"certificate not accepted (authenticationReturnParameter 0x{ret:02X})")
        if not challenge_server:
            raise StepError("ECU returned an empty challenge")
        to_sign = challenge_server + (challenge_client if cfg.get("sign") == "challenge_server+challenge_client" else b"")
        try:
            proof = self.credentials.sign(to_sign)
        except Exception as exc:
            raise StepError(f"signing the ECU challenge failed: {exc}") from exc
        ret, session_key = self.client.proof_of_ownership(proof, **kw)
        if ret != 0x12:
            raise StepError(f"proof of ownership not accepted (authenticationReturnParameter 0x{ret:02X})")
        detail = {"certificate": describe_certificate(chain[0]), "session_key_info_bytes": len(session_key)}
        self._log("info", f"Authenticated with certificate {detail['certificate']['subject']}")
        return detail

    def _k_download(self, step, scope, path):
        block = self._eval(step["download"], scope)
        if not isinstance(block, LogicalBlock):
            raise StepError(f"download expects a firmware block, got {type(block).__name__}")
        kw = self._req_kwargs(step)
        cap = step.get("max_block_length")
        self._done_by_block[block.name] = 0
        if self._t_transfer0 is None:
            self._t_transfer0 = time.monotonic()
        regions = block.download_regions()
        for address, size, data in regions:
            mbl = self.client.request_download(
                block.data_format_identifier, block.address_and_length_format, address, size, **kw
            )
            chunk = min(mbl, self.transport.max_payload, int(cap) if cap else mbl) - 2
            if chunk <= 0:
                raise StepError(f"maxNumberOfBlockLength {mbl} is too small")
            bsc = 1
            for offset in range(0, len(data), chunk):
                if self.cancel.is_set():
                    raise Cancelled()
                part = data[offset : offset + chunk]
                self.client.transfer_data(bsc, part, **kw)
                bsc = (bsc + 1) & 0xFF
                self._done_by_block[block.name] += len(part)
                self._progress(block)
            self.client.request_transfer_exit(**kw)
        return {"block": block.name, "bytes": block.size, "regions": len(regions)}

    def _k_program_block(self, step, scope, path):
        """Runs the block sequence of the block's type (BOOT, CAL, ...), else the default one."""
        block = self._eval(step["program_block"], scope)
        if not isinstance(block, LogicalBlock):
            raise StepError(f"program_block expects a firmware block, got {type(block).__name__}")
        key = block_sequence_key(self._sequence, block.type)
        if key is None:
            raise StepError(f"no block sequence for {block.name} (type {block.type}) and no default one")
        self._log("info", f"Block {block.name} (type {block.type}): block sequence '{key}'")
        self._run_steps(self._sequence["block_sequences"][key], scope.new_child({"block": block}), f"{path}[{key}]")
        return {"block": block.name, "type": block.type, "sequence": key}

    def _progress(self, block: LogicalBlock) -> None:
        done = sum(self._done_by_block.values())
        elapsed = max(time.monotonic() - (self._t_transfer0 or time.monotonic()), 1e-6)
        self.emit(Progress(self.job, block.name, self._done_by_block[block.name], block.size,
                           done, self.image.total_size, done / elapsed))

    def _k_foreach(self, step, scope, path):
        var = step["foreach"]
        items = self._eval(step["in"], scope)
        if not isinstance(items, (list, tuple)):
            raise StepError(f"foreach: '{step['in']}' is not a list")
        retries = int(step.get("retries", 1)) if step.get("on_fail") == "retry" else 0
        for index, item in enumerate(items):
            name = getattr(item, "name", None) or str(index)
            child = scope.new_child({var: item})
            for attempt in range(retries + 1):
                try:
                    self._run_steps(step["steps"], child, f"{path}[{name}]")
                    break
                except StepFailed as exc:
                    if attempt >= retries:
                        raise
                    self._log("warning", f"{path}[{name}] failed ({exc.cause}); retry {attempt + 1}/{retries}")
        return {"items": len(items)}

    def _k_if(self, step, scope, path):
        branch = "steps" if self._eval(step["if"], scope) else "else"
        self._run_steps(step.get(branch) or [], scope, path)
        return {"branch": branch}

    def _k_tester_present(self, step, scope, path):
        on = step["tester_present"]
        on = on if isinstance(on, bool) else str(on).lower() in ("on", "true", "1", "start")
        if on:
            self.tp.functional = bool(step.get("functional", True))
            self.tp.start()
        else:
            self.tp.stop()
        return {"running": on}

    def _k_delay_ms(self, step, scope, path):
        self._sleep(self._int(step["delay_ms"], scope, "delay_ms") / 1000)

    def _k_wait_ecu(self, step, scope, path):
        cfg = step["wait_ecu"] if isinstance(step["wait_ecu"], dict) else {}
        timeout = int(cfg.get("timeout_ms", 5000)) / 1000
        interval = int(cfg.get("interval_ms", 200)) / 1000
        payload = render_bytes(cfg.get("request", "3E 00"), scope)
        end = time.monotonic() + timeout
        tries = 0
        while True:
            tries += 1
            try:
                resp = self.client.request(payload, timeout_ms=int(cfg.get("response_ms", 100)))
                if resp is not None and resp.positive:
                    return {"tries": tries}
            except (UdsError, TransportError):
                pass
            if time.monotonic() >= end:
                raise StepError(f"ECU did not answer within {timeout * 1000:.0f} ms")
            self._sleep(interval)

    def _k_reset(self, step, scope, path):
        reset_type = self._int(step["reset"], scope, "reset type")
        resp = self.client.ecu_reset(reset_type, **self._req_kwargs(step))
        if step.get("wait_ms"):
            self._sleep(int(step["wait_ms"]) / 1000)
        return self._store_and_check(step, scope, resp.data if resp else None)

    def _k_read_did(self, step, scope, path):
        cfg = step["read_did"] if isinstance(step["read_did"], dict) else {"did": step["read_did"]}
        did = self._int(cfg["did"], scope, "did")
        data = self.client.read_data_by_identifier(did, **self._req_kwargs(step))
        if cfg.get("store") or step.get("store"):
            self.vars[cfg.get("store") or step["store"]] = data
        return {"did": f"{did:04X}", "data": data.hex(" ").upper()}

    def _k_write_did(self, step, scope, path):
        cfg = step["write_did"]
        did = self._int(cfg["did"], scope, "did")
        data = render_bytes(cfg["data"], scope)
        self.client.write_data_by_identifier(did, data, **self._req_kwargs(step))
        return {"did": f"{did:04X}", "data": data.hex(" ").upper()}

    def _k_script(self, step, scope, path):
        """Custom action: a function in a Python file, run only when the project allows custom actions."""
        script = str(step["script"])
        if not self.allow_scripts:
            raise StepError(f"custom action {os.path.basename(script)} is blocked: set 'allow_custom_actions: true' "
                            "on the target to run Python scripts from sequences")
        if not os.path.isfile(script):
            raise StepError(f"custom action not found: {script}")
        import hashlib
        import importlib.util

        with open(script, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        spec = importlib.util.spec_from_file_location(f"flashtool_action_{digest[:12]}", script)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
            fn = getattr(module, str(step.get("function", "run")))
        except Exception as exc:
            raise StepError(f"cannot load custom action {os.path.basename(script)}: {exc}") from exc
        args = {k: render_value(v, scope) for k, v in (step.get("args") or {}).items()}
        ctx = ScriptContext(self, scope, args, self._req_kwargs(step))
        try:
            result = fn(ctx)
        except (Cancelled, *RECOVERABLE):
            raise
        except Exception as exc:
            raise StepError(f"{os.path.basename(script)}: {type(exc).__name__}: {exc}") from exc
        detail = {"script": script, "sha256": digest}
        if isinstance(result, dict):
            detail["result"] = {k: (v.hex(" ").upper() if isinstance(v, (bytes, bytearray)) else v)
                                for k, v in result.items()}
        return detail

    def _k_log(self, step, scope, path):
        self._log("info", str(render_value(step["log"], scope)))


class ScriptContext:
    """What a custom action can do: UDS requests, DIDs, sequence variables, log, wait."""

    def __init__(self, engine: Engine, scope: ChainMap, args: dict, request_kwargs: dict):
        self._engine = engine
        self._scope = scope
        self._kw = request_kwargs
        self.args = args
        self.vars = engine.vars  # values stored here are visible to the following steps
        self.image = engine.image
        self.ecu = engine.job

    def request(self, data: bytes | str, *, functional: bool = False, timeout_ms: int | None = None,
                allow_nrc: tuple[int, ...] = ()) -> bytes | None:
        payload = render_bytes(data, self._scope) if isinstance(data, str) else bytes(data)
        kw = dict(self._kw, functional=functional, allow_nrc=tuple(allow_nrc))
        if timeout_ms is not None:
            kw["timeout_ms"] = timeout_ms
        resp = self._engine.client.request(payload, **kw)
        return resp.data if resp is not None else None

    def read_did(self, did: int) -> bytes:
        return self._engine.client.read_data_by_identifier(did)

    def write_did(self, did: int, data: bytes) -> None:
        self._engine.client.write_data_by_identifier(did, bytes(data))

    def log(self, message: str) -> None:
        self._engine._log("info", str(message))

    def sleep(self, seconds: float) -> None:
        self._engine._sleep(seconds)

    def cancelled(self) -> bool:
        return self._engine.cancel.is_set()
