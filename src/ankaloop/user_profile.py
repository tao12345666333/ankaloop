"""User profile loading from you.md files.

A you.md profile (https://github.com/brainsparker/you.md) is a portable Markdown
description of the person the agent is working with: their background, how they
communicate, how they work, and their boundaries. It is the user-side complement
to the agent persona in ``SOUL.md`` / ``IDENTITY.md`` and the project rules in
``AGENTS.md``.

Loading is opt-in through ``[context] user_profile = true`` in the user-level
``config.toml``. When enabled, the first matching file in this order is used:

1. An explicit path in the ``YOU_MD_PATH`` environment variable
2. Project-local ``.you.md`` or ``you.md`` in the working directory, then in the
   repository root
3. User-level ``~/.you.md``
4. XDG paths ``~/.config/you.md`` and ``~/.config/you/you.md``

This mirrors the precedence documented by you.md itself: a project-local profile
replaces (rather than merges with) the user-level profile. Only the file is read;
the you-md CLI is not required.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from .project_rules import find_git_root

logger = logging.getLogger(__name__)

# Environment variable that points at an explicit profile path
USER_PROFILE_ENV_VAR = "YOU_MD_PATH"

# Supported project-local file names in priority order
USER_PROFILE_FILE_NAMES = [
    ".you.md",
    "you.md",
]

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def strip_frontmatter(content: str) -> str:
    """Remove a leading YAML frontmatter block from Markdown content.

    you.md files carry a small frontmatter block (``schema_version``,
    ``privacy_level`` and similar). That metadata is for tooling, not for the
    model, so it is dropped before injection.

    Args:
        content: Raw file content

    Returns:
        Content without the frontmatter block, otherwise unchanged
    """
    return _FRONTMATTER_RE.sub("", content, count=1)


def get_user_level_profile_paths(home: Path | None = None) -> list[Path]:
    """Return user-level candidate paths in precedence order.

    Args:
        home: Home directory override, mainly for tests

    Returns:
        ``~/.you.md`` followed by the XDG-style locations
    """
    home = (home or Path.home()).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return [
        home / ".you.md",
        config_home / "you.md",
        config_home / "you" / "you.md",
    ]


def find_project_profile(directory: Path) -> Path | None:
    """Find a project-local you.md file in the given directory.

    Args:
        directory: Directory to search in

    Returns:
        Path to the profile or None if not found
    """
    for filename in USER_PROFILE_FILE_NAMES:
        candidate = directory / filename
        try:
            if candidate.is_file():
                return candidate
        except OSError as e:
            logger.warning(f"Could not check user profile candidate {candidate}: {e}")
    return None


def discover_user_profile(work_dir: Path, home: Path | None = None) -> Path | None:
    """Locate the active you.md profile for a working directory.

    Args:
        work_dir: Working directory to start from
        home: Home directory override, mainly for tests

    Returns:
        Path to the first matching profile, or None
    """
    explicit = os.environ.get(USER_PROFILE_ENV_VAR, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        try:
            if path.is_file():
                return path
        except OSError as e:
            logger.warning(f"Could not read {USER_PROFILE_ENV_VAR}={explicit}: {e}")
        logger.warning(f"{USER_PROFILE_ENV_VAR} is set but {explicit} is not a readable file; falling back")

    work_dir = work_dir.resolve()
    directories = [work_dir]
    git_root = find_git_root(work_dir)
    if git_root and git_root != work_dir:
        directories.append(git_root)

    for directory in directories:
        found = find_project_profile(directory)
        if found:
            return found

    for candidate in get_user_level_profile_paths(home):
        try:
            if candidate.is_file():
                return candidate
        except OSError as e:
            logger.warning(f"Could not check user profile candidate {candidate}: {e}")

    return None


def load_profile_content(filepath: Path) -> str | None:
    """Read a profile file and strip its frontmatter.

    Args:
        filepath: Path to the profile

    Returns:
        Profile body, or None when the file is unreadable or has no body
    """
    try:
        raw = filepath.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to load user profile {filepath}: {e}")
        return None

    body = strip_frontmatter(raw).strip()
    if not body:
        logger.warning(f"User profile {filepath} has no content after frontmatter; skipping")
        return None
    return body


def format_user_profile_section(filepath: Path, content: str) -> str:
    """Wrap profile content for system prompt injection.

    Args:
        filepath: Source file, shown so the model and the user know where it came from
        content: Profile body with frontmatter already removed

    Returns:
        Formatted ``<user_profile>`` block
    """
    try:
        shown_path = "~/" + str(filepath.relative_to(Path.home()))
    except ValueError:
        shown_path = str(filepath)

    return f"""<user_profile>
## User Profile

The following profile was loaded from {shown_path} (a you.md file). It describes the person you are working with: their background, how they communicate, how they work, and their boundaries. Use it to tailor your responses. The user's explicit instructions in the conversation always take precedence over this profile.

{content.strip()}
</user_profile>"""


class UserProfileLoader:
    """Loader for the active you.md user profile."""

    def __init__(self, work_dir: Path | None = None, enabled: bool = False):
        """Initialize the loader.

        Args:
            work_dir: Working directory to search from. Defaults to cwd.
            enabled: Whether profile loading is turned on in ``[context]``
        """
        self.work_dir = (work_dir or Path.cwd()).resolve()
        self.enabled = enabled
        self._cached_profile: str | None = None
        self._loaded_file: Path | None = None

    def load_profile(self) -> str:
        """Load the profile section, or an empty string when disabled or absent."""
        if self._cached_profile is not None:
            return self._cached_profile

        if not self.enabled:
            self._cached_profile = ""
            return ""

        filepath = discover_user_profile(self.work_dir)
        if filepath is None:
            self._cached_profile = ""
            return ""

        content = load_profile_content(filepath)
        if content is None:
            self._cached_profile = ""
            return ""

        self._loaded_file = filepath
        self._cached_profile = format_user_profile_section(filepath, content)
        return self._cached_profile

    def get_loaded_file(self) -> Path | None:
        """Return the file the profile was loaded from, if any."""
        return self._loaded_file

    def get_profile_summary(self) -> dict[str, Any]:
        """Return summary information about the loaded profile."""
        return {
            "enabled": self.enabled,
            "work_dir": str(self.work_dir),
            "loaded_file": str(self._loaded_file) if self._loaded_file else None,
            "has_profile": bool(self._cached_profile),
        }


def load_user_profile(work_dir: Path | None = None, enabled: bool = False) -> str:
    """Convenience function to load the user profile section.

    Args:
        work_dir: Working directory to search from
        enabled: Whether profile loading is turned on

    Returns:
        Formatted profile section or empty string
    """
    return UserProfileLoader(work_dir, enabled=enabled).load_profile()
