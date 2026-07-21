"""AgentWorkspace — restricted host-user sandbox for ReAct agent tool execution.

Runs commands via sudo -u hatani. Workspaces live in /home/hatani/workspaces/.
"""

import asyncio
import logging
import os
import pwd
import shutil
from config import (
    AGENT_RUN_TIMEOUT,
    AGENT_WORKSPACE_BASE,
    AGENT_WORKSPACE_FLUSH_INTERVAL,
    STALE_WORKSPACE_TTL,
)
import tempfile
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

_SANDBOX_USER = "hatani"
_RUN_TIMEOUT = AGENT_RUN_TIMEOUT
_WS_BASE = AGENT_WORKSPACE_BASE

# cached at import — fails fast if user is missing
_pw = pwd.getpwnam(_SANDBOX_USER)
_SANDBOX_UID: int = _pw.pw_uid
_SANDBOX_GID: int = _pw.pw_gid


def _chown_to_sandbox(path: str):
    """Recursively chown path to sandbox user so they can read/write."""
    try:
        os.chown(path, _SANDBOX_UID, _SANDBOX_GID)
        for root, dirs, files in os.walk(path):
            for d in dirs:
                os.chown(os.path.join(root, d), _SANDBOX_UID, _SANDBOX_GID)
            for f in files:
                os.chown(os.path.join(root, f), _SANDBOX_UID, _SANDBOX_GID)
    except Exception as e:
        logger.warning(f"chown sandbox failed (non-critical): {e}")


def cleanup_stale_workspaces():
    """Remove agent workspaces older than 24 hours. Call at startup."""
    import time as _t
    if not os.path.isdir(_WS_BASE):
        return
    now = _t.time()
    removed = 0
    for name in os.listdir(_WS_BASE):
        full = os.path.join(_WS_BASE, name)
        if not os.path.isdir(full) or not name.startswith("agent_ws_"):
            continue
        try:
            if now - os.path.getmtime(full) > STALE_WORKSPACE_TTL:
                shutil.rmtree(full, ignore_errors=True)
                removed += 1
        except Exception as e:
            logger.debug(f"cleanup skip {name}: {e}")
    if removed:
        logger.info(f"Cleaned {removed} stale workspace(s)")


class AgentWorkspace:
    """Temp directory in /home/hatani/workspaces, owned by sandbox user."""

    def __init__(self, existing_path: str = ""):
        if existing_path and os.path.isdir(existing_path):
            self.host_path = existing_path
            self._persistent = True
            logger.info(f"Workspace reused: {self.host_path}")
        else:
            os.makedirs(_WS_BASE, exist_ok=True)
            _chown_to_sandbox(_WS_BASE)
            self.host_path = tempfile.mkdtemp(prefix="agent_ws_", dir=_WS_BASE)
            _chown_to_sandbox(self.host_path)
            self._persistent = False
            logger.info(f"Workspace created: {self.host_path}")

    def cleanup(self):
        if not self._persistent:
            shutil.rmtree(self.host_path, ignore_errors=True)

    def preload(self, files: dict[str, bytes]):
        """Pre-populate workspace with files before agent starts."""
        for name, data in files.items():
            self.write(name, data)

    def _safe_path(self, rel_path: str) -> str:
        base = os.path.realpath(self.host_path)
        clean = rel_path.removeprefix("/workspace/").removeprefix("workspace/")
        candidate = os.path.realpath(os.path.join(base, clean))
        if not (candidate == base or candidate.startswith(base + os.sep)):
            raise ValueError(f"Path traversal blocked: {rel_path!r}")
        return candidate

    def write(self, rel_path: str, content: str | bytes):
        full = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode, encoding=None if isinstance(content, bytes) else "utf-8") as f:
            f.write(content)
        try:
            os.chown(full, _SANDBOX_UID, _SANDBOX_GID)
        except Exception:
            pass

    def read(self, rel_path: str) -> str:
        try:
            full = self._safe_path(rel_path)
        except ValueError as exc:
            return f"Access denied: {exc}"
        if not os.path.exists(full) or not os.path.isfile(full):
            return f"File not found: {rel_path}"
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read(16_000)


    async def run_as_user(
        self, cmd: list[str], stdin: str = "",
        output_cb: Optional[Callable] = None,
    ) -> Tuple[str, str, int]:
        """Run cmd as sandbox user with workspace as working directory."""
        full_cmd = [
            "sudo", "-u", _SANDBOX_USER,
            "--preserve-env=PATH,HOME,PLAYWRIGHT_BROWSERS_PATH",
        ] + cmd

        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            cwd=self.host_path,
            stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        out_lines: list[str] = []
        err_lines: list[str] = []

        async def _read_stream(stream, buf: list):
            while True:
                line = await stream.readline()
                if not line:
                    break
                buf.append(line.decode(errors="replace"))

        async def _flush_loop():
            while True:
                await asyncio.sleep(AGENT_WORKSPACE_FLUSH_INTERVAL)
                if output_cb:
                    try:
                        await output_cb("".join(out_lines + err_lines))
                    except Exception:
                        pass

        read_out = asyncio.create_task(_read_stream(proc.stdout, out_lines))
        read_err = asyncio.create_task(_read_stream(proc.stderr, err_lines))
        flush_task = asyncio.create_task(_flush_loop()) if output_cb else None

        try:
            if stdin and proc.stdin:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            await asyncio.wait_for(
                asyncio.gather(read_out, read_err, proc.wait()),
                timeout=_RUN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.gather(read_out, read_err, return_exceptions=True)
            if flush_task:
                flush_task.cancel()
            return (
                "".join(out_lines),
                "".join(err_lines) + f"\nTimeout ({int(_RUN_TIMEOUT)}s)",
                124,
            )
        finally:
            if flush_task:
                flush_task.cancel()

        rc = proc.returncode if proc.returncode is not None else -1
        return "".join(out_lines), "".join(err_lines), rc

