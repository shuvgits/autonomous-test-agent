"""Agent loop tests.

These run without an API key. A scripted `FakeClient` replays fixed responses,
which makes the loop's control flow testable and deterministic — you cannot
write a reliable test for "does it recover from failure" against a live model.

Run: pytest tests/test_agent.py -q
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agent import Event, TestWritingAgent, Usage
from src.runner import TestRunner
from src.sandbox import SubprocessSandbox

MODULE = '''
def classify(score):
    if score < 0:
        raise ValueError("negative score")
    if score < 50:
        return "fail"
    if score < 80:
        return "pass"
    return "distinction"
'''

FULL_SUITE = (
    "import pytest\n"
    "from grader import classify\n"
    'def test_distinction(): assert classify(90) == "distinction"\n'
    'def test_fail(): assert classify(10) == "fail"\n'
    'def test_pass(): assert classify(60) == "pass"\n'
    "def test_negative():\n"
    "    with pytest.raises(ValueError): classify(-1)"
)

PARTIAL_SUITE = (
    "from grader import classify\n"
    'def test_fail(): assert classify(10) == "fail"\n'
    'def test_pass(): assert classify(60) == "pass"'
)

BROKEN_ASSERTION = (
    "from grader import classify\n"
    'def test_wrong(): assert classify(90) == "pass"'
)

SYNTAX_ERROR = "def test_broken(:\n    pass"


def block(code: str) -> str:
    return f"```python\n{code}\n```"


class FakeClient:
    """Replays scripted replies in order, repeating the last one if exhausted."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            idx = min(self.outer.calls, len(self.outer.replies) - 1)
            text = self.outer.replies[idx]
            self.outer.calls += 1
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=800, output_tokens=300),
            )

    @property
    def messages(self):
        return self._Messages(self)


def make_agent(replies, **kwargs):
    defaults = dict(max_iterations=5, target_coverage=95.0, min_improvement=2.0)
    defaults.update(kwargs)
    return TestWritingAgent(
        TestRunner(SubprocessSandbox()), client=FakeClient(replies), **defaults
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_reaches_full_coverage_in_one_iteration():
    agent = make_agent([block(FULL_SUITE)])
    result = agent.run("grader", MODULE)

    assert result.success
    assert result.best_coverage == 100.0
    assert result.iterations_used == 1
    assert not result.self_corrected


def test_stops_once_target_coverage_is_reached():
    agent = make_agent([block(FULL_SUITE)], target_coverage=90.0, max_iterations=5)
    result = agent.run("grader", MODULE)
    assert result.iterations_used == 1
    assert any(e.event is Event.DONE for e in result.trace)


# ---------------------------------------------------------------------------
# Self-correction -- the behaviour the project is about
# ---------------------------------------------------------------------------


def test_recovers_from_a_failing_first_attempt():
    agent = make_agent([block(BROKEN_ASSERTION), block(FULL_SUITE)])
    result = agent.run("grader", MODULE)

    assert result.success
    assert result.self_corrected is True
    assert result.best_coverage == 100.0
    assert any(e.event is Event.RETRY for e in result.trace)


def test_recovers_from_a_syntax_error():
    agent = make_agent([block(SYNTAX_ERROR), block(FULL_SUITE)])
    result = agent.run("grader", MODULE)

    assert result.success
    assert result.self_corrected is True


def test_gives_up_after_max_iterations():
    agent = make_agent([block(BROKEN_ASSERITON_ALWAYS := "def test_x(): assert False")],
                       max_iterations=3)
    result = agent.run("grader", MODULE)

    assert not result.success
    assert result.iterations_used == 3
    assert result.best_test_source is None
    assert "FAILED" in result.summary()


def test_handles_response_with_no_code_block():
    agent = make_agent(["I would suggest writing some tests.", block(FULL_SUITE)])
    result = agent.run("grader", MODULE)
    assert result.success
    assert any(e.event is Event.ERROR for e in result.trace)


# ---------------------------------------------------------------------------
# Best-attempt retention
# ---------------------------------------------------------------------------


def test_keeps_the_best_attempt_not_the_last():
    """A later passing-but-worse suite must not overwrite a better earlier one."""
    agent = make_agent(
        [block(FULL_SUITE), block(PARTIAL_SUITE)],
        target_coverage=101.0,     # never satisfied, so the loop continues
        min_improvement=0.1,
    )
    result = agent.run("grader", MODULE)

    assert result.best_coverage == 100.0
    assert result.best_test_source.strip() == FULL_SUITE.strip()
    assert any(e.event is Event.REGRESSED for e in result.trace)


def test_stops_when_improvement_plateaus():
    smaller = PARTIAL_SUITE
    barely_better = PARTIAL_SUITE + '\ndef test_d(): assert classify(90) == "distinction"'
    agent = make_agent(
        [block(smaller), block(barely_better)],
        target_coverage=101.0,
        min_improvement=50.0,      # any realistic gain is "not enough"
        max_iterations=6,
    )
    result = agent.run("grader", MODULE)

    assert result.iterations_used < 6
    assert any(e.event is Event.PLATEAU for e in result.trace)


# ---------------------------------------------------------------------------
# Cost and trace
# ---------------------------------------------------------------------------


def test_cost_accumulates_across_iterations():
    one = make_agent([block(FULL_SUITE)]).run("grader", MODULE)
    two = make_agent([block(BROKEN_ASSERTION), block(FULL_SUITE)]).run("grader", MODULE)
    assert two.usage.cost_usd > one.usage.cost_usd
    assert one.usage.cost_usd > 0


def test_usage_pricing_math():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000,
                  model="claude-sonnet-4-5")
    assert usage.cost_usd == pytest.approx(18.00)     # 3.00 in + 15.00 out


def test_trace_is_ordered_and_serializable():
    result = make_agent([block(FULL_SUITE)]).run("grader", MODULE)

    assert result.trace[0].event is Event.START
    assert result.trace[-1].event is Event.DONE
    # elapsed time never goes backwards
    times = [e.elapsed_s for e in result.trace]
    assert times == sorted(times)
    # every event survives a JSON round trip, so a UI can stream it
    for event in result.trace:
        assert event.to_json()


def test_on_event_callback_receives_every_event():
    seen = []
    result = make_agent([block(FULL_SUITE)]).run("grader", MODULE, on_event=seen.append)
    assert len(seen) == len(result.trace)


def test_baseline_coverage_is_measured_when_tests_exist():
    agent = make_agent([block(FULL_SUITE)])
    result = agent.run("grader", MODULE, existing_tests=PARTIAL_SUITE)

    assert result.baseline_coverage is not None
    assert result.baseline_coverage < 100.0
    assert result.coverage_delta > 0
