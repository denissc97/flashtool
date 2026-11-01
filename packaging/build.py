"""Builds the stand-alone application, its SBOM and a checksum list.

    python packaging/build.py            -> dist/flashtool/, dist/sbom.cdx.json, dist/SHA256SUMS
"""

import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=HERE)


def main() -> int:
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", DIST,
         "--workpath", os.path.join(ROOT, "build"), "flashtool.spec"])
    # Software bill of materials of the Python environment that was frozen (ISO/SAE 21434 SOUP list).
    run([sys.executable, "-m", "cyclonedx_py", "environment", "--output-file",
         os.path.join(DIST, "sbom.cdx.json"), sys.executable])
    sums = []
    for base, _, files in os.walk(os.path.join(DIST, "flashtool")):
        for name in sorted(files):
            path = os.path.join(base, name)
            with open(path, "rb") as f:
                sums.append(f"{hashlib.sha256(f.read()).hexdigest()}  {os.path.relpath(path, DIST)}")
    with open(os.path.join(DIST, "SHA256SUMS"), "w") as f:
        f.write("\n".join(sorted(sums, key=lambda s: s[66:])) + "\n")
    print(f"done: {DIST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
