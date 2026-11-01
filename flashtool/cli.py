"""Command line interface: `flashtool <command>`."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__


def _int(text: str) -> int:
    return int(text, 0)


def cmd_flash(args: argparse.Namespace) -> int:
    from .app.manager import JobManager
    from .app.project import ProjectError, load_project
    from .sequence import events as ev

    try:
        project = load_project(args.project)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ids = args.target or [t.id for t in project.targets]
    for tid in ids:
        project.target(tid)
    manager = JobManager()
    manager.start(project.path, ids)
    results: dict[str, ev.JobFinished] = {}
    last_progress: dict[str, float] = {}
    try:
        for e in manager.wait_all():
            if args.json:
                print(json.dumps({"event": type(e).__name__, **_jsonable(e.__dict__)}), flush=True)
                if isinstance(e, ev.JobFinished):
                    results[e.job] = e
                continue
            prefix = f"[{e.job}]"
            if isinstance(e, ev.JobStarted):
                i = e.info
                print(f"{prefix} {e.target}: {i['transport']}{' (simulated ECU)' if i['simulate'] else ''}, "
                      f"{i['blocks']} block(s), {i['total_size']} bytes, sequence '{i['sequence']}'")
            elif isinstance(e, ev.StepFinished):
                if e.status != "ok" or args.verbose:
                    print(f"{prefix} {e.status:8} {e.path}{' - ' + e.error if e.error else ''}")
            elif isinstance(e, ev.Progress):
                now = time.monotonic()
                if now - last_progress.get(e.job, 0) > 0.5 or e.done == e.total:
                    last_progress[e.job] = now
                    print(f"{prefix} {e.block}: {e.block_done}/{e.block_total} bytes  "
                          f"total {100 * e.done / max(e.total, 1):5.1f}%  {e.bytes_per_s / 1024:7.1f} KB/s")
            elif isinstance(e, ev.LogEvent):
                print(f"{prefix} {e.level}: {e.message}")
            elif isinstance(e, ev.Traffic) and args.trace:
                print(f"{prefix} {e.t:9.4f} {e.direction} {e.data.hex(' ').upper()[:96]} {e.note}")
            elif isinstance(e, ev.JobFinished):
                results[e.job] = e
                state = "PASSED" if e.success else "FAILED"
                print(f"{prefix} {state} in {e.duration_s:.2f} s{' - ' + e.error if e.error else ''}")
                if e.report:
                    print(f"{prefix} report: {e.report.replace('.json', '.html')}")
    except KeyboardInterrupt:
        print("cancelling...", file=sys.stderr)
        manager.cancel()
        for e in manager.wait_all():
            if isinstance(e, ev.JobFinished):
                results[e.job] = e
    finally:
        manager.shutdown()
    return 0 if results and all(r.success for r in results.values()) and len(results) == len(ids) else 1


def _jsonable(d: dict) -> dict:
    return {k: (v.hex() if isinstance(v, (bytes, bytearray)) else v) for k, v in d.items()}


def cmd_inspect(args: argparse.Namespace) -> int:
    import os

    from .firmware.loaders import ODX_EXT, FirmwareError, FirmwareSpec, load_firmware, odx_sessions
    from .firmware.validate import validate_image

    specs = []
    try:
        for f in args.files:
            is_odx = os.path.splitext(f)[1].lower() in ODX_EXT
            specs.append(FirmwareSpec(file=f, base_address=args.base, session=args.session if is_odx else None))
            if is_odx and os.path.isfile(f):  # list the sessions even when the choice is ambiguous
                sessions = odx_sessions(f)
                print(f"{f}: {len(sessions)} ODX-F session(s)")
                for s in sessions:
                    chosen = args.session in (s.name, s.label, f"{s.ecu_mem}/{s.name}") \
                        if args.session else len(sessions) == 1
                    print(f"  {'*' if chosen else ' '} {s.label}  (ECU-MEM {s.ecu_mem}): {', '.join(s.datablocks) or '-'}")
        image = load_firmware(specs)
    except FirmwareError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for src in image.sources:
        print(f"{src.path}  {src.size} bytes  sha256 {src.sha256}")
    for block in image.blocks:
        raw_type = block.metadata.get("type")
        kind = block.type + (f" ({raw_type})" if raw_type and str(raw_type).upper() != block.type else "")
        index = "" if block.index is None else f", logical block {block.index}"
        print(f"block {block.name} [{kind}{index}]: 0x{block.start:08X} - 0x{block.end - 1:08X}, "
              f"{block.size} bytes, DFI 0x{block.data_format_identifier:02X}, "
              f"ALFID 0x{block.address_and_length_format:02X}, "
              f"memory_size {'-' if block.memory_size is None else block.memory_size}, "
              f"signature {'yes' if block.signature else 'no'}, CRC32 {block.crc32:08X}")
        idents = block.metadata.get("own_idents")
        if idents:  # part numbers / versions from the file (VBF header, ODX-F OWN-IDENTS)
            print("  " + ", ".join(f"{k}={v}" for k, v in idents.items()))
        for seg in block.segments:
            print(f"  0x{seg.address:08X} - 0x{seg.end - 1:08X}  {len(seg.data):8d} bytes")
    issues = validate_image(image)
    for issue in issues:
        print(issue)
    return 1 if any(i.severity == "error" for i in issues) else 0


def cmd_sequence(args: argparse.Namespace) -> int:
    from .app.job import JobSetupError, prepare
    from .app.project import ProjectError, load_project
    from .sequence.loader import block_sequence_key, describe_step, flatten

    try:
        project = load_project(args.project)
        target = project.target(args.target or project.targets[0].id)
        image, seq, warnings = prepare(project, target)
    except (ProjectError, JobSetupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{seq['name']}  ({' <- '.join(reversed(seq['sources']))})")
    for name, value in seq["variables"].items():
        print(f"  var {name} = {f'0x{value:02X}' if isinstance(value, int) and not isinstance(value, bool) else repr(value)}")
    sections = [("steps", seq["steps"])] + [(f"block sequence {name}", steps)
                                            for name, steps in seq["block_sequences"].items()]
    for title, steps in sections + [("finally", seq["finally"])]:
        print(f"{title}:")
        for depth, step in flatten(steps):
            print(f"  {'  ' * depth}- {step.get('id', '?')}: {describe_step(step)}")
    if seq["block_sequences"]:
        print("blocks:")
        for block in image.blocks:
            print(f"  - {block.name} [{block.type}] -> {block_sequence_key(seq, block.type) or 'no block sequence'}")
    for w in warnings:
        print(f"warning: {w}")
    return 0


def cmd_hw(args: argparse.Namespace) -> int:
    from .hal.backends import list_channels

    channels = list_channels()
    if not channels:
        print("No CAN channels detected (Vector XL needs Windows + Vector drivers; SocketCAN needs Linux).")
        return 0
    for c in channels:
        print(", ".join(f"{k}={v}" for k, v in c.items()))
    return 0


def cmd_sim_doip(args: argparse.Namespace) -> int:
    from .sim.doip_server import DoIpEcuServer
    from .sim.ecu import SimulatedEcu

    server = DoIpEcuServer(SimulatedEcu(), host=args.host, port=args.port, ecu_address=args.ecu_address).start()
    print(f"Simulated DoIP ECU 0x{args.ecu_address:04X} on {args.host}:{server.port} - Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    server.stop()
    return 0


def cmd_sim_pki(args: argparse.Namespace) -> int:
    from .sim.pki import make_test_pki

    for name, path in make_test_pki(args.out_dir, args.cn).items():
        print(f"{name}: {path}")
    print("Test PKI for the simulator only - never use these keys with real ECUs.")
    return 0


def cmd_audit_verify(args: argparse.Namespace) -> int:
    from .app import audit

    ok, count, problem = audit.verify(args.file)
    print(f"{count} entries, chain {'intact' if ok else 'BROKEN: ' + str(problem)}")
    return 0 if ok else 1


def cmd_pack(args: argparse.Namespace) -> int:
    import os

    from .app.package import PACK_EXT, PackageError, export_pack, read_manifest

    out = args.output or os.path.splitext(args.project)[0] + PACK_EXT
    try:
        path = export_pack(args.project, out, sealed=args.sealed, include_private_keys=args.include_private_keys)
        manifest = read_manifest(path)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    size = sum(e["size"] for e in manifest["files"])
    print(f"{path}: {len(manifest['files'])} file(s), {size} bytes{', SEALED' if manifest['sealed'] else ''}")
    for e in manifest["files"]:
        print(f"  {e['path']}  ({e['kind']}, {e['size']} bytes)")
    for note in manifest["notes"]:
        print(f"note: {note}")
    return 0


def cmd_unpack(args: argparse.Namespace) -> int:
    from .app.package import PackageError, import_pack, read_manifest

    try:
        project = import_pack(args.pack, args.dest)
        manifest = read_manifest(args.pack)
    except PackageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"project: {project}{' (sealed, read-only)' if manifest['sealed'] else ''}")
    for note in manifest["notes"]:
        print(f"note: {note}")
    return 0


def cmd_verify_pack(args: argparse.Namespace) -> int:
    from .app.package import PackageError, read_manifest, verify_pack

    problems = verify_pack(args.pack)
    for p in problems:
        print(f"FAIL: {p}")
    if problems:
        return 1
    try:
        manifest = read_manifest(args.pack)
    except PackageError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"OK: {len(manifest['files'])} file(s) match the manifest - project '{manifest['project']['name']}', "
          f"{manifest['tool']}, {manifest['created']}{', sealed' if manifest['sealed'] else ''}")
    for e in manifest.get("excluded") or []:
        print(f"note: {e['path']}: not in the pack ({e['kind']})")
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui.main_window import run
    except ImportError as exc:
        print(f"GUI needs PySide6 (pip install 'flashtool[gui]'): {exc}", file=sys.stderr)
        return 2
    return run(args.project)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="flashtool", description="ECU flashing over UDS (CAN, CAN FD, DoIP)")
    p.add_argument("--version", action="version", version=f"flashtool {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("flash", help="flash the targets of a project (each in its own process)")
    f.add_argument("project")
    f.add_argument("-t", "--target", action="append", help="target id (repeatable, default: all)")
    f.add_argument("-v", "--verbose", action="store_true", help="show every step")
    f.add_argument("--trace", action="store_true", help="print UDS traffic")
    f.add_argument("--json", action="store_true", help="one JSON event per line")
    f.set_defaults(func=cmd_flash)

    i = sub.add_parser("inspect", help="show segments and checks of firmware files")
    i.add_argument("files", nargs="+")
    i.add_argument("--base", type=_int, help="base address for .bin files")
    i.add_argument("--session", help="ODX-F SESSION of .pdx / .odx-f files (needed when there are several)")
    i.set_defaults(func=cmd_inspect)

    s = sub.add_parser("sequence", help="print the resolved sequence of a target")
    s.add_argument("project")
    s.add_argument("-t", "--target")
    s.set_defaults(func=cmd_sequence)

    h = sub.add_parser("hw", help="list detected CAN channels")
    h.set_defaults(func=cmd_hw)

    d = sub.add_parser("sim-doip", help="run a simulated DoIP ECU")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=13400)
    d.add_argument("--ecu-address", type=_int, default=0x0010)
    d.set_defaults(func=cmd_sim_doip)

    k = sub.add_parser("sim-pki", help="create a test root CA + tester certificate for the simulator")
    k.add_argument("out_dir")
    k.add_argument("--cn", default="FlashTool tester")
    k.set_defaults(func=cmd_sim_pki)

    a = sub.add_parser("audit-verify", help="check the hash chain of an audit log")
    a.add_argument("file")
    a.set_defaults(func=cmd_audit_verify)

    pk = sub.add_parser("pack", help="pack a project and every file it references into one .ftpack")
    pk.add_argument("project")
    pk.add_argument("-o", "--output", help="output .ftpack (default: next to the project)")
    pk.add_argument("--sealed", action="store_true", help="read-only pack: any change fails verification")
    pk.add_argument("--include-private-keys", action="store_true",
                    help="also pack certificate private keys (left out by default)")
    pk.set_defaults(func=cmd_pack)

    up = sub.add_parser("unpack", help="verify a .ftpack and unpack it into a new or empty folder")
    up.add_argument("pack")
    up.add_argument("dest")
    up.set_defaults(func=cmd_unpack)

    vp = sub.add_parser("verify-pack", help="check a .ftpack (or an unpacked folder) against its manifest")
    vp.add_argument("pack")
    vp.set_defaults(func=cmd_verify_pack)

    g = sub.add_parser("gui", help="open the graphical interface")
    g.add_argument("project", nargs="?")
    g.set_defaults(func=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()  # job processes in frozen (PyInstaller) builds
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
