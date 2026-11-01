"""Requirement -> test traceability matrix (docs/requirements.yaml + a pytest run).

    python -m pytest --trace-json=out/trace.json
    python tools/traceability.py out/trace.json -o out/traceability.md
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(requirements: dict, results: list[dict]) -> tuple[str, bool]:
    lines = [
        "# Requirement traceability",
        "",
        "| Requirement | Title | Verification | Tests | Passed | Failed | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    ok = True
    unknown = sorted({r for res in results for r in res["reqs"]} - set(requirements))
    for rid, req in requirements.items():
        tests = [r for r in results if rid in r["reqs"]]
        passed = sum(r["outcome"] == "passed" for r in tests)
        failed = sum(r["outcome"] == "failed" for r in tests)
        if failed:
            status, ok = "FAILED", False
        elif tests:
            status = "covered"
        elif req.get("verification") == "test":
            status, ok = "NOT COVERED", False
        else:
            status = f"needs {req.get('verification')}"
        lines.append(f"| {rid} | {req['title']} | {req.get('verification')} | {len(tests)} | {passed} | {failed} | {status} |")
    untraced = [r["test"] for r in results if not r["reqs"]]
    if unknown:
        ok = False
        lines += ["", "Unknown requirement ids used by tests: " + ", ".join(unknown)]
    if untraced:
        lines += ["", f"Tests without a requirement ({len(untraced)}):", ""] + [f"- `{t}`" for t in untraced]
    lines += ["", "## Tests per requirement", ""]
    for rid in requirements:
        tests = [r for r in results if rid in r["reqs"]]
        if tests:
            lines.append(f"### {rid}")
            lines += [f"- {r['outcome']}: `{r['test']}`" for r in tests]
            lines.append("")
    return "\n".join(lines) + "\n", ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results", help="JSON written by pytest --trace-json")
    p.add_argument("-r", "--requirements", default=os.path.join(ROOT, "docs", "requirements.yaml"))
    p.add_argument("-o", "--output", help="markdown file (default: stdout)")
    args = p.parse_args()
    with open(args.requirements) as f:
        requirements = yaml.safe_load(f)
    with open(args.results) as f:
        results = json.load(f)
    text, ok = build(requirements, results)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text)
        print(f"written {args.output} - {'all requirements verified' if ok else 'GAPS FOUND'}")
    else:
        print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
