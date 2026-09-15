import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.git_authority_snapshot import read_git_authority_snapshot
from tools.upgrade_authority import AuthorityError


class GitAuthoritySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"], check=True)
        (self.root / "state").write_text("clean\n")
        subprocess.run(["git", "-C", str(self.root), "add", "state"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "seed"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "tag", "release-1"], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_clean_reachable_snapshot(self) -> None:
        snapshot = read_git_authority_snapshot(self.root.resolve(), "refs/tags/release-1")
        self.assertEqual("main", snapshot.head_ref)
        self.assertEqual(snapshot.head_commit, snapshot.release_commit)

    def test_dirty_and_unreachable_are_rejected(self) -> None:
        (self.root / "dirty").write_text("x\n")
        with self.assertRaisesRegex(AuthorityError, "dirty"):
            read_git_authority_snapshot(self.root.resolve(), "refs/tags/release-1")
        (self.root / "dirty").unlink()
        subprocess.run(["git", "-C", str(self.root), "checkout", "-b", "later"], check=True, capture_output=True)
        (self.root / "later").write_text("x\n")
        subprocess.run(["git", "-C", str(self.root), "add", "later"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "later"], check=True, capture_output=True)
        with self.assertRaises(AuthorityError):
            read_git_authority_snapshot(self.root.resolve(), "refs/tags/missing")


if __name__ == "__main__":
    unittest.main()
