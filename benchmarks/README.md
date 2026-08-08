# Benchmark: boltons Pure-Stdlib Modules

## Summary

Benchmarked seven dependency-free modules from [boltons](https://github.com/mahmoud/boltons) using Claude Opus. Mean coverage: 95.3%. Mean cost per module: $0.082. Self-correction rate: 50% (3 of 6 passing suites recovered from iteration 1 failure).

## Methodology & Scope

**This is a limited evaluation on seven small, pure-stdlib modules—not a general claim of 95.3% coverage across Python projects.**

- **Modules tested:** pathutils, mathutils, queueutils, typeutils, gcutils, namedutils, mboxutils
- **Statement count per module:** 35–113
- **Dependencies:** None (stdlib only)
- **Model:** Claude Opus
- **Iteration cap:** 3 per module
- **Timeout per run:** 60 seconds
- **Coverage metric:** Line coverage only (no branch coverage)

**Out of scope:**
- Modules with third-party imports (sandbox has no network)
- Modules with heavy file I/O or socket operations (sandbox blocks these)
- Modules over ~400 statements (model writing coherent large suites is harder)
- Test quality (100% line coverage does not mean tests catch bugs; mutation testing would measure that)

## Run Metadata

| Field | Value |
|-------|-------|
| Date | July 24, 2026 |
| Boltons commit | [specify: e.g., `abc123def` or version tag] |
| Agent version | [specify: e.g., commit hash or version tag of this repo] |
| Model | Claude Opus |
| CLI parameters | `--max-iterations 3 --timeout 60` |
| Raw results | `results.json` (in this directory) |

## How to Reproduce

### Prerequisites

```bash
git clone https://github.com/mahmoud/boltons.git
cd boltons
git checkout [COMMIT_HASH]  # See "Boltons commit" above
```

### Run the Benchmark

```bash
cd /path/to/autonomous-test-agent
export ANTHROPIC_API_KEY=sk-...

# Run against the seven modules
python -m src.cli /path/to/boltons/boltons/typeutils.py \
  --max-iterations 3 \
  --timeout 60 \
  --sandbox docker \
  --report typeutils_results.json

# Repeat for: pathutils, mathutils, queueutils, gcutils, namedutils, mboxutils
```

Or use the bundled script:

```bash
bash benchmarks/run_benchmark.sh /path/to/boltons
```

### Interpreting Results

See `results.json` for:
- Per-module coverage, iterations, cost
- Per-iteration trace (thinking, generated, executing, result, retry, improved, done)
- Reason for stopping (coverage plateau, iteration limit, or failure)
- Whether self-correction occurred (iteration 2+ passed after iteration 1 failed)

Key row: **mboxutils (failed).** It wraps the `mailbox` module and requires real file I/O. The sandbox blocked writes and file opens, causing timeouts in iterations 1–2. Iteration 3 produced fewer tests (110 lines vs. 198) but still failed, hitting the iteration budget. This is the sandbox working as designed, not a tool bug, but it illustrates the operating limits.

Key finding: **3 of 6 passing suites (50%) recovered from a failing first attempt.** The model took real pytest errors (syntax errors, assertion failures, import errors) and fixed them without rewriting the entire suite. This is self-correction in practice.

## Known Limitations Surfaced

1. **Repeated-failure detection not implemented.** Running gcutils a second time, iterations 1–3 produced byte-identical failures (1 failed, 17 passed). The model made cosmetic edits without converging. Iteration 4 collapsed to 4 lines before iteration 5 succeeded. This cost $0.24 vs. $0.13 on a successful earlier run. A failure-signature hash would have detected stalling and changed strategy.

2. **No environment-error classifier.** On cachecontrol (not in this benchmark but observed separately), every module failed at import because requests is not installed in the sandbox. The loop spent 5 iterations on unfixable ImportErrors, costing $0.22. The model cannot distinguish "this is unfixable" from "this is a test bug worth retrying."

3. **Modules depending on real I/O are unreachable.** mboxutils and cachecontrol both depend on network or filesystem access outside the sandbox. This is the sandbox working correctly (isolation is the goal), but it bounds the tool's applicability.

## Results Table

| Module | Statements | Coverage | Iterations | Self-corrected | Cost |
|--------|-----------|----------|-----------|----------------|------|
| pathutils | 36 | 100.0% | 1 | – | $0.031 |
| mathutils | 113 | 100.0% | 1 | – | $0.056 |
| queueutils | 86 | 96.5% | 1 | – | $0.044 |
| typeutils | 55 | 94.2% | 2 | ✓ | $0.062 |
| gcutils | 35 | 90.6% | 3 | ✓ | $0.129 |
| namedutils | 103 | 90.4% | 2 | ✓ | $0.137 |
| mboxutils | 53 | failed | 3 | – | $0.114 |
| **Mean** | **69.3** | **95.3%*** | **1.7** | **50%** | **$0.082** |

*95.3% = mean of 6 successful modules only; mboxutils excluded.

## Next Steps

- **Failure-signature detection:** Hash failure output per iteration; on repeated signature, change strategy instead of asking for another patch.
- **Environment-error classifier:** Bail early on unfixable ImportError or sandbox-blocked I/O.
- **Mutation testing:** Measure test strength, not just line coverage.
- **Benchmark on larger modules:** Evaluate performance on modules over 200 statements.
