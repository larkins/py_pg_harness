#!/usr/bin/env python3
"""
Read install.config.yaml and turn it into the things the installer needs:

  --shell         print shell exports (KEY='value') for install.sh to eval
  --write <dir>   write the generated .env and config.yaml into <dir>
  --summary       print a human summary

The generated .env is built FROM the repo's .env.example, so a new env var
added to the app is picked up automatically; the install config only
overrides the values the operator chose.
"""
import argparse
import json
import os
import re
import shlex
import sys


def load_yaml(path):
    import yaml
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def deep_get(cfg, dotted, default=None):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def build_env(cfg, repo_root, generated):
    """Base = .env.example, overridden by the install config."""
    env = {}
    example = os.path.join(repo_root, ".env.example")
    if os.path.exists(example):
        with open(example, encoding="utf-8") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line or line.lstrip().startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()

    install = cfg.get("install", {}) or {}
    db = cfg.get("database", {}) or {}
    agent = cfg.get("agent", {}) or {}
    llm = cfg.get("llm", {}) or {}
    emb = cfg.get("embedding", {}) or {}
    public = cfg.get("public", {}) or {}
    admin = cfg.get("first_admin", {}) or {}

    operator_role = db.get("operator_role") or "harness_operator"
    runtime_role = db.get("runtime_role") or "harness_app"
    db_name = db.get("name") or "harness"
    db_host = db.get("host") or "localhost"
    db_port = db.get("port") or 5432
    runtime_pw = db.get("runtime_password") or generated["runtime_password"]
    agent_name = agent.get("name") or "agent"

    env["DATABASE_URL"] = (
        f"postgresql://{runtime_role}:{runtime_pw}@{db_host}:{db_port}/{db_name}"
    )
    env["APP_DB_OPERATOR_ROLE"] = operator_role
    env["APP_DB_RUNTIME_ROLE"] = runtime_role
    env["HARNESS_DB"] = db_name
    env["HARNESS_INSTALLATION_DATABASE"] = db_name
    env["HARNESS_DEFAULT_AGENT"] = agent_name
    env["HARNESS_PIE_ROOT"] = os.path.join(install.get("install_dir", "."), "pie")
    env["HARNESS_SECRET_KEY"] = generated["secret_key"]
    env["HARNESS_INTERNAL_TOKEN"] = generated["internal_token"]
    env["HARNESS_BIND"] = public.get("bind") or "127.0.0.1:5010"
    if public.get("tls_cert"):
        env["HARNESS_TLS_CERT"] = public["tls_cert"]
    if public.get("tls_key"):
        env["HARNESS_TLS_KEY"] = public["tls_key"]

    env["LLM_ADAPTER"] = llm.get("adapter") or env.get("LLM_ADAPTER", "minimax_m3")
    env["LLM_BASE_URL"] = llm.get("base_url") or env.get("LLM_BASE_URL", "")
    env["LLM_MODEL"] = llm.get("model") or env.get("LLM_MODEL", "")
    env["LLM_API_KEY"] = llm.get("api_key") or generated.get("llm_api_key", "")
    if llm.get("max_tokens"):
        env["LLM_MAX_TOKENS"] = str(llm["max_tokens"])
    if llm.get("timeout_s"):
        env["LLM_TIMEOUT_S"] = str(llm["timeout_s"])
    if llm.get("temperature") is not None:
        env["LLM_TEMPERATURE"] = str(llm["temperature"])

    env["EMBEDDING_BASE_URL"] = emb.get("base_url") or env.get("EMBEDDING_BASE_URL", "")
    env["EMBEDDING_MODEL"] = emb.get("model") or env.get("EMBEDDING_MODEL", "")
    if emb.get("dimensions"):
        env["EMBEDDING_DIMENSIONS"] = str(emb["dimensions"])
    env["EMBEDDING_API_KEY"] = emb.get("api_key") or generated.get("embedding_api_key", "")
    if emb.get("timeout_s"):
        env["EMBEDDING_TIMEOUT_S"] = str(emb["timeout_s"])

    # Per-agent LLM overrides. Each per_agent block REPLACES the global llm
    # block wholesale, so every field must be present. The vars are named
    # <AGENT>_LLM_<FIELD> to match the config.yaml placeholders.
    per_agent = llm.get("per_agent") or {}
    for agent_name, override in per_agent.items():
        override = override or {}
        prefix = f"{agent_name.upper()}_LLM"
        env[f"{prefix}_ADAPTER"] = override.get("adapter") or env.get("LLM_ADAPTER", "")
        env[f"{prefix}_API_KEY"] = override.get("api_key") or env.get("LLM_API_KEY", "")
        env[f"{prefix}_BASE_URL"] = override.get("base_url") or env.get("LLM_BASE_URL", "")
        env[f"{prefix}_MODEL"] = override.get("model") or env.get("LLM_MODEL", "")
        if override.get("max_tokens") is not None:
            env[f"{prefix}_MAX_TOKENS"] = str(override["max_tokens"])
        elif env.get("LLM_MAX_TOKENS"):
            env[f"{prefix}_MAX_TOKENS"] = env["LLM_MAX_TOKENS"]
        if override.get("timeout_s") is not None:
            env[f"{prefix}_TIMEOUT_S"] = str(override["timeout_s"])
        elif env.get("LLM_TIMEOUT_S"):
            env[f"{prefix}_TIMEOUT_S"] = env["LLM_TIMEOUT_S"]
        if override.get("temperature") is not None:
            env[f"{prefix}_TEMPERATURE"] = str(override["temperature"])
        elif env.get("LLM_TEMPERATURE"):
            env[f"{prefix}_TEMPERATURE"] = env["LLM_TEMPERATURE"]
        env[f"{prefix}_THINKING"] = override.get("thinking", env.get("LLM_THINKING", ""))

    # The admin password is hashed by the create_user CLI, never stored here.
    env["HARNESS_ADMIN_EMAIL"] = admin.get("email", "")
    env["HARNESS_ADMIN_DISPLAY_NAME"] = admin.get("display_name", "")
    env["HARNESS_ADMIN_ROLE"] = admin.get("role", "admin")
    return env


def _per_agent_block(per_agent):
    """The config.yaml text for the llm.per_agent section (or "" to drop it)."""
    if not per_agent:
        return ""
    out = ["  per_agent:\n"]
    for agent_name, override in per_agent.items():
        override = override or {}
        prefix = f"{agent_name.upper()}_LLM"
        out.append(f"    {agent_name}:\n")
        out.append(f"      adapter: ${{{prefix}_ADAPTER}}\n")
        out.append(f"      api_key: ${{{prefix}_API_KEY}}\n")
        out.append(f"      base_url: ${{{prefix}_BASE_URL}}\n")
        out.append(f"      model: ${{{prefix}_MODEL}}\n")
        out.append(f"      max_tokens: ${{{prefix}_MAX_TOKENS}}\n")
        out.append(f"      timeout_s: ${{{prefix}_TIMEOUT_S}}\n")
        out.append(f"      temperature: ${{{prefix}_TEMPERATURE}}\n")
        out.append(f"      thinking: ${{{prefix}_THINKING}}\n")
    return "".join(out)


def _drop_per_agent(text):
    """Remove the llm.per_agent block (and its trailing blank line).

    The block's comment lines are indented deeper than its keys, so the
    block ends at the first non-blank line indented less than 4 spaces.
    """
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^  per_agent:\s*$", line):
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt.strip() == "" or re.match(r"^ {4,}\S", nxt):
                    i += 1
                    continue
                break
            while out and out[-1].strip() == "":
                out.pop()
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def write_config_yaml(path, cfg):
    """Rewrite the agents list AND the per-agent LLM section."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    agent = cfg.get("agent", {}) or {}
    llm = cfg.get("llm", {}) or {}
    name = agent.get("name") or "agent"
    new_list = f"  agents:\n  - {name}\n"
    text, n = re.subn(r"  agents:\n(?:  - \S+\n)+", new_list, text, count=1)
    if n == 0:
        # no agents list yet — insert it after the installation_database line
        text = re.sub(r"(\n  installation_database:.*\n)",
                      r"\1" + new_list, text, count=1)

    per_agent = llm.get("per_agent") or {}
    if per_agent:
        block = _per_agent_block(per_agent)
        if re.search(r"^  per_agent:\s*$", text, re.M):
            text = re.sub(r"^  per_agent:\s*\n(?:(?: {4}.*)?\n)*",
                          block, text, count=1, flags=re.M)
        else:
            text = re.sub(r"(\nllm:\n)", r"\1" + block, text, count=1)
    else:
        text = _drop_per_agent(text)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--shell", action="store_true")
    ap.add_argument("--write", metavar="DIR")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    cfg = load_yaml(args.config)
    if not cfg:
        print(f"ERROR: {args.config} is empty", file=sys.stderr)
        return 2

    # required keys
    missing = []
    for dotted in ("install.operator_user", "install.install_dir", "database.name",
                   "database.operator_role", "database.runtime_role",
                   "first_admin.email", "agent.name"):
        if deep_get(cfg, dotted) in (None, ""):
            missing.append(dotted)
    if missing:
        print(f"ERROR: {args.config} is missing: {', '.join(missing)}", file=sys.stderr)
        return 2

    import secrets as _secrets
    generated = {
        "secret_key": _secrets.token_hex(32),
        "internal_token": _secrets.token_urlsafe(32),
        "runtime_password": _secrets.token_urlsafe(24),
        "llm_api_key": "",
        "embedding_api_key": "",
    }
    # a password supplied in the config wins over the generated one
    supplied = deep_get(cfg, "database.runtime_password")
    if supplied:
        generated["runtime_password"] = supplied

    env = build_env(cfg, args.repo_root, generated)

    if args.shell:
        install = cfg.get("install", {}) or {}
        db = cfg.get("database", {}) or {}
        agent = cfg.get("agent", {}) or {}
        admin = cfg.get("first_admin", {}) or {}
        for k, v in env.items():
            print(f"export {k}={shlex.quote(str(v))}")
        # the install config's own keys, flattened for the shell
        for dotted, val in (
            ("install.install_dir", install.get("install_dir")),
            ("install.operator_user", install.get("operator_user")),
            ("install.repo", install.get("repo")),
            ("database.name", db.get("name")),
            ("database.mode", db.get("mode")),
            ("database.host", db.get("host")),
            ("database.port", db.get("port")),
            ("database.operator_role", db.get("operator_role")),
            ("database.runtime_role", db.get("runtime_role")),
            ("database.operator_password", db.get("operator_password")),
            ("agent.name", agent.get("name")),
            ("agent.display_name", agent.get("display_name")),
            ("agent.emoji", agent.get("emoji")),
            ("first_admin.email", admin.get("email")),
            ("first_admin.display_name", admin.get("display_name")),
            ("first_admin.role", admin.get("role")),
            ("first_admin.password", admin.get("password")),
        ):
            if val is not None:
                print(f"export {dotted.replace('.', '_')}={shlex.quote(str(val))}")
        print(f"export INSTALL_RUNTIME_PASSWORD={shlex.quote(generated['runtime_password'])}")
        return 0

    if args.write:
        target = args.write
        os.makedirs(target, exist_ok=True)
        env_path = os.path.join(target, ".env")
        with open(env_path, "w", encoding="utf-8") as fh:
            for k, v in env.items():
                fh.write(f"{k}={v}\n")
        os.chmod(env_path, 0o600)
        cfg_src = os.path.join(args.repo_root, "config.yaml")
        cfg_dst = os.path.join(target, "config.yaml")
        if os.path.exists(cfg_src):
            if os.path.abspath(cfg_src) != os.path.abspath(cfg_dst):
                import shutil
                shutil.copyfile(cfg_src, cfg_dst)
            # when the install dir IS the repo (a clone-install), the file is
            # rewritten in place
            write_config_yaml(cfg_dst, cfg)
        # the generated secrets are reported once, by the installer
        with open(os.path.join(target, ".install_generated.json"), "w", encoding="utf-8") as fh:
            json.dump(generated, fh)
        os.chmod(os.path.join(target, ".install_generated.json"), 0o600)
        print(f"wrote {env_path} and {cfg_dst}")
        return 0

    if args.summary:
        print("install   :", deep_get(cfg, "install.install_dir"), "as",
              deep_get(cfg, "install.operator_user"))
        print("database  :", deep_get(cfg, "database.mode"), deep_get(cfg, "database.name"),
              "operator:", deep_get(cfg, "database.operator_role"),
              "runtime:", deep_get(cfg, "database.runtime_role"))
        print("agent     :", deep_get(cfg, "agent.name"))
        print("admin     :", deep_get(cfg, "first_admin.email"))
        return 0

    print(json.dumps({"env": env, "generated": generated}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
