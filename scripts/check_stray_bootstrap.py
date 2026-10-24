#!/usr/bin/python3
"""Guard against a second stray bootstrap file at pie/<agent>/ root.

Background (2026-10-20): pie/agent2/AGENTS.md existed at the agent root while the
harness actually loads pie/<agent>/identity/AGENTS.md. The stray was untracked,
never injected, and held a divergent draft of the coding-task rules — so an agent
reading the filesystem believed it had instructions the loaded file did not
contain (or vice versa).

bootstrap.py's _cold guard rejects identity/_cold/, but nothing rejected a plain
.md sitting at pie/<agent>/ root. This reports them loudly. Not fatal: a stray is
a mistake, not a reason to refuse to serve.
"""
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "pie"

# The four files the harness injects per agent (config.bootstrap.files).
BOOTSTRAP_NAMES = ("SOUL.md", "USER.md", "AGENTS.md", "IDENTITY.md")


def find_strays():
    strays = []
    if not ROOT.is_dir():
        return strays
    for agent_dir in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        if not (agent_dir / "identity").is_dir():
            continue  # not a bootstrapped agent
        for name in BOOTSTRAP_NAMES:
            stray = agent_dir / name
            if stray.is_file():
                strays.append((agent_dir.name, stray))
    return strays


def main() -> int:
    strays = find_strays()
    if not strays:
        print("bootstrap guard: clean — no stray bootstrap files at pie/<agent>/ root")
        return 0

    print("")
    print("=" * 74)
    print("bootstrap guard: WARNING — %d stray bootstrap file(s) found." % len(strays))
    print("These are NOT loaded. The harness injects pie/<agent>/identity/<name>")
    print("only (app/extensions.py sets workspace_root=pie_root/<agent>/identity).")
    print("")
    for agent, path in strays:
        st = path.stat()
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        print("  pie/%s/%s  (%d bytes, mtime %s)" % (agent, path.name, st.st_size, mtime))
        print("      -> move the content into pie/%s/identity/%s, then delete the stray"
              % (agent, path.name))
    print("")
    print("If the content is already in identity/, just delete the stray.")
    print("=" * 74)
    print("")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
