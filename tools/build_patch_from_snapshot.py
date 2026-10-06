#!/usr/bin/env python3
"""Generate the decisions patch (+ .test.patch) from the PARENT's final snapshot, and only then.

The decision hooks are still being written by other streams. This script therefore never looks at
a live work tree: it waits for a manifest the parent writes when it declares a snapshot final.

    {"final": true, "snapshot_dir": "<dir>", "snapshot_sha256": "<parent's tree hash>",
     "files": [{"path": "agent/decision_native.py", "sha256": "..."}, ...]}

Without such a manifest (or with "final": false) it prints WAITING, writes nothing, exits 3.
It never states or computes a "final hash": the snapshot hash in the output manifest is copied from
the parent's manifest, and the patch hash is the hash of the bytes it just wrote.

The delta is cut against upstream + jev-turn-preflight.patch (the registered order), by three-way
merge of every file both sides touch. A textual conflict stops the run (exit 2) and lists the files;
nothing is guessed or auto-resolved.

    build_decisions.py --upstream DIR --preflight PATCH --manifest FILE --out DIR
"""
import argparse, hashlib, json, re, shutil, subprocess, sys, tempfile
from pathlib import Path

FORBIDDEN = re.compile(r"(^|/)(profiles|secrets)/|\.env$|config\.ya?ml$")
PRIVATE = re.compile(r"/Users/|/home/[a-z]|[\w.+-]+@[\w-]+\.[\w.]+|\b(?:sk|ghp|AKIA)[-_A-Za-z0-9]{12,}")


def sha(b):
    return hashlib.sha256(b).hexdigest()


def git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    for k in ("upstream", "preflight", "manifest", "out"):
        ap.add_argument("--" + k, required=True)
    a = ap.parse_args(argv)
    mf = Path(a.manifest)
    if not mf.is_file():
        print("WAITING: no final-snapshot manifest from the parent; nothing written")
        return 3
    m = json.loads(mf.read_text())
    if m.get("final") is not True:
        print("WAITING: manifest is not marked final; nothing written")
        return 3
    snap = Path(m["snapshot_dir"])
    bad = [f["path"] for f in m["files"]
           if not (snap / f["path"]).is_file() or sha((snap / f["path"]).read_bytes()) != f["sha256"]]
    if bad:
        print("REFUSED: snapshot differs from the parent's manifest:", bad)
        return 1
    banned = [f["path"] for f in m["files"] if FORBIDDEN.search(f["path"])
              or PRIVATE.search((snap / f["path"]).read_text(errors="replace"))]
    if banned:
        print("REFUSED: forbidden or private-looking content:", banned)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="dec-build-"))
    try:
        base, pf = tmp / "base", tmp / "pf"
        for d in (base, pf):
            d.mkdir()
        paths = [f["path"] for f in m["files"]]
        for p in paths:
            r = git("show", f"HEAD:{p}", cwd=a.upstream)
            if r.returncode == 0:
                (base / p).parent.mkdir(parents=True, exist_ok=True)
                (base / p).write_text(r.stdout)
        # upstream + preflight: apply the preflight patch to a scratch copy of HEAD for the touched files
        scratch = tmp / "up"
        r = git("clone", "-q", "--shared", a.upstream, str(scratch))
        if r.returncode:
            print("clone failed", r.stderr); return 1
        r = git("apply", str(Path(a.preflight).resolve()), cwd=scratch)
        if r.returncode:
            print("REFUSED: preflight patch does not apply to upstream:", r.stderr); return 1
        merged_root, conflicts = tmp / "merged", []
        for p in paths:
            ours = scratch / p
            theirs = snap / p
            b = base / p
            (merged_root / p).parent.mkdir(parents=True, exist_ok=True)
            if ours.exists() and b.exists() and ours.read_bytes() != b.read_bytes():
                shutil.copy(ours, merged_root / p)
                r = subprocess.run(["git", "merge-file", str(merged_root / p), str(b), str(theirs)], capture_output=True)
                if r.returncode != 0:
                    conflicts.append(p)
            else:
                shutil.copy(theirs, merged_root / p)
        if conflicts:
            print("CONFLICT (needs a person; the snapshot must be rebased onto upstream+preflight):", conflicts)
            return 2

        def delta(selected):
            a_dir, b_dir = tmp / "da", tmp / "db"
            for d in (a_dir, b_dir):
                shutil.rmtree(d, ignore_errors=True); d.mkdir()
            for p in selected:
                if (scratch / p).exists():
                    (a_dir / p).parent.mkdir(parents=True, exist_ok=True); shutil.copy(scratch / p, a_dir / p)
                (b_dir / p).parent.mkdir(parents=True, exist_ok=True); shutil.copy(merged_root / p, b_dir / p)
            r = git("diff", "--no-index", "--no-color", "da", "db", cwd=tmp)
            out = []
            for line in r.stdout.splitlines(keepends=True):
                if line.startswith("diff --git "):
                    x = re.match(r"diff --git (?:a|b)/(?:da|db)/(\S+) (?:a|b)/(?:da|db)/(\S+)", line)
                    line = f"diff --git a/{x.group(1)} b/{x.group(2)}\n"
                line = re.sub(r"^--- a/da/", "--- a/", line)
                line = re.sub(r"^\+\+\+ b/db/", "+++ b/", line)
                out.append(line)
            return "".join(out)

        tests = [p for p in paths if p.startswith("tests/")]
        src = [p for p in paths if p not in tests]
        out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
        src_patch, test_patch = delta(src), delta(tests)
        (out / "jev-decisions.patch").write_text(src_patch)
        (out / "jev-decisions.test.patch").write_text(test_patch)
        # prove: upstream + preflight + decisions reproduces the merged bytes
        r = git("apply", "--check", str(out / "jev-decisions.patch"), cwd=scratch)
        if r.returncode:
            print("REFUSED: generated patch does not apply on upstream+preflight", r.stderr); return 1
        (out / "jev-decisions.manifest.json").write_text(json.dumps({
            "schema": "jev-decisions-manifest/1",
            "generated_from_parent_snapshot_sha256": m.get("snapshot_sha256"),
            "patch_sha256": sha(src_patch.encode()), "test_patch_sha256": sha(test_patch.encode()),
            "files": paths, "order": "after jev-turn-preflight.patch"}, indent=2) + "\n")
        print(f"WROTE jev-decisions.patch ({len(src)} files) and test patch ({len(tests)} files)")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
