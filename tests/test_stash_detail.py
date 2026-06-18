import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sync_branches_ui", ROOT / "sync-branches-ui.py"
)
sync_branches_ui = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_branches_ui)


def run(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class StashDetailTest(unittest.TestCase):
    def make_stashed_repo(self):
        tmp = tempfile.TemporaryDirectory()
        base = Path(tmp.name) / "workspace"
        repo = base / "demo"
        repo.mkdir(parents=True)

        run(["git", "init", "-q"], repo)
        run(["git", "config", "user.email", "test@example.com"], repo)
        run(["git", "config", "user.name", "Test User"], repo)
        (repo / "tracked.txt").write_text("base\n")
        run(["git", "add", "tracked.txt"], repo)
        run(["git", "commit", "-qm", "init"], repo)

        (repo / "tracked.txt").write_text("changed\n")
        (repo / "new.txt").write_text("new\n")
        run(["git", "stash", "push", "-u", "-m", "sync-branches-create: master"], repo)
        return tmp, base

    def test_stash_detail_lists_tracked_and_untracked_files(self):
        tmp, base = self.make_stashed_repo()
        with tmp:
            detail = sync_branches_ui.stash_detail(str(base), "demo", "stash@{0}")

            self.assertTrue(detail["ok"])
            self.assertEqual(detail["proj"], "demo")
            self.assertEqual(detail["ref"], "stash@{0}")
            self.assertEqual(detail["files"], [
                {"status": "A", "path": "new.txt"},
                {"status": "M", "path": "tracked.txt"},
            ])

    def test_list_stashes_keeps_default_rows_lightweight(self):
        tmp, base = self.make_stashed_repo()
        with tmp:
            stashes = sync_branches_ui.list_stashes(str(base))

            self.assertEqual(len(stashes), 1)
            self.assertNotIn("files", stashes[0])


if __name__ == "__main__":
    unittest.main()
