"""Sandbox execution: one warm container per submission.

Everything here shells out to the docker CLI rather than the SDK -- it keeps the
dependency surface small and the commands are greppable in `docker events` when
something goes wrong in production.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..config import settings

# Must match the `judge` user pinned in sandbox/Dockerfile: the /work tmpfs is
# created by the daemon, not the image, so it needs the numeric ids explicitly.
JUDGE_UID = 1000
JUDGE_GID = 1000

_slots: asyncio.Semaphore | None = None


def sandbox_slots() -> asyncio.Semaphore:
    """Global cap on concurrent containers. CPU is the bottleneck here, not the
    model API, so this is the limit that actually protects the box."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(settings().global_sandbox_slots)
    return _slots


@dataclass
class LanguageSpec:
    filename: str
    compile_cmd: list[str] | None
    run_cmd: list[str]


LANGUAGES: dict[str, LanguageSpec] = {
    "cpp": LanguageSpec(
        filename="user.cpp",
        compile_cmd=["g++", "-O2", "-std=c++17", "-o", "user", "user.cpp"],
        run_cmd=["./user"],
    ),
    "python": LanguageSpec(filename="user.py", compile_cmd=None, run_cmd=["python3", "user.py"]),
    "java": LanguageSpec(
        filename="Main.java",
        compile_cmd=["javac", "-J-Xmx256m", "Main.java"],
        run_cmd=["java", "-Xss64m", "-XX:+UseSerialGC", "-cp", ".", "Main"],
    ),
}


@dataclass
class ExecResult:
    stdout: str
    stderr: str
    code: int
    timed_out: bool


class SandboxError(RuntimeError):
    pass


class Sandbox:
    """A single container, alive for the duration of one submission."""

    def __init__(self, submission_id: str) -> None:
        self.name = f"stress-{submission_id[:8]}-{uuid.uuid4().hex[:6]}"
        self._started = False

    async def __aenter__(self) -> Sandbox:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> None:
        s = settings()
        args = [
            "docker", "run", "-d",
            "--name", self.name,
            # No network at all: the submission cannot exfiltrate the problem
            # set, and cannot reach the model API to poison its own report.
            "--network", "none",
            "--cpus", str(s.sandbox_cpus),
            "--memory", s.sandbox_memory,
            # Without an explicit swap cap equal to memory, the container can
            # swap past its limit and the memory cap becomes decorative.
            "--memory-swap", s.sandbox_memory,
            "--pids-limit", str(s.sandbox_pids),
            "--read-only",
            # Two non-obvious flags here, both discovered the hard way:
            #   exec  -- docker mounts tmpfs noexec by default, so a compiled
            #            binary in /work dies with a bare "Permission denied".
            #   uid/gid/mode -- a tmpfs comes up root:root 0755, which the
            #            unprivileged `judge` user cannot write to at all.
            "--tmpfs", f"/work:rw,exec,size=128m,uid={JUDGE_UID},gid={JUDGE_GID},mode=0700",
            "--tmpfs", "/tmp:rw,exec,size=64m,mode=1777",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            s.sandbox_image,
        ]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise SandboxError(f"container start failed: {err.decode(errors='replace')}")
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", self.name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        self._started = False

    # ------------------------------------------------------------------ files

    async def put_file(self, name: str, content: str) -> None:
        """Write a file into /work via stdin.

        Piping through `cat` avoids building a tar stream for `docker cp`, and
        keeps the content off the process argv (where it would show up in `ps`
        and blow past ARG_MAX for a large source file).
        """
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", self.name, "sh", "-c", f"cat > /work/{shlex.quote(name)}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate(content.encode())
        if proc.returncode != 0:
            raise SandboxError(f"put_file {name}: {err.decode(errors='replace')}")

    # -------------------------------------------------------------- execution

    async def exec(
        self, cmd: list[str], stdin_data: str = "", timeout: float | None = None
    ) -> ExecResult:
        timeout = timeout or settings().sandbox_per_run_timeout
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "-w", "/work", self.name, *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin_data.encode()), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ExecResult("", "timeout", -9, True)
        return ExecResult(
            out.decode(errors="replace"),
            err.decode(errors="replace")[-8000:],
            proc.returncode or 0,
            False,
        )

    async def exec_stream(self, cmd: list[str], timeout: float) -> AsyncIterator[dict]:
        """Run a command and yield its stdout as parsed JSON Lines.

        Used for the stress driver so progress reaches the client while the loop
        is still running, instead of arriving in one lump at the end.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-i", "-w", "/work", self.name, *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None

        async def _pump() -> AsyncIterator[dict]:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    yield json.loads(text)
                except json.JSONDecodeError:
                    # Driver crashed mid-write, or the program under test printed
                    # to our stdout. Neither is fatal; keep reading.
                    continue

        try:
            agen = _pump()
            while True:
                try:
                    event = await asyncio.wait_for(agen.__anext__(), timeout)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    yield {"event": "done", "found": False, "reason": "driver_timeout"}
                    break
                yield event
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    # ----------------------------------------------------------------- helpers

    async def compile_user(self, language: str, source: str) -> ExecResult:
        spec = LANGUAGES[language]
        await self.put_file(spec.filename, source)
        if spec.compile_cmd is None:
            # Python still needs a syntax gate, otherwise a SyntaxError shows up
            # 400 rounds later as a fake "runtime error" counterexample.
            return await self.exec(["python3", "-m", "py_compile", spec.filename], timeout=20)
        return await self.exec(spec.compile_cmd, timeout=30)
