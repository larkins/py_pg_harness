#!/usr/bin/env python3
"""
Render the default agent's identity files from agent_template/.

The templates in agent_template/ are `*.template` files containing
{{PLACEHOLDER}} markers. The installer renders them into
`pie/<agent>/identity/` and checks the result against the harness's
bootstrap byte caps (the app refuses to boot on an over-cap identity file).

Usage:
  render_identity.py --config install.config.yaml --agent-dir <pie agent dir>
                     [--check-only]

Placeholders:
  {{AGENT_NAME}} {{AGENT_DISPLAY_NAME}} {{AGENT_EMOJI}}
  {{USER_NAME}}  {{USER_EMAIL}}
  {{INSTALL_DIR}} {{DB_NAME}} {{OPERATOR_USER}} {{RUNTIME_ROLE}}
  {{EMBEDDING_MODEL}} {{EMBEDDING_BASE_URL}}
"""
import argparse
import os
import re
import sys

# The harness's bootstrap byte caps (see .env.example). A file over its cap
# is a hard boot failure, so the installer refuses to write one.
CAPS = {
    "SOUL.md": 8000,
    "USER.md": 4000,
    "AGENTS.md": 24000,
    "IDENTITY.md": 8000,
    "MEMORY.md": 65536,
    "HEARTBEAT.md": 65536,
}

TEMPLATE_FILES = [
    "SOUL.md.template",
    "USER.md.template",
    "AGENTS.md.template",
    "IDENTITY.md.template",
    "MEMORY.md.template",
    "HEARTBEAT.md.template",
]


def load_config(path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def build_values(cfg):
    install = cfg.get("install", {}) or {}
    db = cfg.get("database", {}) or {}
    admin = cfg.get("first_admin", {}) or {}
    agent = cfg.get("agent", {}) or {}
    emb = cfg.get("embedding", {}) or {}
    return {
        "AGENT_NAME": agent.get("name", "agent"),
        "AGENT_DISPLAY_NAME": agent.get("display_name", "Agent"),
        "AGENT_EMOJI": agent.get("emoji", "🌳"),
        "USER_NAME": admin.get("display_name", "User"),
        "USER_EMAIL": admin.get("email", "admin@example.com"),
        "INSTALL_DIR": install.get("install_dir", ""),
        "DB_NAME": db.get("name", ""),
        "OPERATOR_USER": install.get("operator_user", ""),
        "RUNTIME_ROLE": db.get("runtime_role", ""),
        "EMBEDDING_MODEL": emb.get("model", ""),
        "EMBEDDING_BASE_URL": emb.get("base_url", ""),
    }


def render(text, values):
    # {{NAME}} -> value; unknown placeholders are left as-is and reported
    unknown = set()

    def sub(m):
        key = m.group(1).strip()
        if key in values:
            return str(values[key])
        unknown.add(key)
        return m.group(0)

    out = re.sub(r"\{\{([A-Z0-9_]+)\}\}", sub, text)
    return out, unknown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--agent-dir", required=True,
                    help="the pie/<agent> directory (identity files go in <agent-dir>/identity)")
    ap.add_argument("--template-dir", default=None)
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    tpl_dir = args.template_dir or os.path.join(repo_root, "agent_template")

    cfg = load_config(args.config)
    values = build_values(cfg)

    identity_dir = os.path.join(args.agent_dir, "identity")
    if not args.check_only:
        os.makedirs(identity_dir, exist_ok=True)

    problems = []
    for tf in TEMPLATE_FILES:
        src = os.path.join(tpl_dir, tf)
        if not os.path.exists(src):
            problems.append(f"missing template: {tf}")
            continue
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        out, unknown = render(text, values)
        if unknown:
            problems.append(f"{tf}: unknown placeholder(s) {sorted(unknown)}")
        name = tf[:-len(".template")]
        cap = CAPS.get(name)
        size = len(out.encode("utf-8"))
        if cap and size > cap:
            problems.append(f"{name}: rendered {size} bytes > cap {cap}")
        else:
            print(f"  {name}: {size} bytes" + (f" (cap {cap})" if cap else ""))
        if not args.check_only:
            dst = os.path.join(identity_dir, name)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(out)

    if problems:
        print("\nPROBLEMS:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"identity files {'checked' if args.check_only else 'rendered'} -> {identity_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
