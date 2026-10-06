"""Offline tests for tools/build_patch_from_snapshot.py: waits for a final manifest, verifies hashes,
rebases onto upstream+earlier patch with a three-way merge, stops on conflict. Run: python3 -I tests/test_builder.py"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import build_patch_from_snapshot as B

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}


def sha(t):
    return hashlib.sha256(t.encode()).hexdigest()


class Builder(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="builder-test-"))
        self.up, self.snap, self.out = self.t / "up", self.t / "snap", self.t / "out"
        (self.up / "pkg").mkdir(parents=True); self.snap.mkdir()
        (self.up / "pkg" / "hooks.py").write_text("HOOKS = {\n    'a', 'b',\n}\n")
        for c in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "b"]):
            subprocess.run(["git", *c], cwd=self.up, check=True, capture_output=True, env=ENV)
        # earlier patch edits line 2 of hooks.py
        a, b = self.t / "a", self.t / "b"
        for d, txt in ((a, "HOOKS = {\n    'a', 'b',\n}\n"), (b, "HOOKS = {\n    'a', 'pre', 'b',\n}\n")):
            (d / "pkg").mkdir(parents=True); (d / "pkg" / "hooks.py").write_text(txt)
        r = subprocess.run(["git", "diff", "--no-index", "a", "b"], cwd=self.t, capture_output=True, text=True).stdout
        self.pre = self.t / "pre.patch"
        self.pre.write_text(r.replace("a/a/pkg", "a/pkg").replace("b/b/pkg", "b/pkg"))

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def manifest(self, files, final=True):
        for p, t in files.items():
            (self.snap / p).parent.mkdir(parents=True, exist_ok=True); (self.snap / p).write_text(t)
        m = self.t / "m.json"
        m.write_text(json.dumps({"final": final, "snapshot_dir": str(self.snap), "snapshot_sha256": "parent-owned",
                                 "files": [{"path": p, "sha256": sha(t)} for p, t in files.items()]}))
        return m

    def run_b(self, m):
        return B.main(["--upstream", str(self.up), "--preflight", str(self.pre), "--manifest", str(m), "--out", str(self.out)])

    def test_waits_without_manifest_and_writes_nothing(self):
        self.assertEqual(self.run_b(self.t / "none.json"), 3); self.assertFalse(self.out.exists())

    def test_waits_when_not_final(self):
        self.assertEqual(self.run_b(self.manifest({"pkg/new.py": "x = 1\n"}, final=False)), 3)
        self.assertFalse(self.out.exists())

    def test_refuses_hash_mismatch(self):
        m = self.manifest({"pkg/new.py": "x = 1\n"}); (self.snap / "pkg" / "new.py").write_text("tampered\n")
        self.assertEqual(self.run_b(m), 1)

    def test_refuses_private_content(self):
        self.assertEqual(self.run_b(self.manifest({"pkg/new.py": "p = '/Users/someone/x'\n"})), 1)

    def test_builds_patch_that_applies_on_upstream_plus_earlier_patch(self):
        m = self.manifest({"pkg/new.py": "x = 1\n", "tests/test_new.py": "def test(): pass\n"})
        self.assertEqual(self.run_b(m), 0)
        for name in ("jev-decisions.patch", "jev-decisions.test.patch"):
            self.assertTrue((self.out / name).read_text().startswith("diff --git a/"))
        self.assertNotIn("a/a/", (self.out / "jev-decisions.patch").read_text())
        man = json.loads((self.out / "jev-decisions.manifest.json").read_text())
        self.assertEqual(man["generated_from_parent_snapshot_sha256"], "parent-owned")

    def test_conflicting_line_stops_for_a_person(self):
        m = self.manifest({"pkg/hooks.py": "HOOKS = {\n    'a', 'dec', 'b',\n}\n"})
        self.assertEqual(self.run_b(m), 2); self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
