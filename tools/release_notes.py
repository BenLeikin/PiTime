#!/usr/bin/env python3
"""Print one version's release notes from CHANGELOG.md.

    tools/release_notes.py 2.2.0          notes for v2.2.0
    tools/release_notes.py --latest       notes for the newest version

The output is ready to paste into a GitHub or GitLab release: the version's
section without its heading (the release already has a title), followed by a
link to the full changelog.
"""

import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
HEADING = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\S+))?")


def sections(text):
    """[(version, date, body)] for each '## [x.y.z] - date' section, newest first."""
    found, current = [], None
    for line in text.splitlines():
        match = HEADING.match(line)
        if match:
            current = [match["version"], match["date"], []]
            found.append(current)
        elif current is not None:
            current[2].append(line)
    return [(v, d, "\n".join(body).strip()) for v, d, body in found]


def notes(text, version=None):
    """Release notes for `version` (default: the newest), or None if absent."""
    for v, _date, body in sections(text):
        if version is None or v == version.lstrip("v"):
            footer = "Full history: [CHANGELOG.md](CHANGELOG.md)"
            return f"{body}\n\n{footer}\n" if body else f"{footer}\n"
    return None


def main(argv=None):
    """Print the notes; exit status 1 if the version is not in the changelog."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", nargs="?", help="for example 2.2.0 or v2.2.0")
    parser.add_argument("--latest", action="store_true", help="the newest version")
    args = parser.parse_args(argv)
    if not args.version and not args.latest:
        parser.error("give a version or --latest")
    text = CHANGELOG.read_text()
    result = notes(text, None if args.latest else args.version)
    if result is None:
        print(f"version {args.version} is not in {CHANGELOG.name}", file=sys.stderr)
        return 1
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
