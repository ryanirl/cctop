"""Account provisioning must stay strictly additive and allowlisted.

These lock in the safety guarantees: cloning copies only user config (never
identity, credentials, or session state), never overwrites, never touches the
source; aliasing is idempotent and backs up first; dir creation leaves an
existing dir alone.
"""

from __future__ import annotations

from pathlib import Path

from cctop import manage
from cctop.manage import AddPlan


def _plan(tmp: Path, rc_name: str = ".zshrc", alias: str = "cc-9") -> AddPlan:
    return AddPlan(
        index=9,
        alias_name=alias,
        config_dir=tmp / ".claude-9",
        rc_file=tmp / rc_name,
        alias_line=f"alias {alias}='CLAUDE_CONFIG_DIR=$HOME/.claude-9 command claude'",
        dir_exists=False,
        alias_exists=False,
    )


# -- copy_config: allowlist, additive, source-preserving -----------------------


def test_copy_config_copies_only_allowlist(tmp_path: Path) -> None:
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    dest.mkdir()
    (source / "CLAUDE.md").write_text("instructions")
    (source / "settings.json").write_text("{}")
    (source / "commands").mkdir()
    (source / "commands" / "x.md").write_text("cmd")

    manage.copy_config(source, dest)

    assert (dest / "CLAUDE.md").read_text() == "instructions"
    assert (dest / "settings.json").exists()
    assert (dest / "commands" / "x.md").exists()


def test_copy_config_never_copies_identity_creds_or_state(tmp_path: Path) -> None:
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    dest.mkdir()
    (source / ".claude.json").write_text('{"oauthAccount": "IDENTITY"}')
    (source / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "SECRET"}}')
    (source / "projects").mkdir()
    (source / "projects" / "s.jsonl").write_text("session")
    (source / "history.jsonl").write_text("hist")
    (source / "stats-cache.json").write_text("{}")

    manage.copy_config(source, dest)

    assert not (dest / ".claude.json").exists()
    assert not (dest / ".credentials.json").exists()
    assert not (dest / "projects").exists()
    assert not (dest / "history.jsonl").exists()
    assert not (dest / "stats-cache.json").exists()


def test_copy_config_never_overwrites_or_touches_source(tmp_path: Path) -> None:
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    dest.mkdir()
    (source / "CLAUDE.md").write_text("source version")
    (dest / "CLAUDE.md").write_text("dest own version")

    actions = manage.copy_config(source, dest)

    assert (dest / "CLAUDE.md").read_text() == "dest own version"
    assert (source / "CLAUDE.md").read_text() == "source version"
    assert any("already present" in a for a in actions)


def test_copy_config_empty_source_reports_nothing(tmp_path: Path) -> None:
    source, dest = tmp_path / "src", tmp_path / "dst"
    source.mkdir()
    dest.mkdir()

    actions = manage.copy_config(source, dest)

    assert actions == ["no allowlisted config found to copy"]
    assert list(dest.iterdir()) == []


# -- ensure_config_dir ---------------------------------------------------------


def test_ensure_config_dir_creates_then_leaves_alone(tmp_path: Path) -> None:
    target = tmp_path / ".claude-9"

    created = manage.ensure_config_dir(target)
    assert target.is_dir()
    assert any("created" in a for a in created)

    (target / "sentinel").write_text("keep me")
    again = manage.ensure_config_dir(target)
    assert (target / "sentinel").read_text() == "keep me"
    assert any("already exists" in a for a in again)


# -- set_alias: opt-in, idempotent, backed up ----------------------------------


def test_set_alias_appends_when_absent(tmp_path: Path) -> None:
    plan = _plan(tmp_path)

    actions = manage.set_alias(plan)

    assert plan.rc_file.exists()
    assert "alias cc-9=" in plan.rc_file.read_text()
    assert any("appended alias cc-9" in a for a in actions)


def test_set_alias_is_idempotent(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    manage.set_alias(plan)
    before = plan.rc_file.read_text()

    already = AddPlan(**{**plan.__dict__, "alias_exists": True})
    actions = manage.set_alias(already)

    assert plan.rc_file.read_text() == before  # no duplicate line
    assert any("already in" in a for a in actions)


def test_set_alias_backs_up_existing_rc(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan.rc_file.write_text("# my existing rc\nexport FOO=1\n")

    manage.set_alias(plan)

    backup = plan.rc_file.with_name(plan.rc_file.name + ".cctop.bak")
    assert backup.exists()
    assert backup.read_text() == "# my existing rc\nexport FOO=1\n"
    assert "export FOO=1" in plan.rc_file.read_text()  # original preserved


# -- plan_add: index selection and reuse ---------------------------------------


def test_plan_add_picks_next_free_index(tmp_path: Path) -> None:
    (tmp_path / ".claude-1").mkdir()

    plan = manage.plan_add(tmp_path)

    assert plan.index == 2
    assert plan.config_dir == tmp_path / ".claude-2"
    assert plan.alias_name == "cc-2"
    assert ".claude-2" in plan.alias_line


def test_plan_add_reuse_dir_targets_existing(tmp_path: Path) -> None:
    reuse = tmp_path / ".claude-5"
    reuse.mkdir()

    plan = manage.plan_add(tmp_path, reuse_dir=reuse)

    assert plan.config_dir == reuse
    assert plan.index == 5
    assert plan.alias_name == "cc-5"


def test_plan_add_custom_alias_name(tmp_path: Path) -> None:
    plan = manage.plan_add(tmp_path, name="work")
    assert plan.alias_name == "work"


def test_detect_rc_file_prefers_file_with_marker(tmp_path: Path) -> None:
    (tmp_path / ".bashrc").write_text("export CLAUDE_CONFIG_DIR=$HOME/.claude-1\n")

    assert manage.detect_rc_file(tmp_path) == tmp_path / ".bashrc"
