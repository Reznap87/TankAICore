#!/usr/bin/env python3
"""
TankAI CLI

  python -m tankai.cli "Dein Ziel hier"
  python -m tankai.cli --parallel --dod "..." "Ziel"
  python -m tankai.cli --web
  python -m tankai.cli --demo
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tankai",
        description="TankAI — Web Intelligence OS (Prototyp)",
    )
    parser.add_argument("goal", nargs="?", help="Zielbeschreibung")
    parser.add_argument(
        "--dod",
        default="Eine klare, überprüfbare Antwort liegt vor.",
        help="Definition of Done",
    )
    parser.add_argument("--parallel", action="store_true", help="Parallele Specialists")
    parser.add_argument("--no-tools", action="store_true", help="Tools deaktivieren")
    parser.add_argument("--quiet", action="store_true", help="Weniger Ausgabe")
    parser.add_argument("--web", action="store_true", help="Web-UI starten")
    parser.add_argument("--demo", action="store_true", help="Kurzes Demo-Ziel ausführen")
    parser.add_argument("--selftest", action="store_true", help="Pipeline-Self-Test")
    parser.add_argument("--json", metavar="FILE", help="Ergebnis als JSON speichern")
    parser.add_argument(
        "--llm",
        default=None,
        choices=["mock", "openai", "anthropic", "echo"],
        help="LLM-Provider (default: TANKAI_LLM oder mock)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Modellname (z.B. gpt-4o-mini, claude-sonnet-4-20250514)",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Zeigt die letzten gespeicherten Runs",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Zeigt LLM-Setup-Status (Keys, Pakete)",
    )

    args = parser.parse_args(argv)

    if args.history:
        from tankai.core.run_store import RunStore
        store = RunStore("tankai_runs.jsonl")
        rows = store.list_recent(15)
        if not rows:
            print("Keine gespeicherten Runs.")
            return 0
        for r in rows:
            print(f"{r.get('ts','?')[:19]}  {r.get('status')}  {r.get('goal','')[:70]}")
        return 0

    if args.setup:
        from tankai.core.llm import describe_llm_setup
        print(describe_llm_setup())
        return 0

    if args.selftest:
        from tankai.selftest import run_selftest
        return run_selftest()

    if args.web:
        from tankai.web.server import main as web_main
        web_main()
        return 0

    goal = args.goal
    if args.demo:
        goal = (
            "Nenne die wichtigsten Vorteile und Risiken von Multi-Agenten-Systemen "
            "im Vergleich zu einzelnen großen Sprachmodellen."
        )

    if not goal:
        parser.print_help()
        return 1

    from tankai import TankAI, get_llm
    from tankai.core.long_term_memory import LongTermMemory

    llm_kwargs = {}
    if args.model:
        llm_kwargs["model"] = args.model
    try:
        llm = get_llm(args.llm, **llm_kwargs)
        provider = args.llm or "env/mock"
        if not args.quiet:
            print(f"LLM: {type(llm).__name__} (provider={provider})")
    except Exception as e:
        print(f"LLM-Fehler ({args.llm}): {e}", file=sys.stderr)
        print("Fallback auf MockLLM.", file=sys.stderr)
        llm = get_llm("mock")

    tank = TankAI(
        llm=llm,
        verbose=not args.quiet,
        use_ltm=True,
        parallel=args.parallel,
        enable_tools=not args.no_tools,
    )
    tank.ltm = LongTermMemory(in_memory=True, embedder="hashing")

    result = tank.run(goal_description=goal, definition_of_done=args.dod)

    if args.json:
        import json
        from pathlib import Path
        payload = {
            "goal": goal,
            "definition_of_done": args.dod,
            "status": result.status.value,
            "final_answer": result.final_answer,
            "duration_seconds": result.duration_seconds,
            "receipts": [
                {
                    "actor": r.actor.value if hasattr(r.actor, "value") else str(r.actor),
                    "action": r.action,
                    "success": r.success,
                    "output_summary": r.output_summary,
                }
                for r in result.receipts
            ],
            "plan": {
                "rationale": result.plan.rationale if result.plan else "",
                "steps": [
                    {
                        "type": s.specialist_type,
                        "description": s.description,
                        "status": s.status.value if hasattr(s.status, "value") else str(s.status),
                    }
                    for s in (result.plan.steps if result.plan else [])
                ],
            },
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"JSON geschrieben: {args.json}")

    if args.quiet:
        print(result.final_answer)
    else:
        print("\n" + "=" * 50)
        print(f"Status: {result.status.value} | {result.duration_seconds}s | "
              f"{len(result.receipts)} Receipts")
        if tank.ltm:
            print(tank.ltm.summary())
        print("=" * 50)
        print(result.final_answer)

    if tank.ltm:
        tank.ltm.close()
    return 0 if result.status.value == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
