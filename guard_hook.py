#!/usr/bin/env python3
"""PreToolUse security hook — the per-client sandbox.

Claude Code runs this before a tool executes, passing a JSON event on stdin.
We enforce the deny-by-default policy:

  mode 'deny-tool'  -> always block (used for Bash / web / Task / KillShell)
  mode 'check-path' -> block any Read/Write/Edit whose target resolves OUTSIDE
                       the client's own working dir (no reading other clients,
                       the .session file, ~/.claude, etc.)

Blocking convention: exit code 2 with a reason on stderr. Stdlib only (Python 3.9+).
"""
import json
import os
import sys


def _block(reason: str):
    sys.stderr.write(reason)
    sys.exit(2)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check-path"

    try:
        data = json.load(sys.stdin)
    except Exception:
        # If we cannot parse the event, fail safe by allowing (Claude Code's own
        # permission system still applies); a malformed event should not wedge work.
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {}) or {}

    if mode == "deny-tool":
        _block(
            "Blocked by sandbox: the '%s' tool is disabled for client sessions. "
            "You have no shell or internet — work only with files in this folder." % tool_name
        )

    # check-path: confine file tools to the client's cwd.
    root = data.get("cwd") or os.getcwd()
    root_real = os.path.realpath(root)

    candidates = []
    for key in ("file_path", "path", "notebook_path"):
        v = tool_input.get(key)
        if v:
            candidates.append(v)

    for raw in candidates:
        target = raw if os.path.isabs(raw) else os.path.join(root_real, raw)
        target_real = os.path.realpath(target)
        if target_real != root_real and not target_real.startswith(root_real + os.sep):
            _block(
                "Blocked by sandbox: access to '%s' is outside your client workspace. "
                "You may only read and write files inside this folder." % raw
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
