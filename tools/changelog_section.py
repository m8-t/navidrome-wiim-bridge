"""Print the body of one version section from a Keep a Changelog file."""

import argparse
import re
import sys
from pathlib import Path

_LINK_REF = re.compile(r"^\[[^\]]+\]: ")


def section(text: str, version: str) -> str:
    version = version.removeprefix("v")
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    lines = text.splitlines()
    for start, line in enumerate(lines):
        if heading.match(line):
            break
    else:
        raise LookupError(f"no section for version {version}")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## [") or _LINK_REF.match(line):
            break
        body.append(line)
    result = "\n".join(body).strip("\n")
    if not result.strip():
        raise LookupError(f"section for version {version} is empty")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--file", default="CHANGELOG.md")
    args = parser.parse_args(argv)
    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 1
    try:
        print(section(text, args.version))
    except LookupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
