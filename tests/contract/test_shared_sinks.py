"""Shared run-record and progress files under concurrent writers."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from autobuild.adapters import LocalRunRecordAdapter
from autobuild.adapters.progress import FileProgressAdapter
from autobuild.domain import CampaignRef, RunEvent

WRITERS = ("first", "second")
PER_WRITER = 200
WAIT_SECONDS = 10


def write_together(write) -> None:
    barrier = threading.Barrier(len(WRITERS), timeout=WAIT_SECONDS)
    errors: list[BaseException] = []

    def writer(name: str) -> None:
        try:
            barrier.wait()
            for index in range(PER_WRITER):
                write(name, index)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(name,)) for name in WRITERS]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_SECONDS)
    assert errors == []


def sequence(ref: str) -> int:
    return int(ref.rsplit("#L", 1)[1])


def test_two_writers_get_gapless_unique_event_numbers_that_name_their_lines(tmp_path: Path) -> None:
    records = LocalRunRecordAdapter(tmp_path / "runs")
    record = records.create(CampaignRef("campaign", tmp_path))
    numbered: dict[int, tuple[str, int]] = {}
    guard = threading.Lock()

    def write(name: str, index: int) -> None:
        ref = records.append(record, RunEvent("test.event", item_id=name, payload={"index": index}))
        with guard:
            assert sequence(ref) not in numbered
            numbered[sequence(ref)] = (name, index)

    write_together(write)

    total = len(WRITERS) * PER_WRITER
    assert sorted(numbered) == list(range(1, total + 1))
    lines = (record.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == total
    for number, line in enumerate(lines, start=1):
        event = json.loads(line)
        assert (event["item_id"], event["payload"]["index"]) == numbered[number]


def test_a_new_adapter_appending_to_an_existing_record_continues_from_its_last_number(
    tmp_path: Path,
) -> None:
    first = LocalRunRecordAdapter(tmp_path / "runs")
    record = first.create(CampaignRef("campaign", tmp_path))
    write_together(lambda name, index: first.append(record, RunEvent("test.event", item_id=name)))

    second = LocalRunRecordAdapter(tmp_path / "runs")
    ref = second.append(record, RunEvent("test.next"))

    assert sequence(ref) == len(WRITERS) * PER_WRITER + 1
    assert sequence(second.append(record, RunEvent("test.next"))) == len(WRITERS) * PER_WRITER + 2


def test_two_writers_to_the_progress_log_leave_every_line_whole(tmp_path: Path) -> None:
    progress = FileProgressAdapter()
    record = LocalRunRecordAdapter(tmp_path / "runs").create(CampaignRef("campaign", tmp_path))
    progress.begin(record)

    write_together(lambda name, index: progress.emit(f"line {name} {index} " + "x" * 200))

    lines = (record.root / "progress.log").read_text(encoding="utf-8").splitlines()
    expected = {f"line {name} {index} " + "x" * 200 for name in WRITERS for index in range(PER_WRITER)}
    assert len(lines) == len(expected)
    assert set(lines) == expected
