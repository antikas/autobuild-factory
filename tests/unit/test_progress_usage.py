"""Every usage gate event writes one progress line naming the item, the seat and
the lane, so a lane stopped for usage or a seat held on a failed meter reading
is visible in the progress log."""

from __future__ import annotations

from autobuild.application.progress import render_progress_line
from autobuild.domain import RunEvent

STAMP = "2026-11-21T03:07:37+00:00"
RESET = "2026-11-23T06:00:00+00:00"


def line(event_type: str, payload: dict[str, object]) -> str:
    return render_progress_line(RunEvent(event_type, STAMP, "item-demo", payload=payload))


def assert_one_ascii_line(text: str) -> None:
    assert text
    assert "\n" not in text and "\r" not in text
    assert text.isascii() and text.isprintable()
    assert "item-demo" in text and "claude-code" in text and "builder" in text


def failed_payload(action: str, **extra: object) -> dict[str, object]:
    return {
        "lane": "claude-code",
        "meter_id": "meter-a",
        "seat": "builder",
        "error": "meter returned error rate_limit_error",
        "action": action,
        "retry_seconds": 300.0 if action in ("retry", "hold") else None,
        **extra,
    }


def test_a_held_seat_line_names_the_error_the_last_good_reading_and_the_hold() -> None:
    text = line(
        "usage.meter_failed",
        failed_payload(
            "hold",
            last_good={
                "kind": "weekly_all",
                "scope": None,
                "percent": 86,
                "resets_at": RESET,
                "fetched_at": "2026-11-21T03:00:00+00:00",
            },
            warn_percent=85,
            stop_percent=90,
        ),
    )

    assert_one_ascii_line(text)
    assert "holding" in text
    assert "rate_limit_error" in text
    assert "weekly_all 86%" in text
    assert "warning level 85%" in text
    assert "until a reading succeeds" in text


def test_a_held_seat_line_at_the_stop_names_the_stop_level() -> None:
    text = line(
        "usage.meter_failed",
        failed_payload(
            "hold",
            last_good={"kind": "weekly_all", "percent": 95, "resets_at": None, "fetched_at": ""},
            warn_percent=85,
            stop_percent=90,
        ),
    )

    assert_one_ascii_line(text)
    assert "weekly_all 95%" in text and "stop level 90%" in text


def test_the_allow_retry_and_stop_lines_name_the_error_and_what_the_run_does() -> None:
    allow = line("usage.meter_failed", failed_payload("allow"))
    retry = line("usage.meter_failed", failed_payload("retry"))
    stop = line("usage.meter_failed", failed_payload("stop"))

    for text in (allow, retry, stop):
        assert_one_ascii_line(text)
        assert "rate_limit_error" in text
    assert "starts" in allow
    assert "300 s" in retry
    assert "ends" in stop


def test_the_warning_line_names_the_kind_the_percent_and_the_warning_level() -> None:
    text = line(
        "usage.warning",
        {
            "lane": "claude-code",
            "meter_id": "meter-a",
            "seat": "builder",
            "warn_percent": 75,
            "kind": "weekly_all",
            "scope": None,
            "percent": 76,
            "resets_at": RESET,
        },
    )

    assert_one_ascii_line(text)
    assert "weekly_all 76%" in text and "75%" in text


def test_the_stop_line_names_the_lane_stopped_at_its_usage_level_with_the_detail() -> None:
    detail = "weekly_all 80%, resets 2026-11-23T06:00:00 (stop 80%)"
    text = line(
        "usage.stop",
        {
            "lane": "claude-code",
            "meter_id": "meter-a",
            "seat": "builder",
            "limits": [{"kind": "weekly_all", "scope": None, "percent": 80, "resets_at": RESET}],
            "reset_at": RESET,
            "detail": detail,
        },
    )

    assert_one_ascii_line(text)
    assert "stopped" in text and detail in text and "80%" in text


def test_the_reroute_line_names_the_seat_moved_past_the_lane_for_a_scoped_limit() -> None:
    text = line(
        "usage.seat_rerouted",
        {
            "lane": "claude-code",
            "meter_id": "meter-a",
            "seat": "builder",
            "model_class": "builder-class",
            "limits": [{"kind": "weekly_scoped", "scope": "Fable", "percent": 92, "resets_at": RESET}],
        },
    )

    assert_one_ascii_line(text)
    assert "scoped" in text and "92%" in text and "moves past" in text


def test_an_error_with_a_line_break_or_non_ascii_text_stays_one_ascii_line() -> None:
    text = line("usage.meter_failed", failed_payload("allow", error="rate\nlimited \u2014 later"))

    assert_one_ascii_line(text)


def test_the_usage_stop_line_names_usage_the_end_of_admissions_and_the_reset() -> None:
    text = render_progress_line(
        RunEvent(
            "campaign.usage_stop",
            STAMP,
            payload={
                "lanes": [
                    {"lane": "claude-code", "cooled_until": RESET, "signature": "usage"}
                ],
                "resets_at": RESET,
                "running": ["item-demo"],
            },
        )
    )

    assert text and "\n" not in text and text.isascii()
    assert "every lane is stopped for usage" in text
    assert "claude-code" in text
    assert "no new item is admitted" in text
    assert RESET in text


def test_the_completed_line_names_the_usage_stop() -> None:
    text = render_progress_line(
        RunEvent(
            "campaign.completed",
            STAMP,
            payload={
                "stop_reason": "usage_stop",
                "accepted": 1,
                "parked": 1,
                "failed": 0,
                "report": "docs/campaigns/c.md",
                "batch_validation": "none",
            },
        )
    )

    assert text.endswith("stop usage_stop")


def test_a_cleared_usage_cooling_line_names_the_lane_the_campaign_and_the_reading() -> None:
    text = line(
        "lane.usage_cooling_cleared",
        {
            "lane": "claude-code",
            "meter_id": "meter-a",
            "campaign_id": "autobuild-20261120T220510Z",
            "cooled_until": RESET,
            "percent": 81,
        },
    )

    assert "claude-code" in text and "autobuild-20261120T220510Z" in text
    assert "81%" in text and "cleared" in text
