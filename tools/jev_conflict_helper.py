#!/usr/bin/env python3
"""Manual conflict helper for the local Hermes patches, to sit beside reapply_local_patches*.sh.

Deterministic first. Without --jev nothing leaves this machine and nothing but `git apply --check`
runs. With an explicit --jev, for a patch that is in CONFLICT:

  1. Noul: does upstream HEAD already provide a generic hook with the contract REPORT-SPEC.md
     describes? (synthetic public state only; see PrivacyGate)
  2. native equivalent  -> write a RETIRE PROPOSAL for Big Mac. Nothing is retired.
     no equivalent      -> `jev lane classify` picks a lane; a Claude CLI session re-ports the hook
                           from REPORT-SPEC.md inside an isolated `git worktree` of upstream HEAD.
     unclear            -> stop; a person decides.
  3. Two-state tests (pristine upstream, then re-ported) and `jev lane step` on the re-ported tree.
  4. Citation checks on the re-port report: one Choice per claim, not_enough / supports /
     contradicts (in that order); a not_enough is re-located with git grep and asked once more.
  5. A local JSON receipt for the operator. The candidate patch goes next to it.

It never writes the patches directory or the checkout, never restarts anything, never publishes,
and never sends a working-tree byte anywhere: only committed upstream text, the public spec and
the patch's own file names are ever put in a prompt, and only after PrivacyGate passes.
"""
import argparse, json, os, re, shlex, subprocess, sys, tempfile, time
from pathlib import Path

NATIVE_YES, NATIVE_NO = 0.85, 0.35          # Noul probability bands; between them a person decides
CITATION_ORDER = ("not_enough", "supports", "contradicts")
MAX_STATE_BYTES = 24_000
PRIVATE = re.compile(
    r"/Users/|/home/[a-z]|\.hermes/(profiles|kanban|shared|operations)|[\w.+-]+@[\w-]+\.[\w.]+|"
    r"\b(?:sk|pk|xox[bp]|ghp|AKIA)[-_A-Za-z0-9]{12,}|\b[A-Fa-f0-9]{40,}\b|api[_-]?key\s*[:=]|password\s*[:=]|secret\s*[:=]",
    re.I)


class PrivacyGate:
    """Deterministic: refuses any state that looks like a path, address, credential or profile data."""

    @staticmethod
    def check(text):
        if len(text.encode()) > MAX_STATE_BYTES:
            return False, "too_large"
        m = PRIVATE.search(text)
        return (False, "private_shape") if m else (True, "ok")


class Runner:
    """Thin seam over subprocess so tests can run offline with mocks."""

    def __init__(self, run=subprocess.run):
        self._run = run
        self.calls = []

    def __call__(self, cmd, cwd=None, input=None, timeout=900, env=None):
        self.calls.append({"cmd": list(cmd), "cwd": str(cwd) if cwd else None})
        r = self._run(list(cmd), cwd=cwd, input=input, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout, r.stderr


def git_state(run, checkout, patch):
    """applied | missing | conflict, plus git's own words for the conflict."""
    if run(["git", "apply", "--check", "-R", str(patch)], cwd=checkout)[0] == 0:
        return "applied", ""
    rc, _, err = run(["git", "apply", "--check", str(patch)], cwd=checkout)
    return ("missing", "") if rc == 0 else ("conflict", err.strip())


def conflict_files(err):
    pairs = re.findall(r"patch failed: (\S+?):\d+|^error: (\S+): patch does not apply", err, re.M)
    return sorted({a or b for a, b in pairs})


def spec_section(spec_text, patch_name):
    """The REPORT-SPEC.md section whose heading names the patch (public, hand-written text)."""
    stem = patch_name.removesuffix(".patch")
    parts = re.split(r"(?m)^(?=## )", spec_text)
    hit = [p for p in parts if p.startswith("## ") and stem in p.splitlines()[0]]
    return hit[0] if hit else ""


def jev_ask(run, state, questions):
    rc, out, err = run(["jev", "ask"], input=json.dumps({"state": state, "questions": questions}), timeout=60)
    if rc != 0:
        return None, f"jev_ask_failed:{rc}"
    try:
        return json.loads(out)["answers"], None
    except (ValueError, KeyError):
        return None, "jev_ask_unparseable"


def upstream_text(run, checkout, files, spec_names):
    """Committed upstream text only: HEAD blobs, never the working tree."""
    chunks = []
    for f in files:
        rc, out, _ = run(["git", "show", f"HEAD:{f}"], cwd=checkout)
        if rc != 0:
            continue
        lines = out.splitlines()
        keep = [i for i, l in enumerate(lines) if any(n in l for n in spec_names)]
        for i in keep[:6]:
            chunks.append(f"{f}:{i + 1}\n" + "\n".join(lines[max(0, i - 3): i + 4]))
    return "\n---\n".join(chunks)


class Helper:
    def __init__(self, run, checkout, patches_dir, spec_path, receipt_dir, worktree_root, test_script,
                 scope, max_cycles=3):
        self.run, self.checkout, self.spec_path = run, Path(checkout), Path(spec_path)
        self.patches_dir = Path(patches_dir).resolve()
        self.receipt_dir, self.worktree_root = Path(receipt_dir).resolve(), Path(worktree_root).resolve()
        self.test_script, self.scope, self.max_cycles = test_script, scope, max_cycles
        self.receipt_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.receipt_dir, self.worktree_root):   # hard refusal: never write where patches live
            if d == self.patches_dir or self.patches_dir in d.parents or d == self.checkout.resolve() or self.checkout.resolve() in d.parents:
                raise SystemExit(f"refused: {d} is inside the patches dir or the checkout")

    # ---- deterministic ----
    def inspect(self, patch):
        state, err = git_state(self.run, self.checkout, patch)
        return {"patch": Path(patch).name, "state": state, "git_error": err.splitlines()[:6],
                "files": conflict_files(err) if state == "conflict" else []}

    # ---- jev-assisted ----
    def native_equivalent(self, finding, spec):
        names = re.findall(r"`([A-Za-z_][\w.]*)`", spec)[:12]
        state = ("Hook contract (public spec):\n" + spec[:6000] +
                 "\n\nUpstream HEAD excerpts (committed source):\n" +
                 upstream_text(self.run, self.checkout, finding["files"], names)[:9000])
        ok, why = PrivacyGate.check(state)
        if not ok:
            return {"verdict": "privacy_skip", "reason": why}
        q = [{"id": "native", "type": "noul", "instructions":
              "The upstream excerpts already implement a generic hook with the same insertion point, "
              "inputs, return contract and off-by-default behaviour as the hook contract."}]
        ans, err = jev_ask(self.run, state, q)
        if ans is None:
            return {"verdict": "jev_unavailable", "reason": err}
        p = ans["native"]["noul"]
        verdict = "native_equivalent" if p >= NATIVE_YES else "no_native" if p <= NATIVE_NO else "unclear"
        return {"verdict": verdict, "noul": p, "state_bytes": len(state.encode())}

    def classify_lane(self, name, spec):
        task = (f"Re-port the generic hook patch {name} onto the current upstream HEAD from its written "
                f"spec; add the spec's named tests; keep off-by-default behaviour byte-identical.")
        rc, out, _ = self.run(["jev", "lane", "classify", "--task", task, "--context",
                               "isolated git worktree, python, tests exist"], timeout=60)
        try:
            j = json.loads(out)
            return {"lane": j["lane"], "model": j["target"]["model"], "effort": j["target"]["effort"],
                    "p": j["decision"]["readings"]["lane"]["confidence"]}
        except (ValueError, KeyError, TypeError):
            return {"lane": "keep_current", "model": None, "effort": None, "error": f"classify_failed:{rc}"}

    def report(self, patch_name, finding, lane, spec):
        stamp = time.strftime("%Y%m%dT%H%M%S")
        wt = self.worktree_root / f"report-{patch_name.removesuffix('.patch')}-{stamp}"
        rc, _, err = self.run(["git", "worktree", "add", "--detach", str(wt), "HEAD"], cwd=self.checkout)
        if rc != 0:
            return {"error": f"worktree_failed:{err[:200]}"}
        prompt = ("Re-port this hook onto the code in the current directory. Implement ONLY what the spec "
                  "describes, including its named tests, as the smallest change. Do not touch config, "
                  "profiles, secrets, or files the spec does not name. Write REPORT-PORT.json: "
                  '{"claims":[{"claim":"...","file":"...","line":N,"symbol":"..."}]} citing the lines '
                  "you changed or relied on.\n\nSPEC\n" + spec)
        ok, why = PrivacyGate.check(prompt)
        if not ok:
            return {"error": f"privacy_skip:{why}", "worktree": str(wt)}
        out = {"worktree": str(wt), "cycles": []}
        base_rc, _, _ = self.run(["bash", self.test_script], cwd=wt, timeout=1200)
        out["state_pristine"] = {"exit": base_rc, "expected": "hook tests fail or are absent"}
        attempt, cur = 1, lane
        for _ in range(self.max_cycles):
            cmd = ["claude", "-p", prompt, "--model", cur["model"], "--effort", cur["effort"],
                   "--allowedTools", "Read,Edit,Write,Bash", "--max-turns", "40",
                   "--output-format", "json", "--add-dir", str(wt)]
            crc, _, _ = self.run(cmd, cwd=wt, timeout=3000)
            t_rc, _, _ = self.run(["bash", self.test_script], cwd=wt, timeout=1200)
            step = self.run(["jev", "lane", "step", "--task", f"re-port {patch_name}", "--lane", cur["lane"],
                             "--attempt", str(attempt), "--run", self.test_script, "--scope", self.scope,
                             "--repo", str(wt)], timeout=1800)
            try:
                action = json.loads(step[1])["decision"]["action"]
            except (ValueError, KeyError, TypeError):
                action = "retry"
            out["cycles"].append({"lane": cur["lane"], "attempt": attempt, "claude_exit": crc,
                                  "tests_exit": t_rc, "step": action})
            if action == "complete" and t_rc == 0:
                break
            if action == "escalate" and cur["lane"] != "escalate":
                nxt = {"small": "medium", "medium": "high", "high": "escalate"}[cur["lane"]]
                cur = {**cur, "lane": nxt, "model": "opus" if nxt != "medium" else "sonnet",
                       "effort": "high" if nxt == "escalate" else "medium"}
                attempt = 0
            attempt += 1
        out["state_ported"] = {"exit": out["cycles"][-1]["tests_exit"] if out["cycles"] else None}
        self.run(["git", "add", "-A", "--", ".", ":(exclude)REPORT-PORT.json"], cwd=wt)
        rc, diff, _ = self.run(["git", "diff", "--cached", "--binary", "HEAD"], cwd=wt)
        cand = self.receipt_dir / f"candidate-{patch_name}"
        cand.write_text(diff)
        out["candidate"] = str(cand)
        out["citations"] = self.citations(wt)
        return out

    def citations(self, wt):
        try:
            claims = json.loads((wt / "REPORT-PORT.json").read_text())["claims"]
        except (OSError, ValueError, KeyError):
            return {"claims": 0, "results": [], "error": "report_missing"}
        results = []
        for c in claims[:12]:
            verdict = self._cite(wt, c, c.get("line"))
            if verdict == "not_enough":                      # re-locate deterministically, ask once more
                rc, out, _ = self.run(["git", "grep", "-n", "-F", c.get("symbol", ""), "--", c.get("file", "")], cwd=wt)
                if rc == 0 and out:
                    verdict = self._cite(wt, c, int(out.splitlines()[0].split(":")[1]), relocated=True)
            results.append({"claim": c.get("claim", "")[:120], "file": c.get("file"), "verdict": verdict})
        return {"claims": len(results), "results": results,
                "supports": sum(r["verdict"] == "supports" for r in results)}

    def _cite(self, wt, c, line, relocated=False):
        try:
            lines = (wt / c["file"]).read_text().splitlines()
            window = "\n".join(f"{i + 1}: {lines[i]}" for i in range(max(0, line - 4), min(len(lines), line + 3)))
        except (OSError, KeyError, TypeError, ValueError):
            return "not_enough"
        state = f"Claim: {c.get('claim', '')}\nEvidence {c.get('file')}:{line}\n{window}"
        if not PrivacyGate.check(state)[0]:
            return "privacy_skip"
        crit = {k: {"not_enough": "Evidence is insufficient", "supports": "Evidence supports the claim",
                    "contradicts": "Evidence contradicts the claim"}[k] for k in CITATION_ORDER}
        ans, _ = jev_ask(self.run, state, [{"id": "cite", "type": "choice",
                                            "instructions": "Does the cited evidence support the claim?",
                                            "criteria": crit}])
        return ans["cite"]["choice"] if ans else "not_enough"


def main(argv=None, run=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--checkout", required=True)
    ap.add_argument("--patches-dir", required=True, help="existing patches dir (never written)")
    ap.add_argument("--jev-dir", required=True, help="dir holding jev-*.patch")
    ap.add_argument("--patch", action="append", help="patch name; default: every registered jev patch present")
    ap.add_argument("--spec", default=str(Path(__file__).resolve().parent.parent / "REPORT-SPEC.md"))
    ap.add_argument("--receipt-dir", default=str(Path.cwd() / "receipts"))
    ap.add_argument("--worktree-root", default=str(Path(tempfile.gettempdir()) / "jev-reports"))
    ap.add_argument("--test-script", help="script file whose exit code is the test result (required with --jev re-port)")
    ap.add_argument("--scope", default="agent/**")
    ap.add_argument("--jev", action="store_true", help="explicitly allow Jev Noul / lane classify / Claude CLI re-port")
    ap.add_argument("--dry-run", action="store_true", help="deterministic checks only; print what --jev would do")
    a = ap.parse_args(argv)
    run = Runner(run) if run else Runner()
    spec = Path(a.spec).read_text() if Path(a.spec).exists() else ""
    names = a.patch or [p.name for p in sorted(Path(a.jev_dir).glob("jev-*.patch")) if not p.name.endswith(".test.patch")]
    h = Helper(run, a.checkout, a.patches_dir, a.spec, a.receipt_dir, a.worktree_root, a.test_script or "", a.scope)
    receipt = {"schema": "conflict-helper-receipt/1", "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "mode": "dry-run" if a.dry_run else "jev" if a.jev else "deterministic", "patches": []}
    rc = 0
    for name in names:
        path = (Path(a.jev_dir) / name).resolve()      # git runs inside the checkout: never pass a relative path
        if not path.is_file():
            receipt["patches"].append({"patch": name, "state": "patch_file_missing", "git_error": [], "files": []})
            rc = 2
            continue
        item = h.inspect(path)
        if item["state"] != "conflict":
            receipt["patches"].append(item)
            continue
        rc = 2
        sec = spec_section(spec, name)
        item["spec_section_found"] = bool(sec)
        if a.dry_run or not a.jev:
            item["would_do_with_jev"] = ["jev ask (Noul: native upstream equivalent)",
                                         "jev lane classify", "claude -p in isolated git worktree",
                                         "two-state tests + jev lane step", "citation Choice checks",
                                         "local receipt"]
            item["next"] = "re-run with --jev after reading the deterministic findings" if not a.dry_run else "dry run"
        elif not sec:
            item["next"] = "no REPORT-SPEC section names this patch; manual re-port"
        elif not a.test_script:
            item["next"] = "--test-script is required for a re-port"
        else:
            native = h.native_equivalent(item, sec)
            item["native"] = native
            if native["verdict"] == "native_equivalent":
                prop = h.receipt_dir / f"retire-proposal-{name}.txt"
                prop.parent.mkdir(parents=True, exist_ok=True)
                prop.write_text(f"PROPOSAL for Big Mac (not executed): {name} may be retirable; upstream HEAD appears to "
                                f"provide an equivalent (Noul {native['noul']}). Verify by hand, then decide. "
                                f"Nothing was removed, replaced or restarted.\n")
                item["next"] = f"retire proposal written: {prop}; Big Mac decides"
            elif native["verdict"] == "no_native":
                lane = h.classify_lane(name, sec)
                item["lane"] = lane
                if lane["lane"] == "keep_current" or not lane.get("model"):
                    item["next"] = "classifier kept current model; re-port by hand"
                else:
                    item["report"] = h.report(name, item, lane, sec)
                    item["next"] = "review the candidate patch and receipt; install by hand if accepted"
            else:
                item["next"] = f"{native['verdict']}: a person decides"
        receipt["patches"].append(item)
    h.receipt_dir.mkdir(parents=True, exist_ok=True)
    out = h.receipt_dir / f"conflict-helper-receipt-{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({"receipt": str(out), "conflicts": sum(p["state"] == "conflict" for p in receipt["patches"])}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
