"""Per-run report (JSON + HTML) - docs/ARCHITECTURE.md §4.10."""

from __future__ import annotations

import html
import json
import os
from dataclasses import asdict


def write_report(run_dir: str, report: dict) -> str:
    os.makedirs(run_dir, exist_ok=True)
    json_path = os.path.join(run_dir, "report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=_default)
    with open(os.path.join(run_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(render_html(report))
    return json_path


def _default(o):
    if hasattr(o, "__dataclass_fields__"):
        return asdict(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex(" ").upper()
    return str(o)


def _rows(items: list[tuple[str, object]]) -> str:
    return "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in items)


def render_html(r: dict) -> str:
    ok = r.get("success")
    status = "PASSED" if ok else "FAILED"
    steps = "".join(
        f"<tr class='{html.escape(s['status'])}'><td>{html.escape(s['path'])}</td><td>{html.escape(s['kind'])}</td>"
        f"<td>{html.escape(s['status'])}</td><td class='num'>{s['duration_s']:.3f}</td>"
        f"<td class='num'>{s['attempts']}</td><td>{html.escape(s.get('error') or '')}</td></tr>"
        for s in r.get("steps", [])
    )
    blocks = "".join(
        f"<tr><td>{html.escape(b['name'])}</td><td class='mono'>0x{b['start']:08X}–0x{b['end']:08X}</td>"
        f"<td class='num'>{b['size']}</td><td class='mono'>0x{b['dfi']:02X}</td><td class='mono'>0x{b['alfid']:02X}</td>"
        f"<td class='mono'>{b['crc32']:08X}</td></tr>"
        for b in r.get("firmware", {}).get("blocks", [])
    )
    files = "".join(
        f"<tr><td>{html.escape(f['path'])}</td><td class='num'>{f['size']}</td><td class='mono'>{f['sha256']}</td></tr>"
        for f in r.get("firmware", {}).get("sources", [])
    )
    warnings = "".join(f"<li>{html.escape(w)}</li>" for w in r.get("warnings", []))
    summary = _rows([
        ("Project", r.get("project")), ("Target", r.get("target")), ("Sequence", r.get("sequence", {}).get("name")),
        ("Started", r.get("started")), ("Duration", f"{r.get('duration_s', 0):.2f} s"),
        ("Bytes transferred", r.get("firmware", {}).get("total_size")),
        ("Error", r.get("error") or "-"), ("Tool", r.get("tool")),
    ])
    transport = _rows(list((r.get("transport") or {}).items()))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Flash report – {html.escape(str(r.get('target')))}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#17212b;background:#fff}}
h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:16px;margin:24px 0 8px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:3px;color:#fff;background:{'#2f7d5b' if ok else '#b42318'};font-weight:600}}
table{{border-collapse:collapse;width:100%;max-width:1100px}} th,td{{text-align:left;padding:4px 8px;border-bottom:1px solid #dde3e8;vertical-align:top}}
th{{width:180px;color:#5b6875;font-weight:500}} thead th{{width:auto;color:#17212b;border-bottom:2px solid #8e9aa6}}
.num{{text-align:right;font-variant-numeric:tabular-nums}} .mono{{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}}
tr.failed td{{background:#fbe0de}} tr.warning td{{background:#fdf1d8}} tr.skipped td{{color:#8e9aa6}}
</style></head><body>
<h1>Flash report <span class="badge">{status}</span></h1>
<table>{summary}</table>
{f'<h2>Warnings</h2><ul>{warnings}</ul>' if warnings else ''}
<h2>Transport</h2><table>{transport}</table>
<h2>Firmware files</h2><table><thead><tr><th>File</th><th>Size</th><th>SHA-256</th></tr></thead>{files}</table>
<h2>Blocks</h2><table><thead><tr><th>Block</th><th>Range</th><th>Bytes</th><th>DFI</th><th>ALFID</th><th>CRC32</th></tr></thead>{blocks}</table>
<h2>Executed steps</h2><table><thead><tr><th>Step</th><th>Type</th><th>Result</th><th>Seconds</th><th>Attempts</th><th>Error</th></tr></thead>{steps}</table>
</body></html>"""
