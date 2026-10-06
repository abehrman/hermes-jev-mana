#!/usr/bin/env python3
"""Transactional installer for the hermes-jev-mana bundle (plugin + thin core patches + config).

    install.sh                 install: stage, simulate, back up, apply, configure, health-check
    install.sh --check         report only; writes nothing (not even git objects)
    install.sh --off           install everything but set jev.decisions to off
    install.sh --uninstall     restore exactly what this installer changed, and only that

All-or-nothing: every patch is first simulated, in order, against a throw-away git index. Any
conflict, a missing bundle asset or an unsupported checkout stops the run before the first write.
After the first write, any failure restores every backed-up byte and removes every created file.

It never reads, writes or asks for a key, and it never restarts anything: the final summary only
prints the exact restart command for the operator to run.

Bundle layout (repo root): bundle.json, plugin/, patches/, config/. See README.md.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

STATE_DIR = "jev-mana"
EXIT = {"ok": 0, "unhealthy": 4, "no_health": 5, "conflict": 2, "unsupported": 3, "failed": 1}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).is_file() else None


class Fail(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind = kind


def run(cmd, cwd=None, env=None, input=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, input=input, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


# ---------- source detection: real Hermes APIs, never fixed paths ----------
def interpreter_of(hermes_bin):
    try:
        first = Path(hermes_bin).resolve().read_text(errors="ignore").splitlines()[0]
    except (OSError, IndexError):
        return None
    return first[2:].strip().split()[0] if first.startswith("#!") else None


def detect(args, runner=run):
    """-> (checkout, home, hermes_bin). Explicit flags win, then env, then the hermes executable's own Python."""
    hermes_bin = args.hermes_bin or shutil.which("hermes")
    interp = interpreter_of(hermes_bin) if hermes_bin else None
    checkout = args.checkout or os.environ.get("HERMES_CHECKOUT")
    if not checkout and interp:
        rc, out, _ = runner([interp, "-I", "-c",
                             "import hermes_cli,pathlib;print(pathlib.Path(hermes_cli.__file__).resolve().parents[1])"])
        checkout = out.strip() if rc == 0 else None
    home = args.hermes_home or os.environ.get("HERMES_HOME")
    if not home and interp and checkout:
        rc, out, _ = runner([interp, "-I", "-c",
                             f"import sys;sys.path.insert(0,{checkout!r});from hermes_constants import get_hermes_home;print(get_hermes_home())"])
        home = out.strip() if rc == 0 else None
    if not (checkout and Path(checkout, "hermes_cli").is_dir()):
        raise Fail("unsupported", "Hermes checkout not found; pass --checkout (a git checkout containing hermes_cli/)")
    if not home or not Path(home).is_dir():
        raise Fail("unsupported", "Hermes home not found; pass --hermes-home")
    if runner(["git", "rev-parse", "--git-dir"], cwd=checkout)[0] != 0:
        raise Fail("unsupported", "the checkout is not a git repository; patches need git to apply and verify")
    return Path(checkout).resolve(), Path(home).resolve(), hermes_bin


def upstream_info(checkout, bundle):
    head = run(["git", "rev-parse", "HEAD"], cwd=checkout)[1].strip()
    desc = run(["git", "describe", "--tags", "--always"], cwd=checkout)[1].strip()
    pinned = any(head.startswith(c) for c in bundle.get("supported_upstream", []))
    return {"head": head, "describe": desc, "pinned_supported": pinned}


# ---------- patch simulation without any write to the checkout ----------
class Sim:
    """Throw-away index AND object store: nothing, not even a loose git object, lands in the checkout."""

    def __init__(self, checkout, patches):
        self.checkout = Path(checkout)
        self.tmp = Path(tempfile.mkdtemp(prefix="jev-mana-sim-"))
        gitdir = Path(run(["git", "rev-parse", "--absolute-git-dir"], cwd=checkout)[1].strip())
        self.env = {**os.environ, "GIT_INDEX_FILE": str(self.tmp / "index"),
                    "GIT_OBJECT_DIRECTORY": str(self.tmp / "objects"),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(gitdir / "objects")}
        (self.tmp / "objects").mkdir()
        run(["git", "read-tree", "HEAD"], cwd=checkout, env=self.env)
        paths = sorted({p for f in patches for p in touched(checkout, f)})
        for p in paths:
            fp = self.checkout / p
            if fp.is_file():
                h = run(["git", "hash-object", "-w", str(fp)], cwd=checkout, env=self.env)[1].strip()
                run(["git", "update-index", "--add", "--cacheinfo", f"100644,{h},{p}"], cwd=checkout, env=self.env)
            else:
                run(["git", "update-index", "--force-remove", "--", p], cwd=checkout, env=self.env)

    def apply(self, patch, *flags):
        return run(["git", "apply", "--cached", *flags, str(patch)], cwd=self.checkout, env=self.env)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def touched(checkout, patch):
    out = run(["git", "apply", "--numstat", "-z", str(patch)], cwd=checkout)[1]
    return [f.split("\t")[-1] for f in out.split("\0") if "\t" in f] or \
           re.findall(r"^diff --git a/(\S+) b/", Path(patch).read_text(), re.M)


def plan_patches(checkout, bundle_root, series):
    """-> list of (name, path, action) with action applied|apply; raises Fail on conflict/missing."""
    files = []
    for entry in series:
        f = bundle_root / entry["patch"]
        if not f.is_file():
            raise Fail("conflict", f"bundle asset missing: {entry['patch']}")
        files.append(f)
    sim, plan, conflicts = Sim(checkout, files), [], []
    try:
        for entry, f in zip(series, files):
            if sim.apply(f, "--check", "-R")[0] == 0:
                plan.append((entry["patch"], f, "applied"))
            else:
                rc, _, err = sim.apply(f, "--check")
                if rc == 0:
                    sim.apply(f)
                    plan.append((entry["patch"], f, "apply"))
                else:
                    conflicts.append((entry["patch"], err.strip().splitlines()[:3]))
    finally:
        sim.close()
    if conflicts:
        raise Fail("conflict", "CONFLICT " + "; ".join(f"{n}: {' | '.join(e)}" for n, e in conflicts))
    return plan


# ---------- configuration through the normal Hermes CLI ----------
def config_path(home, profile):
    return Path(home, "config.yaml") if profile in (None, "default") else Path(home, "profiles", profile, "config.yaml")


def prior_decisions(path):
    m = re.search(r"(?m)^jev:\s*\n(?:[ \t]+.*\n)*?[ \t]+decisions:\s*(\S+)", path.read_text()) if path.is_file() else None
    return m.group(1) if m else None


def discover_profiles(home):
    root = Path(home, "profiles")
    return sorted(p.name for p in root.iterdir() if (p / "config.yaml").is_file()) if root.is_dir() else []


def hermes_cmd(hermes_bin, profile, *rest):
    return [hermes_bin, *(["-p", profile] if profile not in (None, "default") else []), *rest]


# ---------- transaction ----------
class Tx:
    def __init__(self, home, txid):
        self.root = Path(home, STATE_DIR, "backup", txid)
        self.files = {}   # abs path -> {"pre": sha|None, "backup": rel|None}

    def own(self, path):
        path = Path(path)
        key = str(path)
        if key in self.files:
            return
        pre = sha(path)
        backup = None
        if pre is not None:
            backup = f"{len(self.files)}"
            dest = self.root / backup
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        self.files[key] = {"pre": pre, "backup": backup}

    def rollback(self):
        for key, meta in self.files.items():
            p = Path(key)
            if meta["backup"] is None:
                p.unlink(missing_ok=True)
                _prune(p.parent)
            else:
                shutil.copy2(self.root / meta["backup"], p)


def _prune(d):
    try:
        while d.name and not any(d.iterdir()):
            d.rmdir(); d = d.parent
    except OSError:
        pass


def plugin_files(bundle_root, bundle):
    src = bundle_root / bundle["plugin_dir"]
    return [p.relative_to(src).as_posix() for p in sorted(src.rglob("*")) if p.is_file() and "__pycache__" not in p.parts]


def selected_profiles(args, home):
    if args.all_profiles:
        return ["default", *discover_profiles(home)]
    return args.profile or ["default"]


def load_bundle(root):
    f = root / "bundle.json"
    if not f.is_file():
        raise Fail("unsupported", "bundle.json missing: this checkout of the installer has no bundle to install")
    b = json.loads(f.read_text())
    for need in ("plugin_name", "plugin_dir", "series"):
        if need not in b:
            raise Fail("unsupported", f"bundle.json lacks {need}")
    if not (root / b["plugin_dir"]).is_dir():
        raise Fail("conflict", f"bundle asset missing: {b['plugin_dir']}/")
    return b


def do_install(args, root, out):
    bundle = load_bundle(root)
    checkout, home, hermes_bin = detect(args)
    info = upstream_info(checkout, bundle)
    out(f"upstream: {info['describe']} ({info['head'][:12]}); " +
        ("pinned-supported" if info["pinned_supported"] else "not a pinned commit; the apply simulation decides"))
    plan = plan_patches(checkout, root, bundle["series"])
    pdir = home / "plugins" / bundle["plugin_name"]
    pfiles = plugin_files(root, bundle)
    profiles = selected_profiles(args, home)
    state_file = home / STATE_DIR / "install.json"
    already = (state_file.is_file() and json.loads(state_file.read_text()).get("bundle_version") == bundle.get("version")
               and all(a == "applied" for _, _, a in plan)
               and all(sha(pdir / r) == sha(root / bundle["plugin_dir"] / r) for r in pfiles))
    for name, _, action in plan:
        out(f"{'OK     ' if action == 'applied' else 'APPLY  '} {name}")
    if state_file.is_file() and not already:
        raise Fail("conflict", "an earlier install of a different bundle version is recorded; run --uninstall first")
    if args.check:
        out("check only: nothing written" + ("; already installed" if already else ""))
        return {"changed": False, "already": already, "profiles": profiles, "home": home, "hermes_bin": hermes_bin,
                "plugin_dir": pdir, "checkout": checkout}
    if already:
        out("already installed; nothing to do")
        return {"changed": False, "already": True, "profiles": profiles, "home": home, "hermes_bin": hermes_bin,
                "plugin_dir": pdir, "checkout": checkout}
    if not hermes_bin:
        raise Fail("unsupported", "hermes executable not found; pass --hermes-bin (configuration uses `hermes config set`)")

    tx = Tx(home, time.strftime("%Y%m%dT%H%M%S"))
    created_dirs = []
    try:
        for name, f, action in plan:
            if action == "apply":
                for p in touched(checkout, f):
                    tx.own(checkout / p)
        for r in pfiles:
            tx.own(pdir / r)
        examples = []
        for ex in bundle.get("examples", []):
            for prof in profiles:
                dest = (home if prof == "default" else home / "profiles" / prof) / ex["dest"]
                if not dest.exists():          # copy absent only
                    tx.own(dest); examples.append((root / ex["src"], dest))
        for prof in profiles:
            tx.own(config_path(home, prof))
        priors = {p: prior_decisions(config_path(home, p)) for p in profiles}
        for name, f, action in plan:
            if action == "apply":
                rc, _, err = run(["git", "apply", str(f)], cwd=checkout)
                if rc: raise Fail("failed", f"apply failed for {name}: {err.strip()[:200]}")
                if run(["git", "apply", "--check", "-R", str(f)], cwd=checkout)[0]:
                    raise Fail("failed", f"post-apply verify failed for {name}")
        for r in pfiles:
            (pdir / r).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / bundle["plugin_dir"] / r, pdir / r)
        for src, dest in examples:
            dest.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dest)
        value = "off" if args.off else "on"
        for prof in profiles:
            for cmd in (("plugins", "enable", bundle["plugin_name"]), ("config", "set", "jev.decisions", value)):
                rc, _, err = run(hermes_cmd(hermes_bin, prof, *cmd))
                if rc: raise Fail("failed", f"`hermes {' '.join(cmd)}` failed for profile {prof}: {err.strip()[:200]}")
        record = {"schema": "jev-mana-install/1", "bundle_version": bundle.get("version"), "txid": tx.root.name,
                  "backup_root": str(tx.root), "profiles": profiles, "decisions": value, "prior_decisions": priors,
                  "patches": [n for n, _, _ in plan],
                  "files": [{"path": k, **v, "post": sha(k)} for k, v in tx.files.items()]}
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(record, indent=2) + "\n")
    except Exception as e:
        tx.rollback()
        shutil.rmtree(tx.root, ignore_errors=True)
        (home / STATE_DIR / "install.json").unlink(missing_ok=True)
        _prune(home / STATE_DIR / "backup")
        out(f"ROLLED BACK: every changed file restored ({len(tx.files)} owned); cause: {e}")
        raise e if isinstance(e, Fail) else Fail("failed", str(e))
    out(f"installed: {len(plan)} patch(es), {len(pfiles)} plugin file(s), decisions={value}")
    return {"changed": True, "already": False, "profiles": profiles, "home": home, "hermes_bin": hermes_bin,
            "plugin_dir": pdir, "checkout": checkout}


def do_uninstall(args, out):
    checkout, home, _ = detect(args)
    sf = home / STATE_DIR / "install.json"
    if not sf.is_file():
        out("nothing recorded; nothing to uninstall"); return 0
    rec = json.loads(sf.read_text())
    root, kept = Path(rec["backup_root"]), []
    if args.check:
        for m in rec["files"]:
            out(f"{'would restore' if sha(m['path']) == m['post'] else 'would LEAVE (changed since install)'}: {m['path']}")
        return 0
    for m in rec["files"]:
        p = Path(m["path"])
        if sha(p) != m["post"]:
            kept.append(m["path"]); continue          # someone else changed it: not ours to revert
        if m["backup"] is None:
            p.unlink(missing_ok=True); _prune(p.parent)
        else:
            shutil.copy2(root / m["backup"], p)
    for k in kept:
        out(f"left in place (modified after install): {k}")
    if not kept:
        shutil.rmtree(root, ignore_errors=True); sf.unlink(); _prune(sf.parent / "backup")
        _prune(sf.parent)
    out(f"uninstalled: {len(rec['files']) - len(kept)} file(s) restored, {len(kept)} left")
    return 0 if not kept else 1


def prerequisites(out):
    out("prerequisites (the installer handles no keys):")
    out(f"  Jev key: {'jev CLI found' if shutil.which('jev') else 'jev CLI not found'}; "
        "run `jev setup-key` yourself in a private terminal. Without a key every decision point falls back to the original branch.")
    out(f"  Executor sign-in: {'claude CLI found' if shutil.which('claude') else 'claude CLI not found'}; "
        "sign in yourself if you use the work.executor point. Missing sign-in means the original Hermes path runs, and health shows it.")


def health(ctx, bundle, root, out, allow_missing):
    h = bundle.get("health")
    if not h:
        out("health: UNAVAILABLE (this bundle declares no health command)")
        return "no_health" if not allow_missing else "ok"
    argv = [a.replace("{python}", sys.executable).replace("{plugin}", str(ctx["plugin_dir"])).replace("{home}", str(ctx["home"]))
            for a in h["argv"]]
    if not Path(argv[1] if len(argv) > 1 else argv[0]).exists() and not shutil.which(argv[0]):
        out("health: UNAVAILABLE (declared health asset is missing)")
        return "no_health" if not allow_missing else "ok"
    rc, so, se = run(argv)
    out(f"health: {'HEALTHY' if rc == 0 else 'UNHEALTHY (decision points fall back to the original branch)'}")
    for line in (so or se).strip().splitlines()[-6:]:
        out(f"  {line}")
    return "ok" if rc == 0 else "unhealthy"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install the hermes-jev-mana bundle transactionally.")
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--off", action="store_true", help="install but leave jev.decisions off")
    ap.add_argument("--profile", action="append", help="profile to configure (repeatable); default: the default profile")
    ap.add_argument("--all-profiles", action="store_true")
    ap.add_argument("--checkout"); ap.add_argument("--hermes-home"); ap.add_argument("--hermes-bin")
    ap.add_argument("--dashboard-port", type=int, default=9119)
    ap.add_argument("--allow-no-health", action="store_true")
    ap.add_argument("--bundle-root", default=str(Path(__file__).resolve().parent.parent))
    a = ap.parse_args(argv)
    out = lambda s: print(s, flush=True)
    root = Path(a.bundle_root)
    try:
        if a.uninstall:
            return do_uninstall(a, out)
        ctx = do_install(a, root, out)
        if a.check:
            return 0
        bundle = load_bundle(root)
        status = health(ctx, bundle, root, out, a.allow_no_health)
        prerequisites(out)
        out(f"dashboard: http://127.0.0.1:{a.dashboard_port}/ (Jev page appears in the standard dashboard after a restart)")
        flag = "" if ctx["profiles"] == ["default"] else " (repeat per profile with -p NAME)"
        out(f"restart (run it yourself when ready; not executed): hermes gateway restart{flag}")
        return EXIT[status]
    except Fail as e:
        out(f"{e.kind.upper()}: {e}")
        return EXIT[e.kind]


if __name__ == "__main__":
    sys.exit(main())
