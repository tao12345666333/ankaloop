"""Tests for the optional you.md user profile loading."""

from pathlib import Path
from unittest.mock import patch

from ankaloop import config as config_module
from ankaloop.agent import Agent
from ankaloop.config import AnkaloopConfig, ContextConfig
from ankaloop.user_profile import (
    USER_PROFILE_ENV_VAR,
    USER_PROFILE_FILE_NAMES,
    UserProfileLoader,
    discover_user_profile,
    format_user_profile_section,
    get_user_level_profile_paths,
    load_profile_content,
    load_user_profile,
    strip_frontmatter,
)

PROFILE = """---
schema_version: "1.1"
privacy_level: "private"
---
# Me

## How I Communicate
Verbosity: concise
"""


def _write(path: Path, content: str = PROFILE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_supported_file_names():
    assert USER_PROFILE_FILE_NAMES == [".you.md", "you.md"]


class TestStripFrontmatter:
    def test_removes_leading_block(self):
        assert strip_frontmatter(PROFILE).lstrip().startswith("# Me")

    def test_leaves_content_without_frontmatter(self):
        assert strip_frontmatter("# Me\n") == "# Me\n"

    def test_ignores_horizontal_rule_later_in_body(self):
        body = "# Me\n\n---\n\nmore\n"
        assert strip_frontmatter(body) == body

    def test_handles_crlf(self):
        content = "---\r\nschema_version: '1.1'\r\n---\r\n# Me\r\n"
        assert strip_frontmatter(content) == "# Me\r\n"


class TestDiscoverUserProfile:
    def test_returns_none_when_nothing_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        work = tmp_path / "work"
        work.mkdir()
        assert discover_user_profile(work, home=tmp_path / "home") is None

    def test_env_var_wins(self, tmp_path, monkeypatch):
        explicit = _write(tmp_path / "elsewhere" / "profile.md")
        _write(tmp_path / "work" / ".you.md")
        monkeypatch.setenv(USER_PROFILE_ENV_VAR, str(explicit))
        assert discover_user_profile(tmp_path / "work", home=tmp_path / "home") == explicit

    def test_env_var_pointing_at_missing_file_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv(USER_PROFILE_ENV_VAR, str(tmp_path / "missing.md"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        home_profile = _write(tmp_path / "home" / ".you.md")
        work = tmp_path / "work"
        work.mkdir()
        assert discover_user_profile(work, home=tmp_path / "home") == home_profile

    def test_project_dot_file_beats_home(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        _write(tmp_path / "home" / ".you.md")
        project = _write(tmp_path / "work" / ".you.md")
        assert discover_user_profile(tmp_path / "work", home=tmp_path / "home") == project

    def test_project_plain_name_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        project = _write(tmp_path / "work" / "you.md")
        assert discover_user_profile(tmp_path / "work", home=tmp_path / "home") == project

    def test_git_root_is_searched_after_work_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        nested = repo / "src" / "pkg"
        nested.mkdir(parents=True)
        root_profile = _write(repo / ".you.md")
        assert discover_user_profile(nested, home=tmp_path / "home") == root_profile

    def test_home_then_xdg_order(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        home = tmp_path / "home"
        work = tmp_path / "work"
        work.mkdir()

        assert get_user_level_profile_paths(home) == [
            home / ".you.md",
            xdg / "you.md",
            xdg / "you" / "you.md",
        ]

        xdg_nested = _write(xdg / "you" / "you.md")
        assert discover_user_profile(work, home=home) == xdg_nested
        xdg_flat = _write(xdg / "you.md")
        assert discover_user_profile(work, home=home) == xdg_flat
        home_profile = _write(home / ".you.md")
        assert discover_user_profile(work, home=home) == home_profile


class TestLoadProfileContent:
    def test_strips_frontmatter(self, tmp_path):
        path = _write(tmp_path / ".you.md")
        content = load_profile_content(path)
        assert content is not None
        assert content.startswith("# Me")
        assert "schema_version" not in content

    def test_frontmatter_only_is_skipped(self, tmp_path):
        path = _write(tmp_path / ".you.md", '---\nschema_version: "1.1"\n---\n')
        assert load_profile_content(path) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert load_profile_content(tmp_path / "nope.md") is None


class TestFormatSection:
    def test_wraps_in_user_profile_block(self, tmp_path):
        section = format_user_profile_section(tmp_path / ".you.md", "# Me\nVerbosity: concise")
        assert section.startswith("<user_profile>")
        assert section.rstrip().endswith("</user_profile>")
        assert "## User Profile" in section
        assert "Verbosity: concise" in section
        assert str(tmp_path / ".you.md") in section

    def test_home_relative_path_is_abbreviated(self, tmp_path):
        with patch("ankaloop.user_profile.Path.home", return_value=tmp_path):
            section = format_user_profile_section(tmp_path / ".you.md", "body")
        assert "~/.you.md" in section


class TestUserProfileLoader:
    def test_disabled_returns_empty_even_when_file_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        _write(tmp_path / "work" / ".you.md")
        loader = UserProfileLoader(tmp_path / "work", enabled=False)
        assert loader.load_profile() == ""
        assert loader.get_loaded_file() is None
        assert loader.get_profile_summary()["has_profile"] is False

    def test_enabled_loads_and_caches(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        profile = _write(tmp_path / "work" / ".you.md")
        loader = UserProfileLoader(tmp_path / "work", enabled=True)
        first = loader.load_profile()
        assert "Verbosity: concise" in first
        assert loader.get_loaded_file() == profile

        profile.write_text("# Changed\n", encoding="utf-8")
        assert loader.load_profile() == first

        summary = loader.get_profile_summary()
        assert summary["enabled"] is True
        assert summary["loaded_file"] == str(profile)
        assert summary["has_profile"] is True

    def test_enabled_without_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        work = tmp_path / "work"
        work.mkdir()
        with patch("ankaloop.user_profile.Path.home", return_value=tmp_path / "home"):
            assert load_user_profile(work, enabled=True) == ""


class TestContextConfig:
    def test_default_is_off(self):
        assert ContextConfig().user_profile is False

    def test_decode_encode_roundtrip(self):
        cfg = config_module._decode_context({"user_profile": True})
        assert cfg is not None
        assert cfg.user_profile is True
        encoded = config_module._encode_context(cfg)
        assert encoded is not None
        assert encoded["user_profile"] is True

    def test_decode_without_key_stays_off(self):
        cfg = config_module._decode_context({"progressive_tools": True})
        assert cfg is not None
        assert cfg.user_profile is False


class TestAgentSystemPrompt:
    def _prompt(self, tmp_path, enabled: bool) -> str:
        cfg = AnkaloopConfig(servers={}, chat=None, context=ContextConfig(user_profile=enabled))
        with (
            patch("ankaloop.agent.CONFIG_DIR", tmp_path / ".config" / "ankaloop"),
            patch("ankaloop.agent.load_config", return_value=cfg),
            patch.object(Agent, "_load_project_rules", return_value=""),
            patch.object(Agent, "_get_memory_prompt_context", return_value=("", "")),
        ):
            agent = Agent()
            return agent._get_system_prompt(tmp_path / "project", conversation_tokens=0)

    def test_profile_injected_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        _write(tmp_path / "project" / ".you.md", PROFILE.replace("concise", "PROFILE_MARKER"))
        prompt = self._prompt(tmp_path, enabled=True)
        assert "<user_profile>" in prompt
        assert "PROFILE_MARKER" in prompt
        assert "schema_version" not in prompt

    def test_profile_omitted_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv(USER_PROFILE_ENV_VAR, raising=False)
        _write(tmp_path / "project" / ".you.md", PROFILE.replace("concise", "PROFILE_MARKER"))
        prompt = self._prompt(tmp_path, enabled=False)
        assert "<user_profile>" not in prompt
        assert "PROFILE_MARKER" not in prompt
