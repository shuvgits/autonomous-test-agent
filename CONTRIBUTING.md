# Contributing

## Setup

```bash
git clone https://github.com/shuvgits/autonomous-test-agent.git
cd autonomous-test-agent
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Running Tests

The test suite is deterministic and runs without an API key:

```bash
pytest -q
```

All 32 tests should pass. The agent tests use a scripted fake client, so test coverage of the loop and self-correction logic does not require live Claude calls.

## Trying the Agent

Export your API key and run against a sample module:

```bash
export ANTHROPIC_API_KEY=sk-...
python -m src.cli fixtures/pathutils.py
```

For a dry run (no API calls, just planning):

```bash
python -m src.cli fixtures/pathutils.py --dry-run
```

## Type Checking

The codebase uses mypy for static type checking:

```bash
mypy src/ tests/
```

## Project Structure

```
src/
  sandbox.py     Docker + subprocess backends with resource limits
  runner.py      pytest execution and coverage parsing
  agent.py       main loop, cost tracking, trace events
  cli.py         entry point, budget cap, JSON report

tests/
  test_sandbox.py   19 containment tests (resource limits, env isolation, etc.)
  test_agent.py     13 loop tests (recovery, coverage tracking, stopping)

fixtures/          sample modules for local testing
benchmarks/        reproducible benchmark data and scripts
```

## Key Design Principles

- **Sandbox first:** All model output runs in isolation. Docker is the default; subprocess is development-only.
- **Real execution feedback:** The only source of truth is what actually executed. Failures from pytest go back to the model; the model does not trust its own claims.
- **Bounded self-correction:** The loop stops when coverage plateaus or iteration budget is exhausted.
- **Transparent reporting:** All costs, iterations, and trace events are logged to JSON.

## Before Submitting a PR

1. Run `pytest -q` locally and ensure all tests pass.
2. Run `mypy src/ tests/` and fix any type issues.
3. Test your changes against one of the fixture modules.
4. Document why the change matters (bugfix, performance, new capability, etc.).

## Known Limitations

These are documented in the README and are not bugs:

- **Dependency-free modules only:** The sandbox has no network, so third-party imports are unavailable.
- **Repeated-failure detection missing:** The loop can stall on the same failure. See the benchmarks document for an example.
- **Subprocess is not a security boundary:** Use Docker for untrusted code.
- **Python-only:** No JavaScript, Go, etc.

Contributions that reduce wasted spend (e.g., early bail on unfixable constraints) or improve determinism are especially welcome.
