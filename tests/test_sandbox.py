"""Sandbox containment tests.

These are the tests that matter most in this project. The sandbox's entire
purpose is to make model-generated code safe to execute, so each test below is
an attack that must be contained rather than a feature that must work.

Run: pytest tests/test_sandbox.py -q
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.sandbox import (
    ExecutionResult,
    SandboxError,
    SandboxLimits,
    SubprocessSandbox,
)


@pytest.fixture
def sb():
    return SubprocessSandbox()


@pytest.fixture
def limits():
    """Tight limits so tests finish fast."""
    return SandboxLimits(
        timeout_s=8, cpu_seconds=2, memory_mb=128,
        max_file_size_mb=8, max_processes=32,
    )


def run_payload(sb, code: str, limits, **kw) -> ExecutionResult:
    return sb.run(["python3", "payload.py"], {"payload.py": code}, limits, **kw)


# ---------------------------------------------------------------------------
# Containment -- each of these must NOT succeed
# ---------------------------------------------------------------------------


def test_infinite_loop_is_killed(sb, limits):
    result = run_payload(sb, "while True: pass", limits)
    assert not result.ok
    # CPU budget should trip before the wall-clock timeout
    assert result.killed_reason is not None
    assert "CPU" in result.killed_reason or result.timed_out


def test_memory_bomb_is_contained(sb, limits):
    """Containment is the requirement; the mechanism is platform-dependent.

    On Linux RLIMIT_AS caps the address space and the process dies almost
    immediately. macOS does not enforce RLIMIT_AS reliably, so the wall-clock
    timeout is what stops it there. Either is acceptable -- what must never
    happen is the payload running to completion or taking the host down. So this
    asserts the outcome, not which limit fired.
    """
    result = run_payload(sb, "x=[]\nwhile True: x.append(' '*10_000_000)", limits)

    assert not result.ok, "memory bomb was not contained"
    # Bounded either way: RLIMIT_AS raises MemoryError (exit 1) on Linux,
    # the timeout kills it on macOS. Both are fine; running unbounded is not.
    assert result.duration_s < limits.timeout_s + 5


def test_fork_bomb_is_contained(sb, limits):
    result = run_payload(sb, "import os\nwhile True: os.fork()", limits)
    assert not result.ok


def test_oversized_file_write_is_blocked(sb, limits):
    result = run_payload(
        sb, "open('big','wb').write(b'0'*(200*1024*1024))", limits
    )
    assert not result.ok


def test_deep_recursion_does_not_crash_the_host(sb, limits):
    result = run_payload(
        sb,
        "import sys\nsys.setrecursionlimit(10**7)\ndef f(n): return f(n+1)\nf(0)",
        limits,
    )
    assert not result.ok


def test_sleep_beyond_timeout_is_killed(sb):
    tight = SandboxLimits(timeout_s=2, cpu_seconds=10, memory_mb=128)
    result = run_payload(sb, "import time; time.sleep(30)", tight)
    assert result.timed_out
    assert result.duration_s < 6          # killed promptly, not after 30s


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", ["../escape.py", "/etc/evil.py", "a/../../out.py"])
def test_path_traversal_is_refused(sb, limits, bad_path):
    with pytest.raises(SandboxError, match="traversal"):
        sb.run(["true"], {bad_path: "x"}, limits)


def test_parent_environment_does_not_leak(sb, limits, monkeypatch):
    """An API key in the parent process must not be visible to generated code."""
    monkeypatch.setenv("SECRET_API_KEY", "sk-must-not-leak")
    result = run_payload(
        sb,
        "import os\nprint('LEAKED' if 'SECRET_API_KEY' in os.environ else 'clean')",
        limits,
    )
    assert "clean" in result.stdout
    assert "LEAKED" not in result.stdout


def test_workdir_is_removed_after_run(sb, limits):
    result = run_payload(sb, "open('trace.txt','w').write('hi')", limits,
                         collect=["trace.txt"])
    assert result.artifacts["trace.txt"] == "hi"
    # Nothing should survive under the temp root
    leftovers = list(Path("/tmp").glob("sbx-*"))
    assert leftovers == []


def test_runs_do_not_share_state(sb, limits):
    run_payload(sb, "open('marker.txt','w').write('first')", limits)
    result = run_payload(
        sb,
        "import os\nprint('DIRTY' if os.path.exists('marker.txt') else 'clean')",
        limits,
    )
    assert "clean" in result.stdout


# ---------------------------------------------------------------------------
# Normal operation must still work
# ---------------------------------------------------------------------------


def test_benign_code_succeeds(sb, limits):
    result = run_payload(sb, "print('sum =', sum(range(1000)))", limits)
    assert result.ok
    assert "499500" in result.stdout


def test_nonzero_exit_is_reported_not_raised(sb, limits):
    result = run_payload(sb, "import sys; sys.exit(3)", limits)
    assert result.exit_code == 3
    assert not result.ok


def test_stderr_is_captured_separately(sb, limits):
    result = run_payload(sb, "raise ValueError('boom')", limits)
    assert "ValueError" in result.stderr
    assert "boom" in result.stderr
    assert result.stdout == ""


def test_multiple_files_are_materialized(sb, limits):
    files = {
        "main.py": "from helper import double\nprint(double(21))",
        "helper.py": "def double(x): return x * 2",
    }
    result = sb.run(["python3", "main.py"], files, limits)
    assert result.ok
    assert "42" in result.stdout


def test_nested_directories_work(sb, limits):
    files = {
        "run.py": "from pkg.mod import val\nprint(val)",
        "pkg/__init__.py": "",
        "pkg/mod.py": "val = 'nested-ok'",
    }
    result = sb.run(["python3", "run.py"], files, limits)
    assert result.ok
    assert "nested-ok" in result.stdout


def test_output_is_truncated_with_a_marker(sb):
    small = SandboxLimits(timeout_s=8, max_output_bytes=1000)
    result = run_payload(sb, "print('x' * 200_000)", small)
    assert len(result.stdout) < 2000
    assert "bytes omitted" in result.stdout


def test_result_summary_is_human_readable(sb, limits):
    ok = run_payload(sb, "print(1)", limits)
    assert "exit=0" in ok.summary()

    slow = run_payload(sb, "import time; time.sleep(30)",
                       SandboxLimits(timeout_s=2, memory_mb=128))
    assert "TIMEOUT" in slow.summary()
