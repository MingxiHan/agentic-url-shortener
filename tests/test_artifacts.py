"""Boundary tests retain temporary artifacts instead of deleting user files."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchestrator import artifacts
from orchestrator.artifacts import PolicyError, apply_changes, manifest, sha256_bytes, snapshot_files, verify_manifest


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="agentic-artifacts-test-")).resolve()
        self.base = self.workspace / "base"
        self.base.mkdir()
        self.destination = self.workspace / "candidate"
        self.source = "service/shortener/app.py"
        self.write(self.base, self.source, "value = 1\n")

    @staticmethod
    def write(root, relative, content):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def change(self, path=None, content="value = 2\n", previous_hash=None):
        path = path or self.source
        if previous_hash is None and path == self.source:
            previous_hash = sha256_bytes(b"value = 1\n")
        return {"path": path, "previous_hash": previous_hash, "content": content}

    def test_copy_is_additive_and_manifest_is_deterministic(self):
        self.write(self.base, "docs/README.md", "# Original\n")
        self.write(self.base, "generated_tests/test_base.py", "def test_base():\n    assert True\n")
        result = apply_changes(self.base, self.destination, [self.change(), self.change("service/shortener/routes/links.py", "value = 3\n")], "builder")
        self.assertEqual(result, manifest(self.destination))
        self.assertEqual(snapshot_files(self.base)[self.source], "value = 1\n")
        self.assertEqual(snapshot_files(self.destination)["docs/README.md"], "# Original\n")
        self.assertEqual(snapshot_files(self.destination)[self.source], "value = 2\n")
        verify_manifest(self.destination, result)
        other = self.workspace / "other"
        other.mkdir()
        for path, content in reversed(list(snapshot_files(self.destination).items())):
            self.write(other, path, content)
        self.assertEqual(manifest(other), result)

    def test_traversal_absolute_and_protected_paths_are_rejected(self):
        for path in ["../outside.py", "/service/shortener/app.py", "service/shortener/../app.py", "service\\shortener\\app.py", "service//shortener/app.py", "service/shortener/./app.py", "service/shortener/conftest.py", "service/shortener/nested/sitecustomize.py", "service/shortener/usercustomize.py", "service/shortener/.env", "orchestrator/engine.py", "acceptance/test_service.py", "service/shortener/app.py\x00"]:
            with self.subTest(path=path), self.assertRaises(PolicyError):
                apply_changes(self.base, self.destination, [self.change(path)], "builder")
        self.assertFalse(self.destination.exists())

    def test_role_boundaries_and_allowed_role_paths(self):
        for role, path in [("builder", "generated_tests/test_extra.py"), ("verifier", self.source), ("verifier", "generated_tests/conftest.py"), ("verifier", "generated_tests/nested/test_extra.py"), ("docs", self.source), ("docs", "docs/nested/README.md")]:
            with self.subTest(role=role, path=path), self.assertRaises(PolicyError):
                apply_changes(self.base, self.destination, [self.change(path)], role)
        for role, path in [("verifier", "generated_tests/test_extra.py"), ("docs", "docs/README.md")]:
            destination = self.workspace / role
            apply_changes(self.base, destination, [self.change(path, "text\n")], role)
            self.assertEqual(snapshot_files(destination)[path], "text\n")

    def test_preimages_creation_and_duplicates(self):
        invalid = [
            [self.change(previous_hash="0" * 64)],
            [{"path": self.source, "previous_hash": None, "content": "changed\n"}],
            [self.change("service/shortener/new.py", previous_hash="0" * 64)],
            [self.change(), self.change()],
            [self.change("service/shortener/App.py")],
            [self.change("service/shortener/app.py/nested.py")],
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(PolicyError):
                apply_changes(self.base, self.destination, changes, "builder")
        self.assertFalse(self.destination.exists())

    def test_deletion_binary_and_oversized_content_are_rejected(self):
        for content in [None, b"bytes", "value\x00", "value\x01", "\ud800", "x" * (artifacts.MAX_FILE_BYTES + 1)]:
            with self.subTest(content_type=type(content).__name__), self.assertRaises(PolicyError):
                apply_changes(self.base, self.destination, [self.change(content=content)], "builder")
        self.assertFalse(self.destination.exists())

    def test_total_size_limit(self):
        changes = [self.change(f"service/shortener/file_{index}.py", "x" * artifacts.MAX_FILE_BYTES) for index in range(8)]
        with self.assertRaises(PolicyError):
            apply_changes(self.base, self.destination, changes, "builder")
        self.assertFalse(self.destination.exists())

    def test_unknown_files_and_directories_in_base_are_rejected(self):
        for relative in [".env", "assignment.pdf", "service/secrets.txt", "generated_tests/pytest.ini"]:
            root = self.workspace / ("invalid_" + str(len(list(self.workspace.iterdir()))))
            root.mkdir()
            self.write(root, relative, "secret")
            with self.subTest(relative=relative), self.assertRaises(PolicyError):
                snapshot_files(root)
        root = self.workspace / "empty_unknown"
        root.mkdir()
        (root / "acceptance").mkdir()
        with self.assertRaises(PolicyError):
            snapshot_files(root)

    def test_binary_and_large_base_files_are_rejected(self):
        for index, content in enumerate([b"\xff", b"\x00", b"x" * (artifacts.MAX_FILE_BYTES + 1)]):
            root = self.workspace / f"binary_{index}"
            target = root / self.source
            target.parent.mkdir(parents=True)
            target.write_bytes(content)
            with self.subTest(index=index), self.assertRaises(PolicyError):
                snapshot_files(root)

    def test_symlink_file_and_ancestor_are_rejected(self):
        outside = self.workspace / "outside"
        outside.mkdir()
        secret = outside / "secret.py"
        secret.write_text("secret = True\n", encoding="utf-8")
        (self.base / "service/shortener/linked.py").symlink_to(secret)
        with self.assertRaises(PolicyError):
            snapshot_files(self.base)
        ancestor = self.workspace / "linked_parent"
        ancestor.symlink_to(outside, target_is_directory=True)
        child = outside / "child"
        child.mkdir()
        with self.assertRaises(PolicyError):
            snapshot_files(ancestor / "child")
        clean = self.workspace / "clean"
        clean.mkdir()
        with self.assertRaises(PolicyError):
            apply_changes(clean, ancestor / "candidate", [], "builder")
        self.assertFalse((outside / "candidate").exists())

    def test_existing_destination_and_nested_destination_are_rejected(self):
        self.destination.mkdir()
        self.write(self.destination, "docs/README.md", "keep me\n")
        with self.assertRaises(PolicyError):
            apply_changes(self.base, self.destination, [self.change()], "builder")
        self.assertEqual((self.destination / "docs/README.md").read_text(), "keep me\n")
        with self.assertRaises(PolicyError):
            apply_changes(self.base, self.base / "candidate", [], "builder")

    def test_root_symlink_destination_symlink_and_hardlink_are_rejected(self):
        base_link = self.workspace / "base_link"
        base_link.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(PolicyError):
            snapshot_files(base_link)
        outside = self.workspace / "outside"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(PolicyError):
            apply_changes(self.base, self.destination, [self.change()], "builder")
        self.assertEqual(list(outside.iterdir()), [])
        secret = outside / "secret.py"
        secret.write_text("secret = True\n", encoding="utf-8")
        (self.base / "service/shortener/hardlinked.py").hardlink_to(secret)
        with self.assertRaises(PolicyError):
            snapshot_files(self.base)

    def test_malformed_role_or_change_schema_is_rejected(self):
        for role in [None, [], "operator"]:
            with self.subTest(role=role), self.assertRaises(PolicyError):
                apply_changes(self.base, self.destination, [], role)
        for changes in [None, [None], [{"path": self.source, "content": "value = 2\n"}], [{**self.change(), "delete": True}]]:
            with self.subTest(changes=changes), self.assertRaises(PolicyError):
                apply_changes(self.base, self.destination, changes, "builder")

    def test_tampered_manifest_or_content_is_rejected(self):
        expected = manifest(self.base)
        for tampered in [{"hash": "0" * 64, "files": expected["files"]}, {"hash": expected["hash"], "files": {}}, {}]:
            with self.subTest(tampered=tampered), self.assertRaises(PolicyError):
                verify_manifest(self.base, tampered)
        self.write(self.base, self.source, "value = 999\n")
        with self.assertRaises(PolicyError):
            verify_manifest(self.base, expected)

    def test_base_mutation_during_assembly_is_rejected_and_output_retained(self):
        real_snapshot = snapshot_files
        calls = 0

        def changing_snapshot(root):
            nonlocal calls
            if root == self.base:
                calls += 1
                if calls == 3:
                    self.write(self.base, self.source, "value = 999\n")
            return real_snapshot(root)

        with mock.patch.object(artifacts, "snapshot_files", side_effect=changing_snapshot):
            with self.assertRaisesRegex(PolicyError, "changed during assembly"):
                apply_changes(self.base, self.destination, [self.change()], "builder")
        self.assertTrue(self.destination.is_dir())
        self.assertEqual((self.destination / self.source).read_text(), "value = 2\n")


if __name__ == "__main__":
    unittest.main()
