#!/usr/bin/env python3
"""
TankAI Self-Test — prüft die wichtigsten Pipeline-Bausteine.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _ok(name: str) -> None:
    print(f"  ✓ {name}")


def _fail(name: str, err: Exception) -> None:
    print(f"  ✗ {name}: {err}")


def run_selftest() -> int:
    failed = 0
    print("TankAI Self-Test\n")

    # 1. Imports
    try:
        from tankai import TankAI, get_llm
        from tankai.core.long_term_memory import LongTermMemory
        from tankai.core.tools import ToolRegistry
        from tankai.core.embeddings import get_embedder
        _ok("Imports")
    except Exception as e:
        _fail("Imports", e)
        return 1

    # 2. Embeddings
    try:
        emb = get_embedder("hashing")
        v = emb.embed("Multi-Agenten Test")
        assert v.shape[0] > 0
        _ok(f"Embeddings (dim={emb.dim})")
    except Exception as e:
        _fail("Embeddings", e)
        failed += 1

    # 3. Tools
    try:
        from tankai.core.tools import ToolRegistry
        reg = ToolRegistry()
        reg.register_defaults()
        out = reg.run("calculator", expression="3*7+1")
        assert "22" in out
        assert "sha256" in reg.run("hash", text="tankai")
        assert "Wörter" in reg.run("text_stats", text="eins zwei drei")
        _ok(f"Tools ({len(reg.list_tools())} registered)")
    except Exception as e:
        _fail("Tools", e)
        failed += 1

    # 4. LTM + procedural
    try:
        ltm = LongTermMemory(in_memory=True, embedder="hashing")
        ltm.add_semantic("Multi-Agenten ermöglichen Spezialisierung.", confidence=0.9)
        hits = ltm.retrieve("Spezialisierung Agenten", k=2)
        assert len(hits) >= 1
        _ok("LTM retrieve")
    except Exception as e:
        _fail("LTM", e)
        failed += 1
        ltm = None

    # 5. Full run
    try:
        tank = TankAI(verbose=False, use_ltm=True, parallel=True, enable_tools=True)
        tank.ltm = ltm or LongTermMemory(in_memory=True, embedder="hashing")
        result = tank.run(
            goal_description=(
                "Nenne drei Vorteile und drei Risiken von Multi-Agenten-Systemen "
                "gegenüber einzelnen LLMs."
            ),
            definition_of_done="Jeweils mindestens drei Punkte, klar strukturiert.",
        )
        assert result.final_answer
        assert len(result.receipts) >= 5
        assert result.plan is not None
        # Inhaltlicher Smoke-Check (Mock sollte Multi-Agenten-Thema treffen)
        low = result.final_answer.lower()
        assert "vorteil" in low or "risiko" in low or "multi" in low
        _ok(f"Full run ({result.status.value}, {len(result.receipts)} receipts)")
        print("\n  --- Antwort (Auszug) ---")
        print("  " + result.final_answer[:400].replace("\n", "\n  "))
        print("  ---")
    except Exception as e:
        _fail("Full run", e)
        failed += 1
        result = None

    # 6. JSON export
    try:
        if result:
            export = {
                "status": result.status.value,
                "answer": result.final_answer,
                "receipts": len(result.receipts),
                "plan_steps": len(result.plan.steps) if result.plan else 0,
            }
            path = Path("tankai_selftest_result.json")
            path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
            _ok(f"JSON export → {path}")
    except Exception as e:
        _fail("JSON export", e)
        failed += 1

    if ltm:
        ltm.close()

    print()
    if failed:
        print(f"FEHLGESCHLAGEN: {failed} Check(s)")
        return 1
    print("ALLE CHECKS BESTANDEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_selftest())
