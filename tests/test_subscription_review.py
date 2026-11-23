from __future__ import annotations

import json
from pathlib import Path

import pytest

from autobuild.bootstrap.subscription_review import _checkout, _store_receipt


def test_checkout_requires_a_full_sha_before_touching_git(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="full 40-character SHA"):
        with _checkout(tmp_path, "deadbeef", tmp_path):
            pass


def test_review_receipt_returns_a_stable_identifier_without_local_paths(tmp_path: Path) -> None:
    example = tmp_path / "src" / "autobuild" / "example.py"
    example.parent.mkdir(parents=True)
    example.write_text("pass\n")
    receipt = _store_receipt(
        tmp_path,
        {
            "commit": "a" * 40,
            "verdict": "pass",
            "lane": "codex",
            "findings": [{
                "code": "EXAMPLE",
                "consequence": f"local diagnostic at {tmp_path / 'raw.log'}",
                "evidence_ref": "src/autobuild/example.py:12",
                "blocking": True,
            }],
        },
        tmp_path,
    )

    assert set(receipt) == {"commit", "verdict", "lane", "model", "receipt_id"}
    assert len(receipt["receipt_id"]) == 32
    assert str(tmp_path) not in json.dumps(receipt)
    stored = tmp_path / "subscription-reviews" / "receipts" / f"{receipt['receipt_id']}.json"
    assert stored.is_file()
    details = json.loads(stored.read_text(encoding="utf-8"))
    assert details["findings"][0]["evidence_ref"] == "src/autobuild/example.py:12"
    assert str(tmp_path) not in details["findings"][0]["consequence"]
    assert details["findings"][0]["consequence"].startswith("Local diagnostic withheld")


@pytest.mark.parametrize("path", ["/Users/alice/log.txt", "C:/Users/alice/log.txt", r"C:\\Users\\alice\\log.txt"])
def test_review_receipt_withholds_each_host_path_form(tmp_path: Path, path: str) -> None:
    receipt = _store_receipt(tmp_path, {"commit": "a" * 40, "verdict": "pass", "lane": "codex", "findings": [{"code": "X", "consequence": "ok", "evidence_ref": path, "blocking": True}]}, tmp_path)
    stored = json.loads((tmp_path / "subscription-reviews" / "receipts" / f"{receipt['receipt_id']}.json").read_text())
    assert stored["findings"][0]["evidence_ref"].startswith("Local diagnostic withheld")


def test_receipt_keeps_only_checked_source_citations_and_portable_prose(tmp_path):
    repo = tmp_path / "reviewed"
    repo.mkdir()
    (repo / "example.py").write_text("pass\n")
    findings = [
        {"code": "EXAMPLE", "blocking": True, "specialist_boundary": None,
         "evidence_ref": "example.py:1; example.py:4-7",
         "consequence": "The value can be missing."},
        {"code": "EXAMPLE", "blocking": True, "specialist_boundary": "see=/var/tmp/private.log",
         "evidence_ref": "example.py:1=/srv/private.log",
         "consequence": "Read path=/home/private/details."},
        {"code": "EXAMPLE", "blocking": True, "specialist_boundary": None,
         "evidence_ref": "missing.py:2",
         "consequence": "See `/var/local/private.log`."},
        {"code": "EXAMPLE", "blocking": True, "specialist_boundary": None,
         "evidence_ref": "example.py:1",
         "consequence": r"The diagnostic is at=C:\Users\private\note.txt."},
    ]
    result = _store_receipt(tmp_path, {"commit": "a" * 40, "verdict": "correct",
                            "lane": "codex", "findings": findings}, repo)
    p = tmp_path / "subscription-reviews" / "receipts" / (result["receipt_id"] + ".json")
    text = p.read_text()
    details = json.loads(text)["findings"]
    assert details[0] == findings[0]
    assert "private" not in text
    assert "missing.py" not in text


def test_review_cli_suppresses_local_paths_in_unexpected_errors(tmp_path, monkeypatch, capsys):
    import argparse
    import subprocess
    from autobuild import cli

    monkeypatch.setattr(cli, "load_settings", lambda *args: object())
    args = argparse.Namespace(repository=str(tmp_path), profile=None, scratch_root=str(tmp_path),
                              commit="a" * 40, brief=str(tmp_path / "brief.md"), timeout_seconds=1)
    def failing_review(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git", "-C", str(tmp_path), "show"])
    assert cli._subscription_review(args, review=failing_review) == 2
    error = capsys.readouterr().err
    assert "review" in error.lower()
    assert str(tmp_path) not in error
