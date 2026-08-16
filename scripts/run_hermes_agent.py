#!/usr/bin/env python3
"""启动本地 Hermes Agent gateway（API Server :8642，供 /hermes 对话委托 /v1/runs）。"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERMES_HOME = ROOT / "data" / "hermes-agent"
LOG_DIR = ROOT / "logs"
LOG_OUT = LOG_DIR / "hermes_agent.log"
LOG_ERR = LOG_DIR / "hermes_agent.err.log"


def _ensure_bootstrap() -> None:
    from scripts.bootstrap_hermes_agent import bootstrap

    bootstrap()


def _hermes_exe() -> str:
    venv_bin = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    candidates = [
        venv_bin / ("hermes.exe" if os.name == "nt" else "hermes"),
        Path(sys.executable).with_name("hermes.exe" if os.name == "nt" else "hermes"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    exe = "hermes.exe" if os.name == "nt" else "hermes"
    return exe




_DEEPSEEK_NO_PROXY = "api.deepseek.com,127.0.0.1,localhost,::1"


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _apply_no_proxy(env: dict[str, str]) -> None:
    extra = (_parse_env_file(ROOT / ".env").get("NO_PROXY") or _DEEPSEEK_NO_PROXY).strip()
    for key in ("NO_PROXY", "no_proxy"):
        cur = (env.get(key) or "").strip()
        if not cur:
            env[key] = extra
            continue
        parts = [p.strip() for p in cur.split(",") if p.strip()]
        for item in [p.strip() for p in extra.split(",") if p.strip()]:
            if item not in parts:
                parts.append(item)
        env[key] = ",".join(parts)

def main() -> None:
    parser = argparse.ArgumentParser(description="Run Hermes Agent gateway (API Server :8642)")
    parser.add_argument("--foreground", action="store_true", help="前台运行（默认后台）")
    args = parser.parse_args()

    _ensure_bootstrap()
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    _apply_no_proxy(env)
    env["HERMES_HOME"] = str(HERMES_HOME)
    cmd = [_hermes_exe(), "gateway", "run", "--accept-hooks"]

    if args.foreground:
        print(f"Hermes Agent gateway: HERMES_HOME={HERMES_HOME}")
        print("API health: http://127.0.0.1:8642/health")
        raise SystemExit(subprocess.call(cmd, cwd=str(ROOT), env=env))

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    with LOG_OUT.open("a", encoding="utf-8") as out, LOG_ERR.open("a", encoding="utf-8") as err:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
        )

    print(f"Hermes Agent gateway started pid={proc.pid}")
    print(f"  HERMES_HOME={HERMES_HOME}")
    print("  API health: http://127.0.0.1:8642/health")
    print(f"  logs: {LOG_OUT} | {LOG_ERR}")


if __name__ == "__main__":
    main()
