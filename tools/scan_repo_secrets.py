#!/usr/bin/env python3
"""
Scan git objects / working trees for literal secret values.

Shipped with the harness so that any install can check its own repo for
leaked credentials — the same check the release gate uses.

Design rules (AGENTS.md):
  * NEVER print a secret value. Only labels, counts, blob SHAs, paths.
  * Never pass a secret on a command line (visible in `ps`) -> secrets are
    matched in-process against bytes streamed from `git cat-file`.
  * A failed read is NOT evidence of absence: if an object cannot be read we
    report it as "unreadable", never as "clean".

Usage:
  scan_repo_secrets.py --repo <git dir> [--env <file> ...] [--json out.json]
  scan_repo_secrets.py --tree <dir>   [--env <file> ...] [--json out.json]
"""
import argparse
import json
import os
import re
import subprocess
import sys

SECRET_NAME_RE = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|CREDENTIAL|PRIVATE_KEY|_KEY$|_API$|API_)", re.I
)
# names that are structural, not secret material
NOT_SECRET_RE = re.compile(
    r"(PUBLIC_KEY|KEY_ID|KEYSTORE|KEYRING|KEY_FILE|KEYFILE|_KEY_PATH|SECRET_KEY_FILE)", re.I
)
PLACEHOLDER_RE = re.compile(
    r"^(changeme|change-me|example|examples?|your[_-].*|xxx+|\$\{.*\}|<.*>|TODO.*|"
    r"placeholder|none|null|true|false|localhost|127\.0\.0\.1|0\.0\.0\.0|"
    r"postgres|peristyle|accounts|public\.users|accounts_app)$",
    re.I,
)
MIN_LEN = 8
URL_RE = re.compile(r"^([a-z][a-z0-9+.\-]*://[^:/@]+):([^@]+)@(.+)$", re.I)


def parse_env(path):
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if v:
                out[k] = v
    return out


def parse_yaml(path):
    """Flat dotted-path parse of a simple YAML file (no external deps)."""
    out = {}
    stack = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue
            m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
            if not m:
                continue
            indent, key, val = len(m.group(1)), m.group(2), m.group(3).strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            val = val.split("  #")[0].strip().strip('"').strip("'")
            if val and not val.startswith(("&", "*", "{", "[")):
                out[".".join(k for _, k in stack)] = val
    return out


def collect_secrets(env_files):
    """-> list of (label, value_bytes). Value never printed."""
    found = []
    for path in env_files:
        data = {}
        if path.endswith((".yaml", ".yml")):
            data = parse_yaml(path)
        else:
            data = parse_env(path)
        for k, v in data.items():
            leaf = k.split(".")[-1]
            # any value that is a URL with embedded credentials carries a secret,
            # whatever the variable is called
            m = URL_RE.match(v)
            if m and len(m.group(2)) >= MIN_LEN and not PLACEHOLDER_RE.match(m.group(2)):
                found.append((f"{os.path.basename(path)}:{k}<url-password>", m.group(2).encode()))
            # a plain URL is a location, not a credential (caught 2026-10-24:
            # MAIL_SERVER_API=http://127.0.0.1:5003 was reported as a secret
            # because its variable name ends in _API)
            if re.match(r"^[a-z][a-z0-9+.\-]*://", v, re.I) and not m:
                continue
            if NOT_SECRET_RE.search(k):
                continue
            if not SECRET_NAME_RE.search(leaf):
                continue
            if len(v) < MIN_LEN or PLACEHOLDER_RE.match(v):
                continue
            found.append((f"{os.path.basename(path)}:{k}", v.encode()))
    # de-duplicate identical values, keep first label
    seen, uniq = set(), []
    for label, val in found:
        if val in seen:
            continue
        seen.add(val)
        uniq.append((label, val))
    return uniq


def git(repo, *args, binary=False):
    p = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, check=False
    )
    if p.returncode != 0:
        return None if binary else p.stderr.decode("utf-8", "replace")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def scan_repo(repo, secrets):
    result = {
        "repo": repo,
        "mode": "objects",
        "objects_scanned": 0,
        "objects_unreadable": 0,
        "secrets_evaluated": len(secrets),
        "findings": {},
        "commit_hits": {},
        "path_hits": {},
    }
    if not os.path.isdir(repo):
        result["error"] = "repo path not found"
        return result

    # 1. reachable blob -> path
    path_of = {}
    rev = git(repo, "rev-list", "--all", "--objects")
    if rev is None:
        result["error"] = "rev-list failed (not a git repo?)"
        return result
    for line in rev.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            path_of[parts[0]] = parts[1]

    # 2. stream EVERY object (reachable or not) and match secret bytes
    proc = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch-all-objects", "--batch"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    hits = {}  # label -> {blob_sha: [paths]}
    sha = None
    otype = None
    try:
        while True:
            header = proc.stdout.readline()
            if not header:
                break
            parts = header.split()
            if len(parts) < 3:
                result["objects_unreadable"] += 1
                continue
            sha, otype, size = parts[0].decode(), parts[1].decode(), int(parts[2])
            data = proc.stdout.read(size)
            proc.stdout.read(1)  # trailing newline
            result["objects_scanned"] += 1
            if otype not in ("blob", "commit", "tag", "tree"):
                continue
            for label, val in secrets:
                if val in data:
                    hits.setdefault(label, {})
                    hits[label].setdefault(sha, set()).add(path_of.get(sha, "<unreachable>"))
    finally:
        proc.stdout.close()
        proc.wait()

    for label, blobs in hits.items():
        result["findings"][label] = {
            "object_count": len(blobs),
            "object_shas": sorted(blobs.keys()),
            "paths": sorted({p for ps in blobs.values() for p in ps}),
        }

    # 3. which commits carry them
    if hits:
        secret_blobs = {sha for blobs in hits.values() for sha in blobs}
        commits = git(repo, "rev-list", "--all")
        for c in (commits or "").split():
            ls = git(repo, "ls-tree", "-r", c)
            if not ls:
                continue
            for line in ls.splitlines():
                meta, _, path = line.partition("\t")
                bits = meta.split()
                if len(bits) >= 3 and bits[2] in secret_blobs:
                    for label, blobs in hits.items():
                        if bits[2] in blobs:
                            result["commit_hits"].setdefault(label, []).append(c[:12])
    for label, cs in result["commit_hits"].items():
        result["commit_hits"][label] = sorted(set(cs))
    return result


def scan_tree(tree, secrets):
    result = {
        "repo": tree,
        "mode": "tree",
        "files_scanned": 0,
        "files_unreadable": 0,
        "secrets_evaluated": len(secrets),
        "findings": {},
    }
    for root, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "node_modules")]
        for f in files:
            p = os.path.join(root, f)
            try:
                if os.path.getsize(p) > 20_000_000:
                    continue
                data = open(p, "rb").read()
            except Exception:
                result["files_unreadable"] += 1
                continue
            result["files_scanned"] += 1
            for label, val in secrets:
                if val in data:
                    result["findings"].setdefault(label, []).append(os.path.relpath(p, tree))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    ap.add_argument("--tree")
    ap.add_argument("--env", action="append", required=True)
    ap.add_argument("--json")
    args = ap.parse_args()

    secrets = collect_secrets(args.env)
    if args.repo:
        res = scan_repo(args.repo, secrets)
    else:
        res = scan_tree(args.tree, secrets)
    res["labels_evaluated"] = sorted(label for label, _ in secrets)

    lines = []
    lines.append(f"repo: {res['repo']}  mode={res['mode']}")
    if "error" in res:
        lines.append(f"ERROR: {res['error']}")
        print("\n".join(lines))
        sys.exit(2)
    if res["mode"] == "objects":
        lines.append(
            f"objects scanned: {res['objects_scanned']}  unreadable: {res['objects_unreadable']}"
        )
    else:
        lines.append(
            f"files scanned: {res['files_scanned']}  unreadable: {res['files_unreadable']}"
        )
    lines.append(
        f"secret labels evaluated: {res['secrets_evaluated']} "
        f"({', '.join(sorted(res.get('labels_evaluated', [])))})"
    )
    scanned = res.get("objects_scanned", res.get("files_scanned", 0))
    if scanned == 0:
        lines.append(
            "RESULT: INCONCLUSIVE — nothing could be read (a failed read is NOT "
            "evidence of absence). Re-run as the owning user."
        )
        print("\n".join(lines))
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(res, fh, indent=2)
        sys.exit(2)
    if not res["findings"]:
        lines.append("RESULT: CLEAN — no secret values found")
    else:
        lines.append("RESULT: SECRETS FOUND")
        for label, info in sorted(res["findings"].items()):
            if res["mode"] == "objects":
                lines.append(
                    f"  [{label}] objects={info['object_count']} paths={','.join(info['paths']) or '-'}"
                )
                if label in res.get("commit_hits", {}):
                    lines.append(
                        f"      commits({len(res['commit_hits'][label])}): "
                        + ",".join(res["commit_hits"][label][:12])
                    )
            else:
                lines.append(f"  [{label}] files={','.join(info)}")
    print("\n".join(lines))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
    sys.exit(1 if res["findings"] else 0)


if __name__ == "__main__":
    main()
