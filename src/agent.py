"""The agent loop.

    write tests -> run in sandbox -> read the failure -> revise -> repeat

Three things this gets right that a naive loop does not:

1. It keeps the best *passing* attempt, not the last attempt. A model that
   raises coverage from 70% to 85% and then breaks the suite on iteration 4
   should not lose the 85%. `best` is only updated when tests actually pass.

2. It distinguishes "tests failed" from "tests could not run". A syntax error
   needs a different instruction than a wrong assertion, and telling the model
   "1 test failed" when the file would not even parse sends it in the wrong
   direction.

3. It stops when it stops improving. Coverage plateaus are the normal ending;
   burning ten more iterations for +0.3% is how these systems get expensive
   without getting better.

Everything is emitted as trace events so a UI can stream the reasoning live.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable, Iterator

from .runner import TestRunner, TestRunResult

# Anthropic per-token pricing, USD per million tokens. Update as needed; the
# point is that the agent reports what a run cost, not that these never change.
PRICING = {
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-opus-4-5": {"input": 15.00, "output": 75.00},
}
DEFAULT_MODEL = "claude-sonnet-4-5"


class Event(str, Enum):
    START = "start"
    THINKING = "thinking"
    GENERATED = "generated"
    EXECUTING = "executing"
    RESULT = "result"
    IMPROVED = "improved"
    REGRESSED = "regressed"
    RETRY = "retry"
    PLATEAU = "plateau"
    DONE = "done"
    ERROR = "error"


@dataclass
class TraceEvent:
    event: Event
    iteration: int
    message: str
    data: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    cost_usd: float = 0.0

    def to_json(self) -> str:
        d = asdict(self)
        d["event"] = self.event.value
        return json.dumps(d)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = DEFAULT_MODEL

    def add(self, in_tokens: int, out_tokens: int) -> None:
        self.input_tokens += in_tokens
        self.output_tokens += out_tokens

    @property
    def cost_usd(self) -> float:
        rates = PRICING.get(self.model, PRICING[DEFAULT_MODEL])
        return (
            self.input_tokens / 1_000_000 * rates["input"]
            + self.output_tokens / 1_000_000 * rates["output"]
        )


@dataclass
class AgentResult:
    """Outcome of one full agent run against one module."""

    module_name: str
    success: bool
    iterations_used: int
    best_test_source: str | None
    best_coverage: float | None
    baseline_coverage: float | None
    final_result: TestRunResult | None
    usage: Usage
    trace: list[TraceEvent] = field(default_factory=list)
    duration_s: float = 0.0
    self_corrected: bool = False       # did it recover from a failing attempt?

    @property
    def coverage_delta(self) -> float | None:
        if self.best_coverage is None or self.baseline_coverage is None:
            return None
        return self.best_coverage - self.baseline_coverage

    def summary(self) -> str:
        if not self.success:
            return (
                f"{self.module_name}: FAILED after {self.iterations_used} iterations "
                f"(${self.usage.cost_usd:.4f})"
            )
        delta = f" (+{self.coverage_delta:.1f} pts)" if self.coverage_delta else ""
        corrected = ", self-corrected" if self.self_corrected else ""
        return (
            f"{self.module_name}: {self.best_coverage:.1f}% coverage{delta} in "
            f"{self.iterations_used} iterations{corrected}, "
            f"${self.usage.cost_usd:.4f}, {self.duration_s:.1f}s"
        )


SYSTEM_PROMPT = """You write pytest test suites for Python modules.

Rules:
- Output ONLY a Python code block. No explanation before or after it.
- Import the module by name, e.g. `from mymodule import thing`.
- Test behaviour, not implementation. Do not assert on private helpers.
- Cover the branches that matter: happy path, boundaries, and the error cases
  the code explicitly raises.
- Use `pytest.raises` for expected exceptions.
- No network calls, no file I/O outside tmp_path, no sleeps.
- Every test must be deterministic. No randomness, no wall-clock dependence.

When given feedback about a failing run, change only what is broken. Do not
rewrite tests that already pass."""


def _first_code_block(text: str) -> str | None:
    """Pull the first fenced Python block out of a model response."""
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Model ignored the fence instruction but produced plausible test code
    if "def test_" in text:
        return text.strip()
    return None


class TestWritingAgent:
    # Not a pytest test class, despite the name.
    __test__ = False

    def __init__(
        self,
        runner: TestRunner,
        client=None,
        model: str = DEFAULT_MODEL,
        max_iterations: int = 5,
        min_improvement: float = 2.0,
        target_coverage: float = 90.0,
    ):
        self.runner = runner
        self.model = model
        self.max_iterations = max_iterations
        self.min_improvement = min_improvement
        self.target_coverage = target_coverage

        if client is None:
            try:
                from anthropic import Anthropic
                client = Anthropic()
            except ImportError as e:
                raise RuntimeError(
                    "anthropic package not installed and no client supplied. "
                    "pip install anthropic, or pass client=FakeClient() for tests."
                ) from e
        self.client = client

    # -- LLM call ----------------------------------------------------------

    def _complete(self, messages: list[dict], usage: Usage) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        if hasattr(response, "usage"):
            usage.add(response.usage.input_tokens, response.usage.output_tokens)
        return "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )

    # -- main loop ---------------------------------------------------------

    def run(
        self,
        module_name: str,
        module_source: str,
        existing_tests: str | None = None,
        on_event: Callable[[TraceEvent], None] | None = None,
    ) -> AgentResult:
        """Iterate until the tests pass and coverage stops improving."""
        started = time.monotonic()
        usage = Usage(model=self.model)
        trace: list[TraceEvent] = []

        def emit(event: Event, iteration: int, message: str, **data) -> None:
            ev = TraceEvent(
                event=event,
                iteration=iteration,
                message=message,
                data=data,
                elapsed_s=round(time.monotonic() - started, 2),
                cost_usd=round(usage.cost_usd, 6),
            )
            trace.append(ev)
            if on_event:
                on_event(ev)

        emit(Event.START, 0, f"Writing tests for {module_name}",
             lines=len(module_source.splitlines()))

        # Baseline: what does the existing suite cover, if there is one?
        baseline = None
        if existing_tests:
            emit(Event.EXECUTING, 0, "Measuring baseline coverage of existing tests")
            base_result = self.runner.run(module_name, module_source, existing_tests)
            baseline = base_result.coverage_percent if base_result.passed else None
            emit(Event.RESULT, 0,
                 f"Baseline coverage: {baseline if baseline is not None else 'n/a'}",
                 coverage=baseline)

        messages: list[dict] = [{
            "role": "user",
            "content": (
                f"Write a pytest suite for this module, saved as "
                f"`{module_name}.py`:\n\n```python\n{module_source}\n```"
                + (f"\n\nAn existing suite covers {baseline:.0f}%. Add tests for "
                   f"what it misses.\n\n```python\n{existing_tests}\n```"
                   if existing_tests and baseline is not None else "")
            ),
        }]

        best_source: str | None = None
        best_coverage: float | None = None
        last_result: TestRunResult | None = None
        had_failure = False
        iterations = 0

        for i in range(1, self.max_iterations + 1):
            iterations = i
            emit(Event.THINKING, i, "Asking the model for tests")

            try:
                reply = self._complete(messages, usage)
            except Exception as e:
                emit(Event.ERROR, i, f"Model call failed: {e.__class__.__name__}: {e}")
                break

            test_source = _first_code_block(reply)
            if not test_source:
                emit(Event.ERROR, i, "No code block in the response; retrying")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content":
                                 "Output only a fenced Python code block."})
                had_failure = True
                continue

            emit(Event.GENERATED, i, f"Generated {len(test_source.splitlines())} lines",
                 test_count=test_source.count("def test_"))

            emit(Event.EXECUTING, i, "Running the suite in the sandbox")
            result = self.runner.run(module_name, module_source, test_source)
            last_result = result

            emit(Event.RESULT, i, result.feedback().split("\n")[0],
                 passed=result.passed,
                 coverage=result.coverage_percent,
                 tests=result.tests_collected,
                 failed=result.tests_failed)

            if not result.is_usable:
                had_failure = True
                emit(Event.RETRY, i, "Suite did not pass; feeding the failure back")
                messages.append({"role": "assistant", "content": reply})
                messages.append({"role": "user", "content": result.feedback()})
                continue

            coverage = result.coverage_percent or 0.0
            previous = best_coverage

            if best_coverage is None or coverage > best_coverage:
                improvement = coverage - (previous or 0.0)
                best_coverage, best_source = coverage, test_source
                emit(Event.IMPROVED, i,
                     f"Coverage now {coverage:.1f}% (+{improvement:.1f})",
                     coverage=coverage, improvement=round(improvement, 2))

                if coverage >= self.target_coverage:
                    emit(Event.DONE, i, f"Reached target of {self.target_coverage}%")
                    break
                if previous is not None and improvement < self.min_improvement:
                    emit(Event.PLATEAU, i,
                         f"Gain of {improvement:.1f} pts is below the "
                         f"{self.min_improvement} pt threshold; stopping")
                    break
            else:
                # Passing but no better. Keep the earlier, better suite.
                emit(Event.REGRESSED, i,
                     f"{coverage:.1f}% does not beat {best_coverage:.1f}%; "
                     f"keeping the earlier suite", coverage=coverage)
                break

            # Ask for more, pointing at the specific uncovered lines
            uncovered = result.missing_lines
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": (
                f"All tests pass and coverage is {coverage:.1f}%. "
                f"Lines {uncovered} are still uncovered. Return the complete "
                f"suite with additional tests that reach them. Keep the "
                f"existing tests unchanged."
            )})

        success = best_source is not None
        if success:
            emit(Event.DONE, iterations,
                 f"Final: {best_coverage:.1f}% coverage, ${usage.cost_usd:.4f}",
                 coverage=best_coverage, cost=round(usage.cost_usd, 6))
        else:
            emit(Event.ERROR, iterations, "No passing suite produced")

        return AgentResult(
            module_name=module_name,
            success=success,
            iterations_used=iterations,
            best_test_source=best_source,
            best_coverage=best_coverage,
            baseline_coverage=baseline,
            final_result=last_result,
            usage=usage,
            trace=trace,
            duration_s=round(time.monotonic() - started, 2),
            self_corrected=success and had_failure,
        )

    def stream(self, module_name: str, module_source: str, **kw) -> Iterator[TraceEvent]:
        """Generator form, for server-sent events in a UI."""
        events: list[TraceEvent] = []
        self.run(module_name, module_source, on_event=events.append, **kw)
        yield from events
