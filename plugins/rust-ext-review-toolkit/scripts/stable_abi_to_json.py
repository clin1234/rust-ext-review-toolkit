#!/usr/bin/env python3
"""Group CPython Stable ABI additions by version (Python 3.2+).

Layout classification uses traditional abi3, not free-threaded abi3t.
Versions come directly from `added`: these are additions, not cumulative lists.
Only entries added in Python 3.12 or later are included.
Includes abi_only and platform-conditional entries without platform filtering.
Types comprise struct and typedef entries, not exported data objects.
"""

import argparse
import json
from pathlib import Path
import tomllib
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/python/cpython/refs/heads/main/Misc/stable_abi.toml"
)
CATEGORIES = (
    "functions",
    "types_with_stable_layout",
    "types_without_stable_layout",
)


def group_by_version(manifest):
    """Return {version: {category: [sorted names]}} for the abi3 ABI."""
    result = {}

    def add(name, entry, category):
        version = entry["added"]
        if not isinstance(version, str):
            raise ValueError(f"{name}: added must be a version string")
        # Validate before writing and sort numerically, so 3.9 precedes 3.10.
        if tuple(map(int, version.split("."))) < (3, 2):
            return
        bucket = result.setdefault(version, {key: [] for key in CATEGORIES})
        bucket[category].append(name)

    for name, entry in manifest["function"].items():
        add(name, entry, "functions")

    for name, entry in manifest["struct"].items():
        kind = entry["struct_abi_kind"]
        if kind in {"full-abi", "abi3t-opaque"}:
            # abi3t-opaque has a full stable layout in traditional abi3.
            category = "types_with_stable_layout"
        elif kind in {"opaque", "members"}:
            # Stable individual members do not guarantee a stable full layout.
            category = "types_without_stable_layout"
        else:
            raise ValueError(f"{name}: unknown struct_abi_kind {kind!r}")
        add(name, entry, category)

    for name, entry in manifest["typedef"].items():
        # The manifest guarantees typedef size/layout/signature stability.
        add(name, entry, "types_with_stable_layout")

    return {
        version: {key: sorted(names) for key, names in result[version].items()}
        for version in sorted(result, key=lambda v: tuple(map(int, v.split("."))))
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", default=DEFAULT_SOURCE,
        help="stabel_abi.toml (if unspecified, fetch from CPython repo)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("../data/stable_abi.json").resolve(),
        help="Destination JSON file (default: ../data/stable_abi.json)",
    )
    args = parser.parse_args()

    try:
        if args.source.startswith(("https://", "http://")):
            with urlopen(args.source, timeout=30) as response:
                manifest = tomllib.loads(response.read().decode("utf-8"))
        else:
            with open(args.source, "rb") as source:
                manifest = tomllib.load(source)
        grouped = group_by_version(manifest)
        args.output.write_text(json.dumps(grouped, indent=2) + "\n", encoding="utf-8")
    except (OSError, URLError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
