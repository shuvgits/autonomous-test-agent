#!/usr/bin/env python3
"""Command line entry point.

    # single file
    python -m src.cli path/to/module.py

    # whole package, with a report
    python -m src.cli src/ --report results.json

    # write the generated suites to disk
    python -m src.cli src/ --out tests/generated/

Requires ANTHROPIC_API_KEY unless --dry-run is passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import AgentResult, Event, TestWritingAgent
from .runner import TestRunner
from .sandbox import SandboxLimits, get_sandbox

COLORS = {
    Event.START: "\033[36m", Event.THINKING: "\033[90m",
    Event.GENERATED: "\033[94m", Event.EXECUTING: "\033[93m",
    Event.RESULT: "\033[97m", Event.IMPROVED: "\033[92m",
    Event.REGRESSED: "\033[33m", Event.RETRY: "\033[33m",
    Event.PLATEAU: "\033[35m", Event.DONE: "\033[92m",
    Event.ERROR: "\033[91m",
}
RESET = "\033[0m"


def print_event(event, use_color: bool = True) -> None:
    color = COLORS.get(event.event, "") if use_color else ""
    dim = "\033[90m" if use_color else ""
    end = RESET if use_color else ""
    print(
        f"  {color}{event.event.value:<10}{end} "
        f"[{event.iteration}] {event.message}  "
        f"{dim}{event.elapsed_s:>5.1f}s  ${event.cost_usd:.4f}{end}"
    )


def discover_modules(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(
        p for p in target.rglob("*.py")
        if not p.name.startswith("test_")
        and p.name != "__init__.py"
        and "__pycache__" not in p.parts
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Autonomous test-writing agent")
    ap.add_argument("target", type=Path, help="Python file or directory")
    ap.add_argument("--out", type=Path, help="Directory to write generated suites into")
    ap.add_argument("--report", type=Path, help="Write a JSON report here")
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--max-iterations", type=int, default=5)
    ap.add_argument("--target-coverage", type=float, default=90.0)
    ap.add_argument("--timeout", type=int, default=60, help="Sandbox timeout, seconds")
    ap.add_argument("--sandbox", choices=["auto", "docker", "subprocess"], default="auto")
    ap.add_argument("--budget", type=float, default=5.00,
                    help="Stop once total spend exceeds this, in USD")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="List the modules that would be processed and exit")
    args = ap.parse_args(argv)

    if not args.target.exists():
        print(f"error: {args.target} does not exist", file=sys.stderr)
        return 2

    modules = discover_modules(args.target)
    if not modules:
        print(f"error: no Python modules found under {args.target}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"{len(modules)} module(s) would be processed:")
        for m in modules:
            print(f"  {m}")
        return 0

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2

    sandbox = get_sandbox(args.sandbox)
    runner = TestRunner(
        sandbox,
        SandboxLimits(timeout_s=args.timeout,
                      cpu_seconds=max(1, args.timeout - 10),
                      memory_mb=512),
    )
    agent = TestWritingAgent(
        runner, model=args.model,
        max_iterations=args.max_iterations,
        target_coverage=args.target_coverage,
    )

    results: list[AgentResult] = []
    spent = 0.0

    for i, path in enumerate(modules, 1):
        if spent >= args.budget:
            print(f"\nBudget of ${args.budget:.2f} reached; stopping "
                  f"({len(modules) - i + 1} module(s) skipped)")
            break

        print(f"\n[{i}/{len(modules)}] {path}")
        try:
            source = path.read_text()
        except (UnicodeDecodeError, OSError) as e:
            print(f"  skipped: {e}")
            continue
        if not source.strip():
            print("  skipped: empty file")
            continue

        result = agent.run(
            path.stem, source,
            on_event=lambda ev: print_event(ev, not args.no_color),
        )
        results.append(result)
        spent += result.usage.cost_usd

        if result.success and args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            out_file = args.out / f"test_{path.stem}.py"
            out_file.write_text(result.best_test_source)
            print(f"  wrote {out_file}")

    # -- summary -----------------------------------------------------------

    succeeded = [r for r in results if r.success]
    corrected = [r for r in succeeded if r.self_corrected]

    print("\n" + "=" * 66)
    print(f"{len(succeeded)}/{len(results)} modules covered")
    if succeeded:
        mean_cov = sum(r.best_coverage for r in succeeded) / len(succeeded)
        print(f"mean coverage      {mean_cov:.1f}%")
        print(f"self-corrected     {len(corrected)}/{len(succeeded)} "
              f"({100 * len(corrected) / len(succeeded):.0f}%)")
        mean_iters = sum(r.iterations_used for r in succeeded) / len(succeeded)
        print(f"mean iterations    {mean_iters:.1f}")
    print(f"total cost         ${spent:.4f}")
    print("=" * 66)

    for r in results:
        print(" ", r.summary())

    if args.report:
        args.report.write_text(json.dumps({
            "modules": len(results),
            "succeeded": len(succeeded),
            "mean_coverage": (sum(r.best_coverage for r in succeeded) / len(succeeded))
                             if succeeded else None,
            "self_correction_rate": (len(corrected) / len(succeeded)) if succeeded else None,
            "total_cost_usd": round(spent, 6),
            "results": [{
                "module": r.module_name,
                "success": r.success,
                "coverage": r.best_coverage,
                "baseline": r.baseline_coverage,
                "iterations": r.iterations_used,
                "self_corrected": r.self_corrected,
                "cost_usd": round(r.usage.cost_usd, 6),
                "duration_s": r.duration_s,
            } for r in results],
        }, indent=2))
        print(f"\nreport written to {args.report}")

    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
