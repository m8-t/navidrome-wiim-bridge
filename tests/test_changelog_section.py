import re
from pathlib import Path

import changelog_section as cs
import pytest

ROOT = Path(__file__).resolve().parent.parent

SAMPLE = """# Changelog

Intro with [a link](https://example.com).

## [0.3.0] - 2026-01-03

- third

## [0.2.1] - 2026-01-02

### Fixed

- middle one
- middle two

## [0.2.0] - 2026-01-01

- last item

[0.3.0]: https://example.com/0.3.0
[0.2.1]: https://example.com/0.2.1
"""


def test_middle_section():
    assert cs.section(SAMPLE, "0.2.1") == "### Fixed\n\n- middle one\n- middle two"


def test_last_section_excludes_links():
    assert cs.section(SAMPLE, "0.2.0") == "- last item"


def test_first_section():
    assert cs.section(SAMPLE, "0.3.0") == "- third"


def test_v_prefix():
    assert cs.section(SAMPLE, "v0.3.0") == "- third"


def test_unknown_version():
    with pytest.raises(LookupError):
        cs.section(SAMPLE, "9.9.9")


def test_prefix_does_not_match():
    with pytest.raises(LookupError):
        cs.section(SAMPLE, "0.2")


def test_empty_section():
    text = "## [1.0.0] - x\n\n\n## [0.9.0] - x\n\n- a\n"
    with pytest.raises(LookupError):
        cs.section(text, "1.0.0")


def test_cli_exit_codes(tmp_path, capsys):
    f = tmp_path / "CL.md"
    f.write_text(SAMPLE, encoding="utf-8")
    assert cs.main(["v0.3.0", "--file", str(f)]) == 0
    assert capsys.readouterr().out == "- third\n"
    assert cs.main(["9.9.9", "--file", str(f)]) == 1
    assert "error" in capsys.readouterr().err
    assert cs.main(["0.3.0", "--file", str(tmp_path / "missing.md")]) == 1
    assert "error" in capsys.readouterr().err


def test_real_changelog_sections_non_empty():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    versions = re.findall(r"^## \[([^\]]+)\]", text, flags=re.M)
    assert versions
    for version in versions:
        assert cs.section(text, version).strip(), version
