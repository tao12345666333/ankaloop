#!/usr/bin/env python3
"""Run a live E2E self-test for skill create/discover/trigger in `anka serve` mode."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx


@dataclass
class E2EResult:
    success: bool
    host: str
    port: int
    base_url: str
    work_dir: str
    serve_log: str
    session_create: str | None
    session_trigger: str | None
    skill_name: str
    skill_dir: str
    trigger_token: str
    expected_output: str
    create_response_tail: str
    trigger_response_tail: str
    checks: dict[str, Any]
    error: str | None = None


class SessionEventCollector:
    def __init__(self, base_url: str, session_id: str):
        self.base_url = base_url
        self.session_id = session_id
        self.events: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        url = f"{self.base_url}/api/v1/sessions/{self.session_id}/events"
        try:
            with httpx.stream("GET", url, timeout=None) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if self._stop.is_set():
                        return
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        self.events.append(json.loads(payload))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_server_ready(base_url: str, timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}/api/v1/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("anka serve did not become ready in time")


def create_session(base_url: str, cwd: Path, timeout_seconds: float = 20.0) -> str:
    resp = httpx.post(
        f"{base_url}/api/v1/sessions",
        json={"cwd": str(cwd)},
        timeout=timeout_seconds,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def run_stream_prompt(base_url: str, session_id: str, content: str, timeout_seconds: float) -> str:
    chunks: list[str] = []
    with httpx.stream(
        "POST",
        f"{base_url}/api/v1/sessions/{session_id}/prompt/stream",
        json={"content": content, "stream": True},
        timeout=timeout_seconds,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            item = json.loads(line)
            if item.get("type") == "chunk":
                chunks.append(item.get("content", ""))
            elif item.get("type") == "error":
                raise RuntimeError(f"Prompt stream error: {item.get('error', 'unknown')}" )
    return "".join(chunks)


def has_tool_start(
    events: list[dict[str, Any]],
    tool_name: str,
    arg_match: callable,
) -> bool:
    for event in events:
        if event.get("type") != "tool.call_start":
            continue
        payload = event.get("payload") or {}
        if payload.get("tool_name") != tool_name:
            continue
        arguments = payload.get("arguments") or {}
        if arg_match(arguments):
            return True
    return False


def run_e2e(
    repo_root: Path,
    host: str,
    port: int,
    watcher_wait_seconds: int,
    prompt_timeout_seconds: int,
    keep_artifacts: bool,
) -> E2EResult:
    base_url = f"http://{host}:{port}"
    work_dir = Path(tempfile.mkdtemp(prefix="ankaloop-e2e-serve-skill-"))
    serve_log = work_dir / "serve.log"

    run_id = uuid.uuid4().hex[:10]
    skill_name = f"e2e-autoskill-{run_id}"
    trigger_token = f"E2E_PING_{run_id}"
    expected_output = f"E2E_PONG_{run_id}"
    skill_dir = Path.home() / ".config" / "ankaloop" / "skills" / skill_name
    skill_md = skill_dir / "SKILL.md"
    ping_script = skill_dir / "scripts" / "ping.py"

    if skill_dir.exists():
        shutil.rmtree(skill_dir)

    proc: subprocess.Popen[str] | None = None
    session_create: str | None = None
    session_trigger: str | None = None
    create_response = ""
    trigger_response = ""
    checks: dict[str, Any] = {}

    try:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(repo_root / "src")

        with serve_log.open("w", encoding="utf-8") as log_fp:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "ankaloop.cli",
                    "serve",
                    "--host",
                    host,
                    "--port",
                    str(port),
                    "--work-dir",
                    str(work_dir),
                ],
                cwd=str(repo_root),
                env=env,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                text=True,
            )

        wait_server_ready(base_url)

        session_create = create_session(base_url, work_dir)
        collector_create = SessionEventCollector(base_url, session_create)
        collector_create.start()

        create_prompt = f"""
Create a new AnkaLoop skill via built-in skill-creator workflow.
- Name: {skill_name}
- Location: ~/.config/ankaloop/skills/{skill_name}
- You MUST read skill-creator SKILL.md first.
- Use init_skill.py to scaffold the skill.
- Add scripts/ping.py that prints exactly: {expected_output}
- Configure SKILL.md so this skill is relevant when input contains: {trigger_token}
- In skill instructions, require running: python scripts/ping.py
- Return exactly that script output as final answer.
- Run validate_skill.py and fix issues until it passes.
""".strip()

        create_response = run_stream_prompt(base_url, session_create, create_prompt, prompt_timeout_seconds)
        time.sleep(1)
        collector_create.stop()

        if not skill_md.exists() or not ping_script.exists():
            raise RuntimeError("Skill creation did not produce expected files")

        time.sleep(watcher_wait_seconds)

        session_trigger = create_session(base_url, work_dir)
        collector_trigger = SessionEventCollector(base_url, session_trigger)
        collector_trigger.start()

        trigger_response = run_stream_prompt(
            base_url,
            session_trigger,
            f"{trigger_token}。请按匹配到的 skill 执行并仅返回结果。",
            prompt_timeout_seconds,
        )
        time.sleep(1)
        collector_trigger.stop()

        skill_md_abs = str(skill_md.resolve())
        checks = {
            "created_skill_files": skill_md.exists() and ping_script.exists(),
            "sessionA_read_skill_creator": has_tool_start(
                collector_create.events,
                "read_file",
                lambda args: "skill-creator/SKILL.md" in json.dumps(args, ensure_ascii=False),
            ),
            "sessionB_read_new_skill": has_tool_start(
                collector_trigger.events,
                "read_file",
                lambda args: skill_md_abs in json.dumps(args, ensure_ascii=False),
            ),
            "sessionB_ran_skill_script": has_tool_start(
                collector_trigger.events,
                "bash",
                lambda args: skill_name in json.dumps(args, ensure_ascii=False)
                and "scripts/ping.py" in json.dumps(args, ensure_ascii=False),
            ),
            "sessionB_contains_expected_output": expected_output in trigger_response,
            "sessionA_event_count": len(collector_create.events),
            "sessionB_event_count": len(collector_trigger.events),
        }

        success = all(
            checks[k]
            for k in (
                "created_skill_files",
                "sessionA_read_skill_creator",
                "sessionB_read_new_skill",
                "sessionB_ran_skill_script",
                "sessionB_contains_expected_output",
            )
        )

        return E2EResult(
            success=success,
            host=host,
            port=port,
            base_url=base_url,
            work_dir=str(work_dir),
            serve_log=str(serve_log),
            session_create=session_create,
            session_trigger=session_trigger,
            skill_name=skill_name,
            skill_dir=str(skill_dir),
            trigger_token=trigger_token,
            expected_output=expected_output,
            create_response_tail=create_response[-1200:],
            trigger_response_tail=trigger_response[-1200:],
            checks=checks,
        )

    except Exception as exc:
        return E2EResult(
            success=False,
            host=host,
            port=port,
            base_url=base_url,
            work_dir=str(work_dir),
            serve_log=str(serve_log),
            session_create=session_create,
            session_trigger=session_trigger,
            skill_name=skill_name,
            skill_dir=str(skill_dir),
            trigger_token=trigger_token,
            expected_output=expected_output,
            create_response_tail=create_response[-1200:],
            trigger_response_tail=trigger_response[-1200:],
            checks=checks,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        if not keep_artifacts:
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            if work_dir.exists():
                shutil.rmtree(work_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 means auto-select a free port")
    parser.add_argument("--watcher-wait", type=int, default=10, help="Seconds to wait for SkillWatcher reload")
    parser.add_argument("--prompt-timeout", type=int, default=420)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--output", type=Path, help="Optional path to write JSON result")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    port = args.port or find_free_port()

    result = run_e2e(
        repo_root=repo_root,
        host=args.host,
        port=port,
        watcher_wait_seconds=args.watcher_wait,
        prompt_timeout_seconds=args.prompt_timeout,
        keep_artifacts=args.keep_artifacts,
    )

    output = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    print(output)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")

    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
