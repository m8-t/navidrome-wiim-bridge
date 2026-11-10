import re
from pathlib import Path

import bridge

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def test_version_matches_the_newest_changelog_section() -> None:
    headings = re.findall(
        r"^## \[(\d+\.\d+\.\d+)\]", CHANGELOG.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert headings
    assert bridge.__version__ == headings[0]
