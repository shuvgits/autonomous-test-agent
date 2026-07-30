"""Running tests and measuring coverage inside the sandbox.

The agent needs two numbers from every attempt:

  did the tests pass      -> whether to keep iterating
  what coverage did we get -> whether the attempt was worth anything

Both come from executing pytest under coverage inside the sandbox and parsing
structured output. Nothing here trusts the model's claim about its own work;
the only source of truth is what actually ran.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import sys

from .sandbox import ExecutionResult, Sandbox, SandboxLimits

RUNNER = r'''
import json, subprocess, sys

# Run the suite under coverage, scoped to the module under test only, so
# coverage of the test file itself does not inflate the number.
proc = subprocess.run(
    [sys.executable, "-m", "coverage", "run", "--source", "{source}",
     "-m", "pytest", "{test_file}", "-q", "--no-header", "-p", "no:cacheprovider"],
    capture_output=True, text=True,
)

report = subprocess.run(
    [sys.executable, "-m", "coverage", "json", "-o", "-"],
    capture_output=True, text=True,
)

coverage_data = None
if report.returncode == 0 and report.stdout.strip():
    try:
        coverage_data = json.loads(report.stdout)
    except json.JSONDecodeError:
        pass

print("---RESULT-JSON---")
print(json.dumps({{
    "pytest_returncode": proc.returncode,
    "pytest_stdout": proc.stdout[-8000:],
    "pytest_stderr": proc.stderr[-4000:],
    "coverage": coverage_data,
}}))
'''


@dataclass
class TestRunResult:
    """What one attempt actually achieved."""

    __test__ = False

    passed: bool
    tests_collected: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    tests_errored: int = 0
    coverage_percent: float | None = None
    missing_lines: list[int] = field(default_factory=list)
    covered_lines: int = 0
    total_lines: int = 0
    failure_output: str = ""
    collection_error: bool = False
    timed_out: bool = False
    raw: ExecutionResult | None = None

    @property
    def is_usable(self) -> bool:
        """A run only counts if the tests actually pass."""
        return self.passed and self.tests_collected > 0

    def feedback(self) -> str:
        """The message handed back to the model on the next iteration.

        Deliberately terse and concrete. Long transcripts of pytest output
        push the model toward rewriting everything instead of fixing the one
        thing that broke.
        """
        if self.timed_out:
            return (
                "The test run timed out, so nothing was measured. A test is "
                "hanging: an infinite loop, a blocking call, or an unbounded "
                "input. Remove or bound the offending test."
            )
        if self.collection_error:
            return (
                "The test file could not be collected, so no test ran. This is a "
                "syntax or import error, not a test failure. Fix it before "
                f"adding cases:\n\n{self.failure_output[:2000]}"
            )
        if self.passed:
            missing = f", uncovered lines: {self.missing_lines}" if self.missing_lines else ""
            return (
                f"All {self.tests_passed} tests passed. "
                f"Coverage {self.coverage_percent:.1f}%{missing}"
            )
        return (
            f"{self.tests_failed} failed, {self.tests_passed} passed. "
            f"Fix only the failing tests; leave the passing ones alone.\n\n"
            f"{self.failure_output[:3000]}"
        )


class TestRunner:
    # Not a pytest test class, despite the name.
    __test__ = False

    def __init__(self, sandbox: Sandbox, limits: SandboxLimits | None = None):
        self.sandbox = sandbox
        self.limits = limits or SandboxLimits(timeout_s=60, cpu_seconds=50, memory_mb=512)

    def run(
        self,
        module_name: str,
        module_source: str,
        test_source: str,
        extra_files: dict[str, str] | None = None,
    ) -> TestRunResult:
        """Execute `test_source` against `module_source` and measure coverage."""
        module_file = f"{module_name}.py"
        test_file = f"test_{module_name}.py"

        files = {
            module_file: module_source,
            test_file: test_source,
            "_runner.py": RUNNER.format(source=module_name, test_file=test_file),
        }
        if extra_files:
            files.update(extra_files)

        # sys.executable, not "python3": PATH lookup can resolve to a system
        # interpreter that lacks pytest and coverage. This guarantees the same
        # interpreter that is running the agent.
        exec_result = self.sandbox.run(
            [sys.executable, "_runner.py"], files, self.limits
        )
        return self._parse(exec_result, module_file)

    # -- parsing -----------------------------------------------------------

    def _parse(self, exec_result: ExecutionResult, module_file: str) -> TestRunResult:
        if exec_result.timed_out:
            return TestRunResult(
                passed=False,
                timed_out=True,
                failure_output="Sandbox timeout; no pytest output was produced.",
                raw=exec_result,
            )

        payload = self._extract_json(exec_result.stdout)
        if payload is None:
            return TestRunResult(
                passed=False,
                collection_error=True,
                failure_output=(
                    (exec_result.stderr or exec_result.stdout or "no output")[:4000]
                ),
                raw=exec_result,
            )

        pytest_out = payload.get("pytest_stdout", "") or ""
        pytest_err = payload.get("pytest_stderr", "") or ""
        returncode = payload.get("pytest_returncode", 1)

        counts = self._parse_counts(pytest_out)

        # pytest exit codes: 0 ok, 1 tests failed, 2 interrupted/usage,
        # 3 internal error, 4 usage error, 5 no tests collected.
        # Anything other than 0 or 1 means the suite never really ran, and a
        # syntax error in the test file lands here rather than as a failure.
        no_tests_ran = counts["collected"] == 0
        collection_error = (
            returncode not in (0, 1)
            or (no_tests_ran and bool(pytest_out.strip() or pytest_err.strip()))
            or "SyntaxError" in pytest_out
            or "ImportError" in pytest_out
            or "collection error" in pytest_out.lower()
        )

        cov_pct, missing, covered, total = self._parse_coverage(
            payload.get("coverage"), module_file
        )

        return TestRunResult(
            passed=(returncode == 0 and counts["collected"] > 0),
            tests_collected=counts["collected"],
            tests_passed=counts["passed"],
            tests_failed=counts["failed"],
            tests_errored=counts["errors"],
            coverage_percent=cov_pct,
            missing_lines=missing,
            covered_lines=covered,
            total_lines=total,
            failure_output=(pytest_out + "\n" + pytest_err).strip(),
            collection_error=collection_error,
            raw=exec_result,
        )

    @staticmethod
    def _extract_json(stdout: str) -> dict | None:
        marker = "---RESULT-JSON---"
        if marker not in stdout:
            return None
        tail = stdout.split(marker, 1)[1].strip()
        try:
            return json.loads(tail)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _parse_counts(pytest_output: str) -> dict[str, int]:
        """Read the pytest summary line, e.g. '3 failed, 7 passed in 0.42s'."""
        counts = {"passed": 0, "failed": 0, "errors": 0, "collected": 0}
        for key, pattern in (
            ("passed", r"(\d+) passed"),
            ("failed", r"(\d+) failed"),
            ("errors", r"(\d+) error"),
        ):
            m = re.search(pattern, pytest_output)
            if m:
                counts[key] = int(m.group(1))
        counts["collected"] = counts["passed"] + counts["failed"] + counts["errors"]
        return counts

    @staticmethod
    def _parse_coverage(
        coverage_json: dict | None, module_file: str
    ) -> tuple[float | None, list[int], int, int]:
        if not coverage_json:
            return None, [], 0, 0

        files = coverage_json.get("files", {})
        # coverage keys the file by whatever path it saw; match on basename.
        entry = None
        for path, data in files.items():
            if path.endswith(module_file):
                entry = data
                break
        if entry is None:
            totals = coverage_json.get("totals", {})
            return totals.get("percent_covered"), [], 0, 0

        summary = entry.get("summary", {})
        return (
            summary.get("percent_covered"),
            entry.get("missing_lines", []),
            summary.get("covered_lines", 0),
            summary.get("num_statements", 0),
        )
