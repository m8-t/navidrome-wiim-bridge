import asyncio
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from bridge.radio import (
    bucket_genres,
    collect_mix,
    is_instrumental,
    is_interlude,
    merge_random,
)

CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "mix_cases.json").read_text(encoding="utf-8")
)


def cycle_rng(seq: list[float]) -> Callable[[], float]:
    values = itertools.cycle(seq)
    return lambda: next(values)


@pytest.mark.parametrize("case", CASES["songs"], ids=lambda c: json.dumps(c["song"]))
def test_song_filters(case: dict[str, Any]) -> None:
    assert is_interlude(case["song"]) is case["interlude"]
    assert is_instrumental(case["song"]) is case["instrumental"]


@pytest.mark.parametrize("case", CASES["genres"], ids=lambda c: c["name"])
def test_bucket_genres(case: dict[str, Any]) -> None:
    assert bucket_genres(case["raw"]) == case["buckets"]


@pytest.mark.parametrize("case", CASES["merge"], ids=lambda c: c["name"])
def test_merge_random(case: dict[str, Any]) -> None:
    out = merge_random(case["lists"], case["count"], cycle_rng(case["rng"]))
    assert [s["id"] for s in out] == case["expected"]


@pytest.mark.parametrize("case", CASES["collect"], ids=lambda c: c["name"])
def test_collect_mix(case: dict[str, Any]) -> None:
    rounds = iter(range(10**6))

    async def fetch_round() -> list[list[dict[str, Any]]]:
        return case["rounds"][min(next(rounds), len(case["rounds"]) - 1)]  # type: ignore[no-any-return]

    songs, short = asyncio.run(
        collect_mix(
            fetch_round,
            case["count"],
            case["skipInterludes"],
            case["skipInstrumentals"],
            cycle_rng(case["rng"]),
        )
    )
    assert [s["id"] for s in songs] == case["expected"]
    assert short is case["short"]
