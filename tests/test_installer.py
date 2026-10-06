"""Offline installer tests on throw-away fixtures: a toy git checkout, a toy bundle and a fake `hermes`.
No network, no real Hermes, no keys. Run: python3 -I tests/test_installer.py
"""
import contextlib, hashlib, io, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import jev_mana_install as I

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
FAKE_HERMES = """#!{py}
import os, sys
from pathlib import Path
a = sys.argv[1:]
prof = None
if a[:1] == ["-p"]:
    prof, a = a[1], a[2:]
home = Path(os.environ["FAKE_HERMES_HOME"])
cfg = (home if prof in (None, "default") else home / "profiles" / prof) / "config.yaml"
if a[:2] == ["config", "set"]:
    if os.environ.get("FAKE_HERMES_FAIL") == "config":
        sys.exit(1)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    old = cfg.read_text() if cfg.exists() else ""
    cfg.write_text(old + f"# set {{a[2]}}={{a[3]}}\\njev:\\n  decisions: {{a[3]}}\\n")
elif a[:2] == ["plugins", "enable"]:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text((cfg.read_text() if cfg.exists() else "") + f"# enabled {{a[2]}}\\n")
"""


def git(*a, cwd):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT_ENV})


def snapshot(*roots, skip_git=True):
    out = {}
    for r in roots:
        for p in sorted(Path(r).rglob("*")):
            if p.is_file() and not (skip_git and ".git" in p.parts):
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Fixture(unittest.TestCase):
    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="mana-test-"))
        self.co, self.home, self.bundle = self.t / "co", self.t / "home", self.t / "bundle"
        for d in (self.co / "hermes_cli", self.home, self.bundle / "plugin", self.bundle / "patches", self.bundle / "config"):
            d.mkdir(parents=True)
        (self.co / "hermes_cli" / "__init__.py").write_text("")
        (self.co / "hermes_cli" / "a.py").write_text("one\ntwo\nthree\n")
        (self.co / "hermes_cli" / "b.py").write_text("alpha\nbeta\n")
        git("init", "-q", cwd=self.co); git("add", "-A", cwd=self.co); git("commit", "-qm", "base", cwd=self.co)
        self.patch("01-a.patch", "hermes_cli/a.py", "one\ntwo\nthree\n", "one\nTWO\nthree\n")
        self.patch("02-b.patch", "hermes_cli/b.py", "alpha\nbeta\n", "alpha\nbeta\ngamma\n")
        (self.bundle / "plugin" / "plugin.yaml").write_text("name: toy\n")
        (self.bundle / "plugin" / "__init__.py").write_text("# toy\n")
        (self.bundle / "plugin" / "health.py").write_text("import sys\nprint('toy ok')\nsys.exit(0)\n")
        (self.bundle / "config" / "jev_config.example.yaml").write_text("margin: 0.15\n")
        self.write_bundle()
        self.hermes = self.t / "hermes"
        self.hermes.write_text(FAKE_HERMES.format(py=sys.executable)); self.hermes.chmod(0o755)
        os.environ["FAKE_HERMES_HOME"] = str(self.home)
        os.environ.pop("FAKE_HERMES_FAIL", None)

    def tearDown(self):
        shutil.rmtree(self.t, ignore_errors=True)

    def patch(self, name, rel, old, new):
        a, b = self.t / "pa", self.t / "pb"
        for d in (a, b):
            shutil.rmtree(d, ignore_errors=True)
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (a / rel).write_text(old); (b / rel).write_text(new)
        r = subprocess.run(["git", "diff", "--no-index", "--no-color", "pa", "pb"], cwd=self.t, capture_output=True, text=True)
        text = r.stdout.replace(f"a/pa/{rel}", f"a/{rel}").replace(f"b/pb/{rel}", f"b/{rel}")
        (self.bundle / "patches" / name).write_text(text)

    def write_bundle(self, **over):
        b = {"version": "1", "plugin_name": "toy", "plugin_dir": "plugin",
             "series": [{"patch": "patches/01-a.patch"}, {"patch": "patches/02-b.patch"}],
             "examples": [{"src": "config/jev_config.example.yaml", "dest": "jev_config.yaml"}],
             "health": {"argv": ["{python}", "{plugin}/health.py"]}, **over}
        (self.bundle / "bundle.json").write_text(json.dumps(b))

    def go(self, *flags):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = I.main(["--bundle-root", str(self.bundle), "--checkout", str(self.co), "--hermes-home", str(self.home),
                         "--hermes-bin", str(self.hermes), *flags])
        return rc, buf.getvalue()


class Install(Fixture):
    def test_check_writes_nothing_even_in_git(self):
        before = snapshot(self.co, self.home, skip_git=False)
        rc, out = self.go("--check")
        self.assertEqual(rc, 0); self.assertIn("APPLY", out)
        self.assertEqual(snapshot(self.co, self.home, skip_git=False), before)

    def test_install_sets_on_and_prints_restart_without_running_it(self):
        rc, out = self.go()
        self.assertEqual(rc, 0, out)
        self.assertIn("TWO", (self.co / "hermes_cli" / "a.py").read_text())
        self.assertTrue((self.home / "plugins" / "toy" / "plugin.yaml").exists())
        self.assertIn("decisions: on", (self.home / "config.yaml").read_text())
        self.assertIn("hermes gateway restart", out); self.assertIn("not executed", out)
        self.assertIn("HEALTHY", out); self.assertIn("jev setup-key", out); self.assertIn("127.0.0.1:9119", out)

    def test_off_flag(self):
        self.go("--off")
        self.assertIn("decisions: off", (self.home / "config.yaml").read_text())

    def test_idempotent(self):
        self.go(); snap = snapshot(self.co, self.home)
        rc, out = self.go()
        self.assertEqual(rc, 0); self.assertIn("already installed", out)
        self.assertEqual(snapshot(self.co, self.home), snap)

    def test_conflict_in_second_patch_writes_nothing(self):
        (self.co / "hermes_cli" / "b.py").write_text("alpha\nDRIFT\n")
        before = snapshot(self.co, self.home)
        rc, out = self.go()
        self.assertEqual(rc, 2); self.assertIn("CONFLICT", out); self.assertIn("02-b.patch", out)
        self.assertEqual(snapshot(self.co, self.home), before)
        self.assertIn("two", (self.co / "hermes_cli" / "a.py").read_text())      # patch 1 was NOT applied

    def test_missing_bundle_asset_refuses_visibly(self):
        (self.bundle / "patches" / "02-b.patch").unlink()
        before = snapshot(self.co, self.home)
        rc, out = self.go()
        self.assertEqual(rc, 2); self.assertIn("bundle asset missing", out)
        self.assertEqual(snapshot(self.co, self.home), before)

    def test_failure_after_writes_rolls_back_byte_identical(self):
        (self.home / "config.yaml").write_text("model: keep-me\n")
        (self.home / "plugins" / "toy").mkdir(parents=True)
        (self.home / "plugins" / "toy" / "__init__.py").write_text("# pre-existing\n")
        before = snapshot(self.co, self.home)
        os.environ["FAKE_HERMES_FAIL"] = "config"
        rc, out = self.go()
        self.assertEqual(rc, 1); self.assertIn("ROLLED BACK", out)
        self.assertEqual(snapshot(self.co, self.home), before)

    def test_examples_copy_absent_only(self):
        (self.home / "jev_config.yaml").write_text("mine: true\n")
        self.go()
        self.assertEqual((self.home / "jev_config.yaml").read_text(), "mine: true\n")
        self.go("--uninstall")
        self.assertEqual((self.home / "jev_config.yaml").read_text(), "mine: true\n")

    def test_missing_health_is_visible_and_nonzero(self):
        self.write_bundle(health=None)
        rc, out = self.go()
        self.assertEqual(rc, 5); self.assertIn("health: UNAVAILABLE", out)
        self.assertEqual(self.go("--allow-no-health", "--check")[0], 0)

    def test_unhealthy_keeps_install_but_says_so(self):
        (self.bundle / "plugin" / "health.py").write_text("import sys\nprint('bad')\nsys.exit(1)\n")
        rc, out = self.go()
        self.assertEqual(rc, 4); self.assertIn("UNHEALTHY", out); self.assertIn("original branch", out)
        self.assertIn("TWO", (self.co / "hermes_cli" / "a.py").read_text())

    def test_different_bundle_version_refuses_until_uninstall(self):
        self.go(); self.write_bundle(version="2")
        rc, out = self.go()
        self.assertEqual(rc, 2); self.assertIn("--uninstall", out)

    def test_profiles(self):
        (self.home / "profiles" / "p1").mkdir(parents=True)
        (self.home / "profiles" / "p1" / "config.yaml").write_text("x: 1\n")
        self.go("--all-profiles")
        self.assertIn("decisions: on", (self.home / "profiles" / "p1" / "config.yaml").read_text())
        self.assertTrue((self.home / "profiles" / "p1" / "jev_config.yaml").exists())


class Uninstall(Fixture):
    def test_uninstall_restores_everything_byte_identical(self):
        (self.home / "config.yaml").write_text("model: keep-me\n")
        (self.home / "plugins" / "toy").mkdir(parents=True)
        (self.home / "plugins" / "toy" / "__init__.py").write_text("# pre-existing\n")
        (self.home / "plugins" / "toy" / "extra.py").write_text("# user file\n")
        before = snapshot(self.co, self.home)
        self.assertEqual(self.go()[0], 0)
        rc, out = self.go("--uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(snapshot(self.co, self.home), before)
        self.assertFalse((self.home / "jev-mana").exists())

    def test_uninstall_leaves_files_changed_after_install(self):
        self.go()
        (self.co / "hermes_cli" / "a.py").write_text("one\nTWO\nthree\nlocal edit\n")
        (self.home / "config.yaml").write_text((self.home / "config.yaml").read_text() + "later: 1\n")
        rc, out = self.go("--uninstall")
        self.assertEqual(rc, 1); self.assertIn("left in place", out)
        self.assertIn("local edit", (self.co / "hermes_cli" / "a.py").read_text())
        self.assertIn("later: 1", (self.home / "config.yaml").read_text())
        self.assertNotIn("gamma", (self.co / "hermes_cli" / "b.py").read_text())    # untouched files still restored

    def test_uninstall_check_writes_nothing(self):
        self.go(); snap = snapshot(self.co, self.home)
        self.go("--uninstall", "--check")
        self.assertEqual(snapshot(self.co, self.home), snap)

    def test_nothing_recorded(self):
        rc, out = self.go("--uninstall")
        self.assertEqual(rc, 0); self.assertIn("nothing recorded", out)


class Detection(unittest.TestCase):
    def test_uses_hermes_own_interpreter_not_fixed_paths(self):
        t = Path(tempfile.mkdtemp())
        try:
            (t / "co" / "hermes_cli").mkdir(parents=True); (t / "home").mkdir()
            subprocess.run(["git", "init", "-q"], cwd=t / "co", check=True)
            exe = t / "hermes"; exe.write_text("#!/opt/venv/bin/python\n")
            calls = []

            def runner(cmd, cwd=None, env=None, input=None):
                calls.append(cmd)
                if cmd[0] == "/opt/venv/bin/python" and "hermes_cli.__file__" in cmd[-1]:
                    return 0, str(t / "co") + "\n", ""
                if cmd[0] == "/opt/venv/bin/python":
                    return 0, str(t / "home") + "\n", ""
                return 0, "", ""

            ns = type("A", (), {"hermes_bin": str(exe), "checkout": None, "hermes_home": None})()
            os.environ.pop("HERMES_CHECKOUT", None); os.environ.pop("HERMES_HOME", None)
            co, home, _ = I.detect(ns, runner)
            self.assertEqual((co, home), ((t / "co").resolve(), (t / "home").resolve()))
            self.assertGreaterEqual(len(calls), 2)
        finally:
            shutil.rmtree(t, ignore_errors=True)

    def test_unknown_checkout_is_refused(self):
        ns = type("A", (), {"hermes_bin": None, "checkout": "/nonexistent-xyz", "hermes_home": "/tmp"})()
        with self.assertRaises(I.Fail):
            I.detect(ns)


class NoKeys(unittest.TestCase):
    def test_installer_source_never_touches_key_material(self):
        src = (Path(__file__).resolve().parents[1] / "tools" / "jev_mana_install.py").read_text()
        for bad in ("TYPESAFE_API_KEY", "keystore", "getpass", "open(os.path.expanduser"):
            self.assertNotIn(bad, src)


if __name__ == "__main__":
    unittest.main()
