"""End-to-end test of the sandbox path, with no model in the loop.

Uses a hand-written brute force / generator / validator so the whole
compile -> stress -> shrink chain can be exercised without an API key. This is
the test that would have caught the tmpfs-ownership bug: a /work that the
unprivileged judge user cannot write to.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from stressagent.runner.docker_runner import Sandbox

FIXTURE = Path(__file__).parent / "fixtures" / "sum_overflow"
DRIVER = Path(__file__).parents[1] / "sandbox" / "driver.py"

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")


async def test_finds_and_shrinks_integer_overflow() -> None:
    async with Sandbox("test-overflow") as sb:
        compiled = await sb.compile_user("cpp", (FIXTURE / "user.cpp").read_text())
        assert compiled.code == 0, compiled.stderr

        for name in ("brute.py", "generator.py", "validator.py"):
            await sb.put_file(name, (FIXTURE / name).read_text())
        await sb.put_file("driver.py", DRIVER.read_text())

        events = []
        async for event in sb.exec_stream(
            [
                "python3", "driver.py",
                "--user-cmd", "./user",
                "--rounds", "200",
                "--size-min", "1",
                "--size-max", "8",
                "--time-budget", "45",
                "--per-run-timeout", "5",
            ],
            timeout=120,
        ):
            events.append(event)

    done = next(e for e in events if e.get("event") == "done")
    assert done["found"] is True, f"stress loop missed a planted bug: {events}"

    # Three values near 10^9 are the fewest that can overflow int32, so a
    # correct shrinker lands on n=3 and cannot do better.
    assert done["size"] == 3, f"expected minimal n=3, got n={done['size']}"
    assert int(done["expected"]) > 2**31 - 1
    assert int(done["actual"]) < 0


async def test_write_and_exec_inside_sandbox() -> None:
    """Guards the two tmpfs flags that are easy to lose in a refactor: without
    `exec` a compiled binary cannot run, without uid/gid /work is unwritable."""
    async with Sandbox("test-perms") as sb:
        await sb.put_file("hello.c", "int main(){return 7;}")
        build = await sb.exec(["gcc", "-o", "hello", "hello.c"], timeout=30)
        assert build.code == 0, build.stderr
        run = await sb.exec(["./hello"], timeout=10)
        assert run.code == 7


async def test_no_network_in_sandbox() -> None:
    async with Sandbox("test-netns") as sb:
        res = await sb.exec(
            ["python3", "-c",
             "import socket;socket.create_connection(('1.1.1.1',53),timeout=3)"],
            timeout=15,
        )
        assert res.code != 0, "sandbox reached the network; --network none is not applied"
