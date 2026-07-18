"""The worker: resume a client's Claude Code session for exactly one turn.

This is "the wake". Given a client row + their new message, it runs

    claude -p "<wrapped message>"  --session-id|--resume <uuid>  (cwd = client dir)

on the MAX SUBSCRIPTION (ANTHROPIC_API_KEY is stripped from the env so billing can
never silently flip to the metered API). The session is one-turn: the process runs,
Claude does its work + writes any outbox files, then exits. State persists on disk.
"""
import asyncio
import json
import os
from typing import Optional

from templates import build_turn_prompt


async def run_turn(cfg: dict, client_row, inbox_row) -> dict:
    cwd = client_row["cwd"]
    session_id = client_row["session_id"]
    first_turn = (client_row["started"] == 0)

    media_paths = []
    if inbox_row["media"]:
        try:
            media_paths = json.loads(inbox_row["media"])
        except Exception:
            media_paths = []

    prompt = build_turn_prompt(inbox_row["text"] or "", media_paths)

    id_flag = "--session-id" if first_turn else "--resume"
    cmd = [
        cfg.get("claude_bin", "claude"),
        "-p", prompt,
        id_flag, session_id,
        "--output-format", "json",
        "--max-turns", str(cfg.get("max_turns", 40)),
        "--permission-mode", "acceptEdits",
        "--allowedTools", cfg.get("allowed_tools", "Read,Write,Edit,Glob,Grep,TodoWrite"),
        "--disallowedTools", cfg.get("disallowed_tools", "Bash,WebSearch,WebFetch,Task,KillShell"),
    ]
    if cfg.get("model"):
        cmd += ["--model", cfg["model"]]

    # Stay on the subscription: never let an API key leak into the child.
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    out_s = (out or b"").decode("utf-8", "replace").strip()
    err_s = (err or b"").decode("utf-8", "replace").strip()

    if proc.returncode != 0:
        return {
            "ok": False,
            "cost": 0.0,
            "error": "claude exited %s: %s" % (proc.returncode, (err_s or out_s)[:800]),
        }

    parsed = _parse_json_result(out_s)
    if parsed is None:
        return {"ok": False, "cost": 0.0, "error": "could not parse claude output: %s" % out_s[:800]}

    if parsed.get("is_error"):
        return {
            "ok": False,
            "cost": float(parsed.get("total_cost_usd") or 0.0),
            "error": "claude reported is_error: %s" % str(parsed.get("result"))[:800],
        }

    return {
        "ok": True,
        "cost": float(parsed.get("total_cost_usd") or 0.0),
        "result": parsed.get("result", ""),
        "num_turns": parsed.get("num_turns"),
    }


def _parse_json_result(s: str) -> Optional[dict]:
    """--output-format json prints one JSON object. Be defensive about extra lines."""
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        pass
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None
