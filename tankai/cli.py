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
import os
import sys


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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
        help="LLM-Provider (sonst muss TANKAI_LLM gesetzt sein)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Expliziter Modellname des Hauptproviders",
    )
    parser.add_argument(
        "--critic-llm",
        default=None,
        choices=["mock", "openai", "anthropic", "echo"],
        help="Separater Critic-Provider (sonst TANKAI_CRITIC_LLM oder Hauptmodell)",
    )
    parser.add_argument(
        "--critic-model",
        default=None,
        help="Separates Critic-Modell",
    )
    parser.add_argument(
        "--require-independent-critic",
        action="store_true",
        help="Abbruch, falls Critic und Hauptmodell dieselbe Provider-/Modellidentität haben",
    )
    parser.add_argument(
        "--strict-web-research",
        action="store_true",
        help="Abbruch beim Start, wenn kein funktionierender Suchanbieter konfiguriert ist",
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
            mode = r.get("execution_mode", "unknown")
            print(f"{r.get('ts','?')[:19]}  {r.get('status')}  {mode}  {r.get('goal','')[:70]}")
        return 0

    if args.setup:
        from tankai.core.llm import describe_llm_setup
        from tankai.core.web_research import describe_web_research_setup
        print(describe_llm_setup())
        print(describe_web_research_setup())
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
    from tankai.core.llm import get_critic_llm
    llm_kwargs = {}
    if args.model:
        llm_kwargs["model"] = args.model
    try:
        llm = get_llm(args.llm, **llm_kwargs)
        provider = args.llm or "env"
        if not args.quiet:
            mode = "simulation" if llm.is_simulation else "live"
            print(f"LLM: {type(llm).__name__} (provider={provider}, mode={mode})")
    except Exception as e:
        print(f"LLM-Konfigurationsfehler: {e}", file=sys.stderr)
        return 3

    try:
        critic_llm = llm
        if args.critic_llm:
            critic_kwargs = {}
            if args.critic_model:
                critic_kwargs["model"] = args.critic_model
            critic_llm = get_llm(args.critic_llm, **critic_kwargs)
        elif os.environ.get("TANKAI_CRITIC_LLM", "").strip():
            if args.critic_model:
                os.environ["TANKAI_CRITIC_MODEL"] = args.critic_model
            critic_llm = get_critic_llm(default=llm)

        tank = TankAI(
            llm=llm,
            critic_llm=critic_llm,
            require_independent_critic=(
                args.require_independent_critic
                or _env_bool("TANKAI_REQUIRE_INDEPENDENT_CRITIC", False)
            ),
            require_research_evidence=_env_bool(
                "TANKAI_REQUIRE_RESEARCH_EVIDENCE", True
            ),
            verbose=not args.quiet,
            use_ltm=True,
            parallel=args.parallel,
            enable_tools=not args.no_tools,
            strict_web_research=(
                args.strict_web_research
                or _env_bool("TANKAI_STRICT_WEB_RESEARCH", False)
            ),
        )
    except Exception as e:
        print(f"TankAI-Konfigurationsfehler: {e}", file=sys.stderr)
        return 3

    if not args.quiet:
        print(
            f"Critic: {tank.critic_llm_identity} "
            f"(independent={tank.critic_independent}) | "
            f"Web research={tank.tools.web_research_status()}"
        )
    result = tank.run(goal_description=goal, definition_of_done=args.dod)

    if args.json:
        import json
        from pathlib import Path
        payload = {
            "goal": goal,
            "definition_of_done": args.dod,
            "status": result.status.value,
            "execution_mode": result.execution_mode,
            "main_llm_identity": result.main_llm_identity,
            "critic_llm_identity": result.critic_llm_identity,
            "critic_independent": result.critic_independent,
            "verification_passed": result.verification_passed,
            "release_ready": result.release_ready,
            "plan_gate_passed": result.plan_gate_passed,
            "failed_step_ids": result.failed_step_ids,
            "web_research_provider": result.web_research_provider,
            "source_ids": result.source_ids,
            "source_urls": result.source_urls,
            "final_answer": result.final_answer,
            "duration_seconds": result.duration_seconds,
            "receipts": [
                {
                    "actor": r.actor.value if hasattr(r.actor, "value") else str(r.actor),
                    "action": r.action,
                    "success": r.success,
                    "output_summary": r.output_summary,
                    "details": r.details,
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
    return 0 if result.status.value == "completed" else (4 if result.status.value == "simulated" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
