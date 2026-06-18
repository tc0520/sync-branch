import importlib.util
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "sync-branches-ui.py"


def load_module():
    spec = importlib.util.spec_from_file_location("sync_branches_ui", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(args, cwd, check=True):
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            "git %s failed in %s:\n%s" % (" ".join(args), cwd, result.stdout)
        )
    return result.stdout.strip()


def make_project(root, name, main_branch="main"):
    remotes = root / "remotes"
    work = root / "work"
    remotes.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    remote = remotes / ("%s.git" % name)
    seed = root / ("seed-%s" % name)

    git(["init", "--bare", str(remote)], root)
    git(["clone", str(remote), str(seed)], root)
    git(["checkout", "-b", main_branch], seed)
    git(["config", "user.email", "test@example.com"], seed)
    git(["config", "user.name", "Test User"], seed)
    (seed / "README.md").write_text("%s\n" % name, encoding="utf-8")
    git(["add", "README.md"], seed)
    git(["commit", "-m", "initial"], seed)
    git(["push", "-u", "origin", main_branch], seed)
    git(["symbolic-ref", "HEAD", "refs/heads/%s" % main_branch], remote)

    clone = work / name
    git(["clone", str(remote), str(clone)], root)
    git(["config", "user.email", "test@example.com"], clone)
    git(["config", "user.name", "Test User"], clone)
    return clone, remote, work


def make_peer_clone(root, remote, name):
    peer = root / ("peer-%s" % name)
    git(["clone", str(remote), str(peer)], root)
    git(["config", "user.email", "test@example.com"], peer)
    git(["config", "user.name", "Test User"], peer)
    return peer


class CreateBranchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.mod = load_module()

    def tearDown(self):
        self.tmpdir.cleanup()

    def collect_events(self):
        events = []

        def emit(kind, data):
            events.append((kind, data))

        return events, emit

    def latest_result(self, events):
        results = [data for kind, data in events if kind == "result"]
        self.assertTrue(results, "expected at least one result event")
        return results[-1]

    def test_parse_project_list_ignores_blanks_and_comments(self):
        projects, errors = self.mod.parse_project_list("alpha\n# skip\n\n beta \n")

        self.assertEqual(["alpha", "beta"], projects)
        self.assertEqual([], errors)

    def test_list_projects_returns_git_repositories_only(self):
        (self.root / "work").mkdir()
        (self.root / "work" / "zeta").mkdir()
        (self.root / "work" / "zeta" / ".git").mkdir()
        (self.root / "work" / "alpha").mkdir()
        (self.root / "work" / "alpha" / ".git").write_text(
            "gitdir: /tmp/alpha.git\n", encoding="utf-8")
        (self.root / "work" / "not_git").mkdir()
        (self.root / "work" / ".hidden").mkdir()
        (self.root / "work" / ".hidden" / ".git").mkdir()

        projects = self.mod.list_projects(str(self.root / "work"))

        self.assertEqual(["alpha", "zeta"], projects)

    def test_list_projects_supports_multiple_roots_and_deduplicates(self):
        first = self.root / "first"
        second = self.root / "second"
        for base, names in ((first, ("shared", "alpha")),
                            (second, ("shared", "beta"))):
            for name in names:
                (base / name / ".git").mkdir(parents=True)

        projects = self.mod.list_projects("%s\n%s" % (first, second))

        self.assertEqual(["alpha", "beta", "shared"], projects)
        self.assertEqual(1, projects.count("shared"))

    def test_text_inputs_disable_system_autocorrection(self):
        for element_id in ("list", "createProjects", "createBranch", "switchProjects", "switchBranch"):
            match = re.search(r'<(?:input|textarea)\b[^>]*\bid="%s"[^>]*>' % element_id,
                              self.mod.PAGE)
            self.assertIsNotNone(match, "missing input %s" % element_id)
            tag = match.group(0)
            self.assertIn('spellcheck="false"', tag)
            self.assertIn('autocorrect="off"', tag)
            self.assertIn('autocapitalize="off"', tag)
            self.assertIn('autocomplete="off"', tag)

    def test_create_and_switch_pages_have_project_picker_controls(self):
        for element_id in ("btnPickCreate", "btnPickSwitch", "projectPickerModal", "projectPickerList"):
            self.assertIn('id="%s"' % element_id, self.mod.PAGE)

    def test_create_local_branch_from_origin_head_without_pushing(self):
        clone, remote, work = make_project(self.root, "repo")
        events, emit = self.collect_events()

        self.mod.create_branch_one("repo", "dev_feature", str(work), False, emit)

        self.assertEqual("dev_feature", git(["branch", "--show-current"], clone))
        self.assertEqual(
            0,
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", "origin/main", "dev_feature"],
                cwd=clone,
            ).returncode,
        )
        self.assertNotIn(
            "refs/heads/dev_feature",
            git(["for-each-ref", "--format=%(refname)", "refs/heads"], remote),
        )
        self.assertNotEqual(
            0,
            subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{u}"],
                cwd=clone,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ).returncode,
        )
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_create_branch_uses_first_root_when_project_names_collide(self):
        first_root = self.root / "first-root"
        second_root = self.root / "second-root"
        first_root.mkdir()
        second_root.mkdir()
        first_clone, _first_remote, first_work = make_project(first_root, "repo")
        second_clone, _second_remote, second_work = make_project(second_root, "repo")
        events, emit = self.collect_events()

        self.mod.create_branch_one(
            "repo", "dev_first", "%s\n%s" % (first_work, second_work), False, emit)

        self.assertEqual("dev_first", git(["branch", "--show-current"], first_clone))
        self.assertEqual("main", git(["branch", "--show-current"], second_clone))
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_dirty_worktree_is_stashed_and_not_restored_to_new_branch(self):
        clone, _remote, work = make_project(self.root, "repo")
        (clone / "scratch.txt").write_text("old branch work\n", encoding="utf-8")
        events, emit = self.collect_events()

        self.mod.create_branch_one("repo", "dev_clean", str(work), False, emit)

        self.assertEqual("dev_clean", git(["branch", "--show-current"], clone))
        self.assertFalse((clone / "scratch.txt").exists())
        stash_list = git(["stash", "list"], clone)
        self.assertIn("sync-branches-create: main", stash_list)
        result = self.latest_result(events)
        self.assertEqual("ok", result["state"])
        self.assertTrue(result["stashed"])

    def test_create_branch_stash_is_visible_and_restorable(self):
        clone, _remote, work = make_project(self.root, "repo")
        (clone / "scratch.txt").write_text("old branch work\n", encoding="utf-8")
        _events, emit = self.collect_events()
        self.mod.create_branch_one("repo", "dev_clean", str(work), False, emit)

        stashes = self.mod.list_stashes(str(work))

        self.assertEqual(1, len(stashes))
        self.assertEqual("repo", stashes[0]["proj"])
        self.assertEqual("main", stashes[0]["branch"])

        ok, msg = self.mod.pop_stash(str(work), "repo", stashes[0]["ref"])

        self.assertTrue(ok, msg)
        self.assertEqual("main", git(["branch", "--show-current"], clone))
        self.assertEqual("old branch work\n", (clone / "scratch.txt").read_text(encoding="utf-8"))

    def test_existing_local_branch_is_checked_out(self):
        clone, _remote, work = make_project(self.root, "repo")
        git(["checkout", "-b", "dev_exists"], clone)
        git(["checkout", "main"], clone)
        events, emit = self.collect_events()

        self.mod.create_branch_one("repo", "dev_exists", str(work), True, emit)

        self.assertEqual("dev_exists", git(["branch", "--show-current"], clone))
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_existing_remote_branch_is_fetched_and_checked_out(self):
        clone, _remote, work = make_project(self.root, "repo")
        git(["checkout", "-b", "dev_remote"], clone)
        git(["push", "-u", "origin", "dev_remote"], clone)
        git(["checkout", "main"], clone)
        git(["branch", "-D", "dev_remote"], clone)
        events, emit = self.collect_events()

        self.mod.create_branch_one("repo", "dev_remote", str(work), True, emit)

        self.assertEqual("dev_remote", git(["branch", "--show-current"], clone))
        self.assertEqual("origin/dev_remote", git(["rev-parse", "--abbrev-ref", "@{u}"], clone))
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_push_remote_sets_upstream(self):
        clone, remote, work = make_project(self.root, "repo")
        events, emit = self.collect_events()

        self.mod.create_branch_one("repo", "dev_push", str(work), True, emit)

        self.assertEqual("dev_push", git(["branch", "--show-current"], clone))
        self.assertIn(
            "refs/heads/dev_push",
            git(["for-each-ref", "--format=%(refname)", "refs/heads"], remote),
        )
        self.assertEqual("origin/dev_push", git(["rev-parse", "--abbrev-ref", "@{u}"], clone))
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_switch_branch_stashes_dirty_work_and_merges_main(self):
        clone, remote, work = make_project(self.root, "repo")
        peer = make_peer_clone(self.root, remote, "repo")
        git(["checkout", "-b", "dev_target"], peer)
        git(["push", "-u", "origin", "dev_target"], peer)
        git(["checkout", "main"], peer)
        (peer / "main.txt").write_text("main advance\n", encoding="utf-8")
        git(["add", "main.txt"], peer)
        git(["commit", "-m", "main advance"], peer)
        git(["push", "origin", "main"], peer)
        (clone / "scratch.txt").write_text("old branch work\n", encoding="utf-8")
        events, emit = self.collect_events()

        self.mod.switch_branch_one("repo", "dev_target", str(work), emit)

        self.assertEqual("dev_target", git(["branch", "--show-current"], clone))
        self.assertEqual("main advance\n", (clone / "main.txt").read_text(encoding="utf-8"))
        self.assertFalse((clone / "scratch.txt").exists())
        self.assertIn("sync-branches-switch: main", git(["stash", "list"], clone))
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_switch_branch_fetches_remote_only_target(self):
        clone, remote, work = make_project(self.root, "repo")
        peer = make_peer_clone(self.root, remote, "repo")
        git(["checkout", "-b", "dev_remote_switch"], peer)
        git(["push", "-u", "origin", "dev_remote_switch"], peer)
        events, emit = self.collect_events()

        self.mod.switch_branch_one("repo", "dev_remote_switch", str(work), emit)

        self.assertEqual("dev_remote_switch", git(["branch", "--show-current"], clone))
        self.assertEqual("origin/dev_remote_switch", git(["rev-parse", "--abbrev-ref", "@{u}"], clone))
        self.assertEqual("ok", self.latest_result(events)["state"])

    def test_switch_branch_missing_target_errors(self):
        clone, _remote, work = make_project(self.root, "repo")
        events, emit = self.collect_events()

        self.mod.switch_branch_one("repo", "missing_branch", str(work), emit)

        self.assertEqual("main", git(["branch", "--show-current"], clone))
        self.assertEqual("error", self.latest_result(events)["state"])

    def test_switch_branch_conflict_can_resume_without_push_or_pop(self):
        clone, remote, work = make_project(self.root, "repo")
        peer = make_peer_clone(self.root, remote, "repo")
        git(["checkout", "-b", "dev_conflict"], peer)
        (peer / "README.md").write_text("target line\n", encoding="utf-8")
        git(["add", "README.md"], peer)
        git(["commit", "-m", "target change"], peer)
        git(["push", "-u", "origin", "dev_conflict"], peer)
        git(["checkout", "main"], peer)
        (peer / "README.md").write_text("main line\n", encoding="utf-8")
        git(["add", "README.md"], peer)
        git(["commit", "-m", "main change"], peer)
        git(["push", "origin", "main"], peer)
        (clone / "scratch.txt").write_text("old branch work\n", encoding="utf-8")
        events, emit = self.collect_events()

        self.mod.switch_branch_one("repo", "dev_conflict", str(work), emit)

        result = self.latest_result(events)
        self.assertEqual("conflict", result["state"])
        self.assertTrue(result["resume"])
        self.assertEqual("dev_conflict", git(["branch", "--show-current"], clone))
        self.assertTrue((clone / ".git" / "MERGE_HEAD").exists())
        self.assertIn("sync-branches-switch: main", git(["stash", "list"], clone))

        (clone / "README.md").write_text("resolved line\n", encoding="utf-8")
        git(["add", "README.md"], clone)
        resume_events, resume_emit = self.collect_events()
        self.mod.resume_one("repo", "dev_conflict", str(work), resume_emit)

        self.assertEqual("ok", self.latest_result(resume_events)["state"])
        self.assertEqual("dev_conflict", git(["branch", "--show-current"], clone))
        self.assertFalse((clone / "scratch.txt").exists())
        self.assertIn("sync-branches-switch: main", git(["stash", "list"], clone))
        self.assertNotEqual(
            git(["rev-parse", "dev_conflict"], clone),
            git(["rev-parse", "origin/dev_conflict"], clone),
        )


if __name__ == "__main__":
    unittest.main()
