"""Tests for the apply_patch module."""

import tempfile
from pathlib import Path

import pytest

from ankaloop.apply_patch import (
    FileOperation,
    Hunk,
    HunkLine,
    Patch,
    PatchApplier,
    PatchApplyError,
    PatchOperationType,
    PatchParseError,
    PatchParser,
    apply_patch_text,
)


class TestPatchParser:
    """Tests for the PatchParser class."""

    def test_parse_empty_patch(self):
        """Test parsing an empty patch."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** End Patch"""
        patch = parser.parse(patch_text)
        assert len(patch.operations) == 0

    def test_parse_no_begin_patch(self):
        """Test that missing Begin Patch raises error."""
        parser = PatchParser()
        patch_text = """*** End Patch"""
        with pytest.raises(PatchParseError, match="No '\\*\\*\\* Begin Patch' found"):
            parser.parse(patch_text)

    def test_parse_add_file(self):
        """Test parsing an Add File operation."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** Add File: test/new_file.py
+#!/usr/bin/env python3
+
+def hello():
+    return "Hello"
*** End Patch"""
        patch = parser.parse(patch_text)
        assert len(patch.operations) == 1
        op = patch.operations[0]
        assert op.op_type == PatchOperationType.ADD_FILE
        assert op.path == "test/new_file.py"
        assert len(op.content_lines) == 4
        assert op.content_lines[0] == "#!/usr/bin/env python3"
        assert op.content_lines[3] == '    return "Hello"'

    def test_parse_delete_file(self):
        """Test parsing a Delete File operation."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** Delete File: old/obsolete.py
*** End Patch"""
        patch = parser.parse(patch_text)
        assert len(patch.operations) == 1
        op = patch.operations[0]
        assert op.op_type == PatchOperationType.DELETE_FILE
        assert op.path == "old/obsolete.py"

    def test_parse_update_file_simple(self):
        """Test parsing a simple Update File operation."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** Update File: src/app.py
@@ def hello():
-    return "old"
+    return "new"
*** End Patch"""
        patch = parser.parse(patch_text)
        assert len(patch.operations) == 1
        op = patch.operations[0]
        assert op.op_type == PatchOperationType.UPDATE_FILE
        assert op.path == "src/app.py"
        assert len(op.hunks) == 1
        hunk = op.hunks[0]
        assert "def hello():" in hunk.anchors
        assert len(hunk.lines) == 2
        assert hunk.lines[0].is_deletion
        assert hunk.lines[1].is_addition

    def test_parse_update_file_with_context(self):
        """Test parsing Update File with context lines."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** Update File: src/app.py
@@ class MyClass
@@ def method():
     # context before
     x = 1
-    return x + 1
+    return x + 2
     # context after
*** End Patch"""
        patch = parser.parse(patch_text)
        op = patch.operations[0]
        hunk = op.hunks[0]
        assert len(hunk.anchors) == 2
        assert "class MyClass" in hunk.anchors
        assert "def method():" in hunk.anchors
        # 2 context + 1 deletion + 1 addition + 1 context = 5
        assert len(hunk.lines) == 5
        assert hunk.lines[0].is_context
        assert hunk.lines[2].is_deletion
        assert hunk.lines[3].is_addition

    def test_parse_update_file_with_move(self):
        """Test parsing Update File with Move to."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** Update File: old/path.py
*** Move to: new/path.py
@@
-old
+new
*** End Patch"""
        patch = parser.parse(patch_text)
        op = patch.operations[0]
        assert op.move_to == "new/path.py"

    def test_parse_multiple_operations(self):
        """Test parsing patch with multiple operations."""
        parser = PatchParser()
        patch_text = """*** Begin Patch
*** Add File: new.py
+content

*** Update File: existing.py
@@
-old
+new

*** Delete File: obsolete.py
*** End Patch"""
        patch = parser.parse(patch_text)
        assert len(patch.operations) == 3
        assert patch.operations[0].op_type == PatchOperationType.ADD_FILE
        assert patch.operations[1].op_type == PatchOperationType.UPDATE_FILE
        assert patch.operations[2].op_type == PatchOperationType.DELETE_FILE

    def test_parse_case_insensitive(self):
        """Test that operation keywords are case-insensitive."""
        parser = PatchParser()
        patch_text = """*** begin patch
*** ADD FILE: test.py
+content
*** END PATCH"""
        patch = parser.parse(patch_text)
        assert len(patch.operations) == 1


class TestPatchApplier:
    """Tests for the PatchApplier class."""

    def test_apply_add_file(self, tmp_path):
        """Test applying Add File operation."""
        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.ADD_FILE,
                    path="new_file.py",
                    content_lines=["# New file", "def hello():", "    pass"],
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        changes = applier.apply(patch)

        assert len(changes) == 1
        assert changes[0]["type"] == "add"

        new_file = tmp_path / "new_file.py"
        assert new_file.exists()
        content = new_file.read_text()
        assert "# New file" in content
        assert "def hello():" in content

    def test_apply_add_file_creates_dirs(self, tmp_path):
        """Test that Add File creates parent directories."""
        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.ADD_FILE,
                    path="deep/nested/dir/file.py",
                    content_lines=["content"],
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        applier.apply(patch)

        assert (tmp_path / "deep/nested/dir/file.py").exists()

    def test_apply_delete_file(self, tmp_path):
        """Test applying Delete File operation."""
        # Create file to delete
        file_to_delete = tmp_path / "to_delete.py"
        file_to_delete.write_text("content")

        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.DELETE_FILE,
                    path="to_delete.py",
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        changes = applier.apply(patch)

        assert len(changes) == 1
        assert changes[0]["type"] == "delete"
        assert not file_to_delete.exists()

    def test_apply_delete_nonexistent_file(self, tmp_path):
        """Test that deleting nonexistent file raises error."""
        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.DELETE_FILE,
                    path="nonexistent.py",
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        with pytest.raises(PatchApplyError, match="File not found for deletion"):
            applier.apply(patch)

    def test_apply_update_file_simple(self, tmp_path):
        """Test applying simple Update File operation."""
        # Create file to update
        file_path = tmp_path / "app.py"
        file_path.write_text(
            """def hello():
    return "old"
"""
        )

        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.UPDATE_FILE,
                    path="app.py",
                    hunks=[
                        Hunk(
                            anchors=[],
                            lines=[
                                HunkLine(prefix=" ", text="def hello():"),
                                HunkLine(prefix="-", text='    return "old"'),
                                HunkLine(prefix="+", text='    return "new"'),
                            ],
                        )
                    ],
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        changes = applier.apply(patch)

        assert len(changes) == 1
        assert changes[0]["type"] == "update"
        assert changes[0]["additions"] == 1
        assert changes[0]["deletions"] == 1

        content = file_path.read_text()
        assert 'return "new"' in content
        assert 'return "old"' not in content

    def test_apply_update_file_with_anchor(self, tmp_path):
        """Test that anchors help locate the correct position."""
        file_path = tmp_path / "app.py"
        file_path.write_text(
            """class First:
    def method(self):
        return 1

class Second:
    def method(self):
        return 2
"""
        )

        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.UPDATE_FILE,
                    path="app.py",
                    hunks=[
                        Hunk(
                            anchors=["class Second"],
                            lines=[
                                HunkLine(prefix=" ", text="    def method(self):"),
                                HunkLine(prefix="-", text="        return 2"),
                                HunkLine(prefix="+", text="        return 42"),
                            ],
                        )
                    ],
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        applier.apply(patch)

        content = file_path.read_text()
        assert "return 42" in content
        assert "return 1" in content  # First class unchanged

    def test_apply_update_file_with_move(self, tmp_path):
        """Test Update File with rename."""
        old_path = tmp_path / "old.py"
        old_path.write_text("content\n")

        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.UPDATE_FILE,
                    path="old.py",
                    move_to="new.py",
                    hunks=[
                        Hunk(
                            anchors=[],
                            lines=[
                                HunkLine(prefix="-", text="content"),
                                HunkLine(prefix="+", text="new content"),
                            ],
                        )
                    ],
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        applier.apply(patch)

        assert not old_path.exists()
        new_path = tmp_path / "new.py"
        assert new_path.exists()
        assert "new content" in new_path.read_text()

    def test_apply_rejects_absolute_paths(self, tmp_path):
        """Test that absolute paths are rejected."""
        patch = Patch(
            operations=[
                FileOperation(
                    op_type=PatchOperationType.ADD_FILE,
                    path="/absolute/path.py",
                    content_lines=["content"],
                )
            ]
        )
        applier = PatchApplier(tmp_path)
        with pytest.raises(PatchApplyError, match="Absolute paths not allowed"):
            applier.apply(patch)


class TestApplyPatchText:
    """Integration tests for apply_patch_text function."""

    def test_full_workflow(self, tmp_path):
        """Test a complete patch workflow."""
        # Create initial files
        (tmp_path / "existing.py").write_text(
            """def old():
    pass
"""
        )
        (tmp_path / "obsolete.py").write_text("old content\n")

        patch_text = """*** Begin Patch
*** Add File: new_file.py
+# New file
+def new():
+    return True

*** Update File: existing.py
@@
-def old():
+def updated():
     pass

*** Delete File: obsolete.py
*** End Patch"""

        changes = apply_patch_text(patch_text, tmp_path)

        assert len(changes) == 3

        # Verify new file
        new_file = tmp_path / "new_file.py"
        assert new_file.exists()
        assert "def new():" in new_file.read_text()

        # Verify updated file
        existing = tmp_path / "existing.py"
        content = existing.read_text()
        assert "def updated():" in content

        # Verify deleted file
        assert not (tmp_path / "obsolete.py").exists()

    def test_complex_update_with_context(self, tmp_path):
        """Test update with multiple context lines."""
        file_path = tmp_path / "calculator.py"
        file_path.write_text(
            """class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        \"\"\"Subtract b from a.\"\"\"
        return a + b  # Bug!

    def multiply(self, a, b):
        return a * b
"""
        )

        patch_text = """*** Begin Patch
*** Update File: calculator.py
@@ class Calculator
@@ def subtract(self, a, b):
        \"\"\"Subtract b from a.\"\"\"
-        return a + b  # Bug!
+        return a - b
*** End Patch"""

        changes = apply_patch_text(patch_text, tmp_path)
        assert len(changes) == 1  # One update operation

        content = file_path.read_text()
        assert "return a - b" in content
        assert "return a + b  # Bug!" not in content
        # Other methods unchanged
        assert "return a + b" in content  # add method
        assert "return a * b" in content


class TestStrictHunkVerification:
    """Regression tests for the silent-corruption incident.

    A patch whose context/deletion lines do not match the actual file
    content (e.g. a typo) must be rejected, not silently applied into a
    corrupted file. File content is left untouched on failure.
    """

    def test_typo_in_deletion_line_rejected(self, tmp_path):
        """Case #1 from INCIDENT-REPORT: typo patch rejected, file intact."""
        file_path = tmp_path / "sample.py"
        original = 'def hello():\n    print("hello world")\n    return 0\n'
        file_path.write_text(original, encoding="utf-8")

        typo_patch = (
            "*** Begin Patch\n"
            "*** Update File: sample.py\n"
            "@@ def hello():\n"
            '    print("hello world")\n'
            "-    retunr 0\n"
            "+    return 42\n"
            "*** End Patch"
        )

        with pytest.raises(PatchApplyError):
            apply_patch_text(typo_patch, tmp_path)

        assert file_path.read_text(encoding="utf-8") == original

    def test_context_mismatch_rejected(self, tmp_path):
        """A context line whose content does not exist in the file is rejected.

        Mirrors Codex's old_lines block match: context participates in
        verification (whitespace-only drift is tolerated, but a wrong token is
        not).
        """
        file_path = tmp_path / "m.py"
        original = "class A:\n    x = 1\n    y = 2\n"
        file_path.write_text(original, encoding="utf-8")

        # Context says "    z = 3" which does not exist in the file.
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: m.py\n"
            "@@ class A:\n"
            "    x = 1\n"
            "    z = 3\n"
            "-    y = 2\n"
            "+    y = 20\n"
            "*** End Patch"
        )

        with pytest.raises(PatchApplyError):
            apply_patch_text(patch_text, tmp_path)

        assert file_path.read_text(encoding="utf-8") == original

    def test_additions_only_anchor_not_found_rejected(self, tmp_path):
        """Additions-only hunk with a missing anchor must raise, not append."""
        file_path = tmp_path / "m.py"
        original = "class A:\n    pass\n"
        file_path.write_text(original, encoding="utf-8")

        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: m.py\n"
            "@@ class A:\n"
            "    anchors: [NonExistentMarker]\n"
            "+    new_field = 1\n"
            "*** End Patch"
        )

        with pytest.raises(PatchApplyError):
            apply_patch_text(patch_text, tmp_path)

        assert file_path.read_text(encoding="utf-8") == original

    def test_fuzzy_match_stays_within_anchor_scope(self, tmp_path):
        """A fuzzy match must not modify an earlier lookalike block."""
        file_path = tmp_path / "m.py"
        file_path.write_text(
            "class First:\n    marker\n    target\n\nclass Second:\n  marker\n    target\n",
            encoding="utf-8",
        )

        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: m.py\n"
            "@@ class Second\n"
            "     marker\n"
            "-    target\n"
            "+    changed\n"
            "*** End Patch"
        )

        apply_patch_text(patch_text, tmp_path)

        assert file_path.read_text(encoding="utf-8") == (
            "class First:\n    marker\n    target\n\nclass Second:\n  marker\n    changed\n"
        )

    def test_fuzzy_match_accounts_for_context_between_deletions(self, tmp_path):
        """Deletion offsets include intervening context lines."""
        file_path = tmp_path / "m.py"
        file_path.write_text(
            "  before\nold1\n  middle\nold2\nafter\n",
            encoding="utf-8",
        )

        patch_text = (
            "*** Begin Patch\n*** Update File: m.py\n@@\n before\n-old1\n middle\n-old2\n+new\n after\n*** End Patch"
        )

        apply_patch_text(patch_text, tmp_path)

        assert file_path.read_text(encoding="utf-8") == "  before\n  middle\nnew\nafter\n"

    def test_repeated_path_is_rejected_before_any_changes(self, tmp_path):
        """Separate operations may not compute from stale content for one path."""
        file_path = tmp_path / "m.py"
        original = "a\nb\n"
        file_path.write_text(original, encoding="utf-8")

        patch_text = (
            "*** Begin Patch\n*** Update File: m.py\n@@\n-a\n+A\n*** Update File: m.py\n@@\n-b\n+B\n*** End Patch"
        )

        with pytest.raises(PatchApplyError, match="Multiple operations target the same path"):
            apply_patch_text(patch_text, tmp_path)

        assert file_path.read_text(encoding="utf-8") == original

    def test_commit_failure_rolls_back_prior_writes(self, tmp_path, monkeypatch):
        """A later filesystem failure restores files written earlier."""
        first = tmp_path / "first.py"
        second = tmp_path / "second.py"
        first.write_text("old first\n", encoding="utf-8")
        second.write_text("old second\n", encoding="utf-8")
        original_write_text = Path.write_text

        def fail_second_write(path, content, *args, **kwargs):
            if path == second:
                raise OSError("simulated write failure")
            return original_write_text(path, content, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_second_write)
        patch_text = (
            "*** Begin Patch\n"
            "*** Update File: first.py\n"
            "@@\n"
            "-old first\n"
            "+new first\n"
            "*** Update File: second.py\n"
            "@@\n"
            "-old second\n"
            "+new second\n"
            "*** End Patch"
        )

        with pytest.raises(PatchApplyError, match="Failed to commit patch"):
            apply_patch_text(patch_text, tmp_path)

        assert first.read_text(encoding="utf-8") == "old first\n"
        assert second.read_text(encoding="utf-8") == "old second\n"
