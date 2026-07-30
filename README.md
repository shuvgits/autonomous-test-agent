# Autonomous Test-Writing Agent

![Demo](demo.gif)

**Writes pytest suites, runs them in a sandbox, reads its own failures, and fixes them.**

Point it at a Python module. It generates a test suite, executes it inside an
isolated sandbox, measures coverage, and iterates — feeding real execution
failures back to the model until the suite passes and coverage stops improving.
It reports what each run cost.

Benchmarked on seven `boltons` modules: **95.3% mean coverage, 6 of 7 modules,
$0.08 per module**, with half the successful runs recovering from a failing first
attempt.

```
[7/7] /tmp/small/typeutils.py
  start      [0] Writing tests for typeutils                     0.0s  $0.0000
  thinking   [1] Asking the model for tests                      0.0s  $0.0000
  generated  [1] Generated 209 lines                            18.6s  $0.0288
  executing  [1] Running the suite in the sandbox               18.6s  $0.0288
  result     [1] 1 failed, 22 passed. Fix only the failing...   19.4s  $0.0288
  retry      [1] Suite did not pass; feeding the failure back   19.4s  $0.0288
  thinking   [2] Asking the model for tests                     19.4s  $0.0288
  generated  [2] Generated 203 lines                            34.7s  $0.0618
  executing  [2] Running the suite in the sandbox               34.7s  $0.0618
  result     [2] All 22 tests passed. Coverage 94.2%            35.5s  $0.0618
  improved   [2] Coverage now 94.2% (+94.2)                     35.5s  $0.0618
  done       [2] Final: 94.2% coverage, $0.0618                 35.5s  $0.0618
  wrote generated_tests/test_typeutils.py
```

That is a real run. The first attempt produced 22 tests with one failure; the
sandbox surfaced the actual pytest error, the model fixed that single test, and
the second attempt passed all 22 at 94.2% coverage. Nothing about the recovery
was scripted.

---

## Status

| Component | State |
|---|---|
| Sandbox (Docker + subprocess backends) | Complete, 19 containment tests |
| Test execution and coverage measurement | Complete |
| Agent loop with self-correction | Complete, 13 tests |
| CLI with budget cap and JSON report | Complete |
| Benchmark across public repos | Run: 7 modules from `boltons` |
| Web UI with streaming trace | Not started |

**32 tests passing**, and benchmarked against real third-party code.

## Why a sandbox

The code being executed is model output. It is untrusted by definition: it may
loop forever, allocate everything, fork endlessly, write outside its directory,
or try to reach the network. The sandbox makes those outcomes boring.

Verified containment, one test per row:

| Attack | Result |
|---|---|
| `while True: pass` | SIGXCPU at the CPU budget |
| Unbounded allocation | Contained (`RLIMIT_AS` on Linux, timeout on macOS) |
| `while True: os.fork()` | Contained |
| 200 MB file write | Blocked by `RLIMIT_FSIZE` |
| Runaway recursion | Contained, host unaffected |
| `sleep(30)` under a 2s timeout | Whole process group killed promptly |
| `../escape.py` and absolute paths | Refused before any write |
| Parent environment inheritance | Not inherited |

That last row matters more than it looks. This tool runs with an API key in its
environment. Without a deny-by-default env, generated code could read
`ANTHROPIC_API_KEY` and exfiltrate it. The sandbox builds the child environment
from nothing.

### Two backends, honestly labelled

**`DockerSandbox`** is real isolation: separate namespaces, `network_disabled`,
read-only root with a small writable tmpfs, `cap_drop ALL`,
`no-new-privileges`, `pids_limit`, hard memory ceiling with no swap escape,
running as `nobody`.

**`SubprocessSandbox`** is POSIX rlimits plus a temp directory. It stops runaway
resource use and accidental damage. It is **not** a security boundary: same
process tree, no network namespace, and no barrier to reading files the current
user can read.

`get_sandbox()` prefers Docker and prints a loud warning when it falls back,
because silently downgrading a security boundary is how incidents happen. A
sandbox trusted more than it deserves is worse than no sandbox.

## Three loop behaviours worth naming

**It keeps the best passing attempt, not the last one.** A model that reaches
85% on iteration 2 and then breaks the suite on iteration 4 should not lose the
85%. `best_coverage` only updates when tests actually pass, and a passing-but-
worse suite triggers a `regressed` event rather than overwriting.

**It distinguishes "tests failed" from "tests could not run".** A syntax error
needs a different instruction than a wrong assertion. Telling the model "1 test
failed" when the file would not parse sends it in the wrong direction. pytest
exit codes other than 0 and 1 are classified as collection errors, and the
feedback message differs accordingly.

**It stops when it stops improving.** Coverage plateaus are the normal ending.
Burning five more iterations for +0.3% is how agents get expensive without
getting better, so a gain below `min_improvement` ends the run.

Nothing trusts the model's claim about its own work. The only source of truth is
what actually executed.

## Results

Benchmarked against seven modules from [boltons](https://github.com/mahmoud/boltons),
a pure-stdlib utility library, at 35 to 113 statements each. Model:
`claude-sonnet-4-5`, capped at 3 iterations per module.

| Metric | Value |
|---|---|
| Modules attempted | 7 |
| Suites that passed | **6 (86%)** |
| Mean coverage achieved | **95.3%** |
| Self-correction rate | **3 of 6 (50%)** |
| Mean iterations to pass | 1.7 |
| Total cost | $0.57 |
| Mean cost per module | **$0.082** |

Per module:

| Module | Statements | Coverage | Iterations | Self-corrected | Cost |
|---|---|---|---|---|---|
| `pathutils` | 36 | 100.0% | 1 | | $0.031 |
| `mathutils` | 113 | 100.0% | 1 | | $0.056 |
| `queueutils` | 86 | 96.5% | 1 | | $0.044 |
| `typeutils` | 55 | 94.2% | 2 | yes | $0.062 |
| `gcutils` | 35 | 90.6% | 3 | yes | $0.129 |
| `namedutils` | 103 | 90.4% | 2 | yes | $0.137 |
| `mboxutils` | 53 | failed | 3 | | $0.114 |

**The self-correction rate is the number worth reading.** Half the successful
runs failed on the first attempt and recovered from real execution output. On
`typeutils`, iteration 1 produced 22 tests with one failure; the pytest error
went back to the model and iteration 2 passed all 22 at 94.2% coverage. On
`gcutils` it took three rounds, going 2 failures, then 1, then clean — fixing
individual tests rather than rewriting the suite, which is what the "change only
what is broken" instruction is for.

### The failure is as informative as the successes

`mboxutils` wraps `mailbox` and touches the filesystem. Iterations 1 and 2 both
**timed out at 60 seconds** — the model wrote tests that blocked on file I/O the
sandbox would not permit. Iteration 3 dropped from 198 lines to 110, and got to
1 failure out of 8, but ran out of budget.

Two real limits show up in that one row:

- The agent cannot distinguish an *unfixable environment constraint* from a
  *fixable test bug*. It kept trying, because a timeout looks like a failure it
  could fix.
- Modules whose behaviour depends on real I/O are out of reach for a
  network-isolated, filesystem-restricted sandbox. That is the sandbox working
  as designed, not a bug, but it bounds what the tool can be pointed at.

An earlier run against [cachecontrol](https://github.com/psf/cachecontrol) failed
on every module for a related reason: it depends on `requests`, which is not
installed inside the sandbox, so generated tests never even imported. Five
iterations of unfixable `ImportError` cost $0.22 for one module and produced
nothing. **Dependency-free modules are the operating range.**

### The loop can stall on a repeated failure

Running `gcutils` a second time produced this:

| Iteration | Result |
|---|---|
| 1 | 1 failed, 17 passed |
| 2 | 1 failed, 17 passed |
| 3 | 1 failed, 17 passed |
| 4 | 4 lines generated, collection error |
| 5 | All 18 passed, 90.6% coverage |

Iterations 1 through 3 are byte-identical in outcome. The model was not
converging; it was making cosmetic edits to the same broken test while the
seventeen passing ones stayed passing. Iteration 4 collapsed to a 4-line file
before iteration 5 solved it. Five iterations, $0.24 — three times the cost of
the same module succeeding in three rounds on the earlier benchmark run.

**The loop has no notion of a repeated failure.** It compares coverage between
attempts but never asks whether the *failure signature* has changed. A better
design would hash the failure output, notice the same signature three times, and
switch strategy: instruct the model to delete the offending test rather than keep
patching it, or stop early and report the module as partially covered.

This is the clearest improvement available and it is not implemented. It surfaced
from running the tool rather than from reading the code.

### Where it works and where it does not

| Module profile | Result |
|---|---|
| Pure functions, stdlib only, under ~120 statements | Reliable, 90 to 100% coverage |
| Third-party imports | Fails at import; sandbox has no network |
| Real file or socket I/O | Times out |
| Over ~400 statements | Model writes suites too large to keep coherent |

## Usage

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...

# one module
python -m src.cli path/to/module.py

# a package, capped at $2, writing suites out
python -m src.cli src/ --out tests/generated/ --budget 2.00 --report results.json

# see what would run, no API calls
python -m src.cli src/ --dry-run
```

Useful flags: `--max-iterations`, `--target-coverage`, `--timeout`,
`--sandbox {auto,docker,subprocess}`, `--budget`, `--no-color`.

The budget cap is not decoration. An agent looping on a module it cannot solve
is the failure mode that costs money, so spend is checked between modules.

```bash
pytest -q          # 32 tests, no API key required
```

The agent tests use a scripted fake client. You cannot write a reliable test for
"does it recover from failure" against a live model, so the control flow is
tested deterministically and the model call is the seam.

## Structure

```
├── src/
│   ├── sandbox.py     Docker + subprocess backends, resource limits
│   ├── runner.py      pytest execution and coverage parsing
│   ├── agent.py       the loop, cost tracking, trace events
│   └── cli.py         entry point, budget cap, JSON report
├── tests/
│   ├── test_sandbox.py   19 containment tests
│   └── test_agent.py     13 loop tests
└── fixtures/          sample modules to run against
```

## Limitations

- **Coverage is not test quality.** 100% line coverage says every line ran, not
  that behaviour was checked. A suite of `assert True` can reach it. Mutation
  testing would measure the thing that actually matters, and is not implemented.
- **Single-module scope.** Modules with heavy imports, database access, or
  network dependencies are out of reach; the sandbox has no network by design.
- **No branch coverage.** Line coverage only, which overstates thoroughness on
  conditionals.
- **The model can write tautological tests.** Nothing here detects a test that
  asserts the implementation back at itself.
- **Cost estimates depend on hardcoded pricing** in `agent.py` and will drift.
- **`SubprocessSandbox` is not a security boundary.** Stated again because it
  matters: use Docker for anything you would not run on your own machine.
- **Python only.**
- **Dependency-free modules only.** The sandbox has no network by design, so
  third-party imports are unavailable and generated tests fail at import. A run
  against `cachecontrol` (which needs `requests`) failed on every module for
  this reason.
- **No repeated-failure detection.** The loop can spend three iterations on the
  same unchanged failure, because it tracks coverage between attempts but not
  whether the failure itself changed. Observed costing $0.24 on a module that
  succeeded for $0.13 on another run.
- **The agent cannot tell an unfixable constraint from a fixable bug.** A test
  that times out because the sandbox blocks file I/O looks, to the loop, like a
  failure worth retrying. It will spend its full iteration budget on it. A
  classifier that distinguished environment errors from test errors and bailed
  early would cut wasted spend.
- **On macOS, `SubprocessSandbox` is weaker than on Linux.** Two rlimits behave
  differently on Darwin: `RLIMIT_NPROC` is scoped per-user rather than
  per-process-tree (so setting it low makes the first fork fail outright, and it
  is skipped), and `RLIMIT_AS` is not reliably enforced. Fork bombs and memory
  bombs are therefore contained by the wall-clock timeout there rather than by a
  hard cap. `DockerSandbox` enforces both properly via `pids_limit` and
  `mem_limit`. Use Docker on macOS for anything you would not run unsandboxed.

## Next

1. **Failure-signature tracking.** Hash the failure output per iteration; on a
   repeated signature, change strategy instead of asking for another patch. This
   is the highest-value fix, based on the stall documented above.
2. **Environment-error classifier.** Distinguish an unfixable `ImportError` or
   sandbox-blocked I/O from a fixable assertion failure, and bail early rather
   than spending the iteration budget.
3. Mutation testing with `mutmut`, so the metric measures test strength rather
   than line execution.
4. Branch coverage instead of line coverage.
5. Web UI streaming the trace over server-sent events.
6. Multi-file modules with dependency resolution.

## License

MIT
