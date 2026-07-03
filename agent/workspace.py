"""AgentWorkspace — isolated Docker sandbox for ReAct agent tool execution."""

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

_DOCKER_TIMEOUT = 600.0
_SANDBOX_IMAGE  = "hatani-sandbox:latest"


class AgentWorkspace:
    """Temp directory on host, bind-mounted into Docker for isolated execution."""

    def __init__(self, existing_path: str = ""):
        if existing_path and os.path.isdir(existing_path):
            self.host_path = existing_path
            self._persistent = True
            logger.info(f"Workspace reused: {self.host_path}")
        else:
            project_dir = os.path.dirname(os.path.abspath(__file__))
            ws_dir = os.path.join(project_dir, ".agent_workspaces")
            os.makedirs(ws_dir, exist_ok=True)
            try:
                os.chown(ws_dir, 1000, 1000)
                os.chmod(ws_dir, 0o777)
            except Exception:
                pass
            self.host_path = tempfile.mkdtemp(prefix="agent_ws_", dir=ws_dir)
            try:
                os.chown(self.host_path, 1000, 1000)
                os.chmod(self.host_path, 0o777)
            except Exception:
                pass
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
        candidate = os.path.realpath(os.path.join(base, rel_path.lstrip("/")))
        if not (candidate == base or candidate.startswith(base + os.sep)):
            raise ValueError(f"Path traversal blocked: {rel_path!r}")
        return candidate

    def write(self, rel_path: str, content: str | bytes):
        full = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode, encoding=None if isinstance(content, bytes) else "utf-8") as f:
            f.write(content)

    def read(self, rel_path: str) -> str:
        try:
            full = self._safe_path(rel_path)
        except ValueError as exc:
            return f"Access denied: {exc}"
        if not os.path.exists(full):
            return f"File not found: {rel_path}"
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read(16_000)

    def list_files(self) -> list[str]:
        result = []
        for root, dirs, files in os.walk(self.host_path):
            for fname in files:
                rel = os.path.relpath(os.path.join(root, fname), self.host_path)
                result.append(rel)
        return result[:50]

    async def docker_run(
        self, cmd: list[str], stdin: str = "",
        output_cb: Optional[Callable] = None,
    ) -> Tuple[str, str, int]:
        """Run cmd inside sandbox container with workspace mounted."""
        def _fix_workspace_permissions():
            try:
                os.chown(self.host_path, 1000, 1000)
                os.chmod(self.host_path, 0o777)
                for root, dirs, files in os.walk(self.host_path):
                    for d in dirs:
                        path = os.path.join(root, d)
                        os.chown(path, 1000, 1000)
                        os.chmod(path, 0o777)
                    for f in files:
                        path = os.path.join(root, f)
                        os.chown(path, 1000, 1000)
                        os.chmod(path, 0o777)
            except Exception as e:
                logger.warning(f"Failed to fix workspace permissions recursive: {e}")

        await asyncio.to_thread(_fix_workspace_permissions)

        docker_cmd = [
            "docker", "run", "--rm",
            "--memory=1024m", "--cpus=2",
            "--user=sandbox",
            "--workdir=/workspace",
            "-v", f"{self.host_path}:/workspace",
            _SANDBOX_IMAGE,
        ] + cmd

        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
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
            import time as _t
            last = _t.monotonic()
            while True:
                await asyncio.sleep(2)
                if output_cb:
                    try:
                        await output_cb("".join(out_lines + err_lines))
                    except Exception:
                        pass

        read_out = asyncio.create_task(_read_stream(proc.stdout, out_lines))
        read_err = asyncio.create_task(_read_stream(proc.stderr, err_lines))
        flush_task = asyncio.create_task(_flush_loop()) if output_cb else None

        try:
            if stdin:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            await asyncio.wait_for(
                asyncio.gather(read_out, read_err, proc.wait()),
                timeout=_DOCKER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await asyncio.gather(read_out, read_err, return_exceptions=True)
            if flush_task:
                flush_task.cancel()
            return "".join(out_lines), "".join(err_lines) + f"\nTimeout ({int(_DOCKER_TIMEOUT)}s)", 124
        finally:
            if flush_task:
                flush_task.cancel()

        return "".join(out_lines), "".join(err_lines), proc.returncode
