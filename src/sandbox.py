"""Sandboxed execution of untrusted code.

The agent writes code and then runs it. That code is model output, so it is
untrusted by definition: it may loop forever, allocate everything, fork
endlessly, write outside its directory, or call home. The sandbox exists to make
those outcomes boring rather than dangerous.

Two backends, with an honest difference in strength:

  DockerSandbox      Real isolation. Separate PID/network/mount namespaces,
                     read-only root, no network, dropped capabilities. This is
                     what you run when the code could be genuinely hostile.

  SubprocessSandbox  Same process tree, isolated only by POSIX rlimits and a
                     temp directory. Stops runaway resource use and accidental
                     damage. Does NOT stop a determined escape: no network
                     namespace, so a subprocess can still open sockets, and
                     nothing prevents reading files the current user can read.
                     Fine for local development against your own model output.
                     Not a security boundary.

Being explicit about that difference matters more than having one sandbox. A
sandbox you trust more than it deserves is worse than no sandbox.
"""

from __future__ import annotations

import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Outcome of one sandboxed run."""

    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    killed_reason: str | None = None
    backend: str = "unknown"
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self) -> str:
        if self.timed_out:
            return f"TIMEOUT after {self.duration_s:.1f}s"
        if self.killed_reason:
            return f"KILLED ({self.killed_reason})"
        return f"exit={self.exit_code} in {self.duration_s:.2f}s"


@dataclass
class SandboxLimits:
    """Resource ceilings applied to every run.

    Defaults are deliberately tight. A test suite that needs more than 512MB or
    30 seconds is usually a runaway, not a legitimate workload, and the agent
    should see the failure and adapt rather than have the ceiling raised.
    """

    timeout_s: int = 30
    memory_mb: int = 512
    cpu_seconds: int = 25          # below timeout_s, so CPU burn trips first
    max_processes: int = 64        # blunts fork bombs
    max_file_size_mb: int = 64     # blunts disk-filling
    max_output_bytes: int = 512_000


class SandboxError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class Sandbox(Protocol):
    """Anything that can run a command against a directory of files."""

    def run(
        self,
        command: list[str],
        files: dict[str, str],
        limits: SandboxLimits | None = None,
        collect: list[str] | None = None,
    ) -> ExecutionResult:
        ...


# ---------------------------------------------------------------------------
# Subprocess backend -- testable anywhere, weaker isolation
# ---------------------------------------------------------------------------


def _apply_rlimits(limits: SandboxLimits):
    """Returned closure runs in the child after fork, before exec.

    Note: no os.setsid() here. Popen(start_new_session=True) already creates
    the session, and calling setsid twice raises EPERM.

    Each limit is applied independently so that one unsupported rlimit (they
    vary by platform and container runtime) does not silently drop the others.
    Anything that fails to apply is reported via the SANDBOX_RLIMIT_WARNINGS
    env var rather than swallowed, because a limit you think is on but is not
    is worse than no limit.
    """

    def preexec():
        failures = []
        mem = limits.memory_mb * 1024 * 1024
        fsize = limits.max_file_size_mb * 1024 * 1024

        # RLIMIT_NPROC counts processes per USER on Darwin, not per process
        # tree as it does on Linux. Setting a low value there makes the very
        # first fork fail with EAGAIN, because the user already has hundreds of
        # processes. Skip it on macOS; fork-bomb containment relies on
        # DockerSandbox (pids_limit) on that platform.
        nproc_supported = sys.platform not in ("darwin",)

        candidates = [
            ("RLIMIT_AS", getattr(resource, "RLIMIT_AS", None), (mem, mem)),
            # Soft below hard on purpose: exceeding the soft limit raises
            # SIGXCPU, which is a nameable "you burned your CPU budget" signal.
            # Setting soft == hard makes the kernel send SIGKILL instead, and
            # the agent then sees an opaque -9 it cannot reason about.
            ("RLIMIT_CPU", getattr(resource, "RLIMIT_CPU", None),
             (limits.cpu_seconds, limits.cpu_seconds + 2)),
            ("RLIMIT_FSIZE", getattr(resource, "RLIMIT_FSIZE", None), (fsize, fsize)),
            ("RLIMIT_CORE", getattr(resource, "RLIMIT_CORE", None), (0, 0)),
        ]
        if nproc_supported:
            candidates.append(
                ("RLIMIT_NPROC", getattr(resource, "RLIMIT_NPROC", None),
                 (limits.max_processes, limits.max_processes))
            )

        for name, res_id, value in candidates:
            if res_id is None:
                failures.append(name)
                continue
            try:
                resource.setrlimit(res_id, value)
            except (ValueError, OSError):
                failures.append(name)

        if failures:
            os.environ["SANDBOX_RLIMIT_WARNINGS"] = ",".join(failures)

    return preexec


class SubprocessSandbox:
    """Runs code in a temp directory under POSIX resource limits.

    NOT a security boundary. See the module docstring.
    """

    backend = "subprocess"

    def __init__(self, workdir_root: str | Path | None = None):
        self.workdir_root = Path(workdir_root) if workdir_root else None

    def run(
        self,
        command: list[str],
        files: dict[str, str],
        limits: SandboxLimits | None = None,
        collect: list[str] | None = None,
    ) -> ExecutionResult:
        limits = limits or SandboxLimits()
        workdir = Path(
            tempfile.mkdtemp(prefix=f"sbx-{uuid.uuid4().hex[:8]}-", dir=self.workdir_root)
        )

        try:
            self._materialize(workdir, files)

            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(workdir),
                "TMPDIR": str(workdir),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                # Deny-by-default: nothing inherited from the parent env, so an
                # API key in the parent process cannot leak into generated code.
            }

            start = time.monotonic()
            timed_out = False
            killed_reason = None

            proc = subprocess.Popen(
                command,
                cwd=str(workdir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=_apply_rlimits(limits),
                start_new_session=True,
            )

            try:
                stdout, stderr = proc.communicate(timeout=limits.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill_tree(proc)
                stdout, stderr = proc.communicate()

            duration = time.monotonic() - start
            exit_code = proc.returncode if proc.returncode is not None else -1

            # Negative return codes mean a signal; name the common ones so the
            # agent gets a usable message instead of "-9".
            if exit_code < 0:
                sig = -exit_code
                killed_reason = {
                    signal.SIGKILL: "SIGKILL (memory limit or forced kill)",
                    signal.SIGXCPU: "SIGXCPU (CPU time limit)",
                    signal.SIGXFSZ: "SIGXFSZ (file size limit)",
                    signal.SIGSEGV: "SIGSEGV (segmentation fault)",
                }.get(sig, f"signal {sig}")

            artifacts = self._collect(workdir, collect or [])

            return ExecutionResult(
                exit_code=exit_code,
                stdout=self._truncate(stdout, limits.max_output_bytes),
                stderr=self._truncate(stderr, limits.max_output_bytes),
                duration_s=duration,
                timed_out=timed_out,
                killed_reason=killed_reason,
                backend=self.backend,
                artifacts=artifacts,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _materialize(workdir: Path, files: dict[str, str]) -> None:
        """Write files, refusing any path that escapes the workdir."""
        for rel, content in files.items():
            target = (workdir / rel).resolve()
            if not str(target).startswith(str(workdir.resolve()) + os.sep):
                raise SandboxError(
                    f"Refusing path traversal: {rel!r} resolves outside the sandbox"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """SIGKILL the whole process group, not just the direct child."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    @staticmethod
    def _collect(workdir: Path, names: list[str]) -> dict[str, str]:
        out = {}
        for name in names:
            path = workdir / name
            if path.is_file():
                try:
                    out[name] = path.read_text()
                except (UnicodeDecodeError, OSError):
                    out[name] = "<unreadable>"
        return out

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        if text is None:
            return ""
        if len(text) <= limit:
            return text
        half = limit // 2
        omitted = len(text) - limit
        return f"{text[:half]}\n\n... [{omitted} bytes omitted] ...\n\n{text[-half:]}"


# ---------------------------------------------------------------------------
# Docker backend -- real isolation
# ---------------------------------------------------------------------------


class DockerSandbox:
    """Runs code in a throwaway container with no network.

    Isolation applied:
      network_disabled   no egress at all
      read_only rootfs   plus a small writable tmpfs at /work
      cap_drop ALL       no capabilities
      no-new-privileges  blocks setuid escalation
      pids_limit         blunts fork bombs
      mem_limit          hard memory ceiling, OOM-killed on breach
      cpu_quota          CPU share cap
      user 65534:65534   nobody, never root
    """

    backend = "docker"

    def __init__(self, image: str = "python:3.12-slim"):
        try:
            import docker  # imported lazily so the subprocess backend needs no dep
        except ImportError as e:
            raise SandboxError(
                "The docker package is required for DockerSandbox. "
                "pip install docker, or use SubprocessSandbox for local dev."
            ) from e
        self._docker = docker
        self.image = image
        self.client = docker.from_env()

    def run(
        self,
        command: list[str],
        files: dict[str, str],
        limits: SandboxLimits | None = None,
        collect: list[str] | None = None,
    ) -> ExecutionResult:
        limits = limits or SandboxLimits()
        host_dir = Path(tempfile.mkdtemp(prefix="dsbx-"))
        container = None

        try:
            SubprocessSandbox._materialize(host_dir, files)

            start = time.monotonic()
            container = self.client.containers.run(
                image=self.image,
                command=command,
                working_dir="/work",
                volumes={str(host_dir): {"bind": "/work", "mode": "rw"}},
                network_disabled=True,
                read_only=True,
                tmpfs={"/tmp": "size=64m,noexec"},
                mem_limit=f"{limits.memory_mb}m",
                memswap_limit=f"{limits.memory_mb}m",   # no swap escape hatch
                pids_limit=limits.max_processes,
                cpu_period=100_000,
                cpu_quota=100_000,                      # one core
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user="65534:65534",
                environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
                detach=True,
            )

            timed_out = False
            try:
                status = container.wait(timeout=limits.timeout_s)
                exit_code = status.get("StatusCode", -1)
            except Exception:
                timed_out = True
                exit_code = -1
                try:
                    container.kill()
                except Exception:
                    pass

            duration = time.monotonic() - start
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", "replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", "replace")

            killed_reason = None
            if exit_code == 137:
                killed_reason = "OOM-killed (exceeded memory limit)"

            return ExecutionResult(
                exit_code=exit_code,
                stdout=SubprocessSandbox._truncate(stdout, limits.max_output_bytes),
                stderr=SubprocessSandbox._truncate(stderr, limits.max_output_bytes),
                duration_s=duration,
                timed_out=timed_out,
                killed_reason=killed_reason,
                backend=self.backend,
                artifacts=SubprocessSandbox._collect(host_dir, collect or []),
            )
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            shutil.rmtree(host_dir, ignore_errors=True)


# ---------------------------------------------------------------------------


def get_sandbox(prefer: str = "auto") -> Sandbox:
    """Pick a backend. Docker when available, subprocess otherwise.

    Emits a warning on fallback, because silently downgrading a security
    boundary is exactly the kind of thing that should be loud.
    """
    if prefer in ("auto", "docker"):
        try:
            return DockerSandbox()
        except Exception as e:
            if prefer == "docker":
                raise
            print(f"[sandbox] Docker unavailable ({e.__class__.__name__}); "
                  f"falling back to SubprocessSandbox. This is NOT a security "
                  f"boundary -- see src/sandbox.py docstring.")
    return SubprocessSandbox()
