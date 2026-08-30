from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[2]


def test_harness_skill_is_only_a_python_workflow_launcher() -> None:
    skill = (ROOT / "skills" / "autobuild" / "SKILL.md").read_text(encoding="utf-8")

    assert "autobuild run" in skill
    assert "reviewedCampaign" not in skill
    assert "codexJudge" not in skill
    assert "select →" not in skill
    assert "blind dual audit" not in skill


def test_package_entry_points_delegate_to_the_application_sequence() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    cli = (ROOT / "src" / "autobuild" / "cli.py").read_text(encoding="utf-8")
    composition = (
        ROOT / "src" / "autobuild" / "bootstrap" / "composition.py"
    ).read_text(encoding="utf-8")
    application = (
        ROOT / "src" / "autobuild" / "application" / "campaign.py"
    ).read_text(encoding="utf-8")

    assert 'autobuild = "autobuild.cli:main"' in project
    assert "run_campaign(" in cli
    assert "CampaignRunner(ports).run" in composition
    assert "while len(outcomes) < campaign.max_items" in application
