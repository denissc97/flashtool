"""Helper process for BridgeSeedKey: loads a seed&key provider and serves requests.

Run with an interpreter whose bitness matches the library, e.g. a 32-bit Python
for a 32-bit OEM DLL. Only depends on the standard library and this package.
"""

from __future__ import annotations

import json
import sys

from .seedkey import SeedKeyConfig, create_seedkey_provider


def main() -> int:
    try:
        cfg = SeedKeyConfig(**json.loads(sys.argv[1]))
        provider = create_seedkey_provider(cfg)
        if provider is None:
            raise ValueError("bridge target provider is 'none'")
    except Exception as exc:
        print(json.dumps({"ready": False, "error": str(exc)}), flush=True)
        return 1
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        try:
            req = json.loads(line)
            key = provider.compute_key(bytes.fromhex(req["seed"]), int(req["level"]), req.get("variant", ""))
            print(json.dumps({"key": key.hex()}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
    provider.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
