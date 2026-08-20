"""
TankAI Kern-Loop: PLAN → ROUTE → VERIFY → LEARN

Der Commander hält das Ziel und orchestriert den gesamten Ablauf.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..agents.critic import Critic
from ..agents.planner import Planner
from ..agents.specialist import Specialist
from ..agents.synthesizer import Synthesizer
from .llm import BaseLLM, MockLLM
from .memory import Memory
from .long_term_memory import LongTermMemory
from .tools import ToolRegistry, MemorySearchTool
from .run_store import RunStore
from .models import (
    Goal,
    Plan,
    Receipt,
    RunResult,
    TaskStatus,
)


console = Console()


class TankAI:
    """
    Hauptklasse des Prototyps.

    Verwendung:
        tank = TankAI()
        result = tank.run("Mein Ziel ...", definition_of_done="...")
    """

    def __init__(
        self,
        llm: Optional[BaseLLM] = None,
        *,
        max_retries: int = 2,
        verbose: bool = True,
        memory_db: Optional[str] = None,
        use_ltm: bool = False,
        ltm_db: str = "tankai_ltm.db",
        ltm_vectors: str = "tankai_vectors.npz",
        parallel: bool = False,
        enable_tools: bool = True,
        run_store_path: str | None = "tankai_runs.jsonl",
    ) -> None:
        self.llm = llm or MockLLM()
        self.memory = Memory(db_path=memory_db)
        self.ltm: Optional[LongTermMemory] = None
        if use_ltm:
            try:
                self.ltm = LongTermMemory(db_path=ltm_db, vector_path=ltm_vectors)
            except Exception:
                self.ltm = LongTermMemory(in_memory=True)
        self.max_retries = max_retries
        self.verbose = verbose
        self.parallel = parallel
        self.run_store = RunStore(run_store_path) if run_store_path else None

        # Tools
        self.tools = ToolRegistry()
        if enable_tools:
            self.tools.register_defaults(ltm=self.ltm)

        # Agenten
        self.planner = Planner(self.llm)
        self.critic = Critic(self.llm)
        self.synthesizer = Synthesizer(self.llm)

        # Laufzeit-Zustand
        self.receipts: list[Receipt] = []
        self.current_goal: Optional[Goal] = None

    def run(
        self,
        goal_description: str,
        definition_of_done: str = "Eine klare, überprüfbare Antwort liegt vor.",
        constraints: Optional[list[str]] = None,
    ) -> RunResult:
        """
        Führt den kompletten PLAN → ROUTE → VERIFY → LEARN Zyklus aus.
        """
        start = time.perf_counter()

        goal = Goal(
            description=goal_description,
            definition_of_done=definition_of_done,
            constraints=constraints or [],
            status=TaskStatus.PLANNING,
        )
        self.current_goal = goal
        self.receipts = []

        self._log_header(goal)

        # LEARN / RETRIEVAL: Kurzzeit-Memory + Langzeit-Memory
        memory_context = ""
        past = self.memory.search(goal_description, limit=3)
        if past:
            memory_context = "\n\n".join(
                f"[ShortMem | {e.source} | conf={e.confidence:.2f}] {e.content[:250]}"
                for e in past
            )

        if self.ltm:
            hits = self.ltm.retrieve(goal_description, k=5)
            if hits:
                ltm_ctx = self.ltm.format_retrieval_context(hits)
                memory_context = (memory_context + "\n\n" + ltm_ctx).strip()
                if self.verbose:
                    console.print(
                        f"[dim]📚 LTM: {len(hits)} Treffer | {self.ltm.summary()}[/dim]"
                    )
            elif self.verbose and past:
                console.print(f"[dim]📚 {len(past)} ShortMem-Einträge[/dim]")
        elif past and self.verbose:
            console.print(f"[dim]📚 {len(past)} Memory-Einträge geladen[/dim]")

        # 1. PLAN (mit Procedural Memory falls vorhanden)
        procedural_context = ""
        if self.ltm:
            proc_hits = self.ltm.retrieve(
                goal_description,
                k=3,
                memory_types=["procedural"],
                min_score=0.08,
            )
            if proc_hits:
                procedural_context = self.ltm.format_retrieval_context(proc_hits)
                if self.verbose:
                    console.print(
                        f"[dim]🔁 {len(proc_hits)} Procedural-Pattern(s) für den Planner geladen[/dim]"
                    )

        plan, plan_receipt = self.planner.run(goal, procedural_context=procedural_context or None)
        self.receipts.append(plan_receipt)
        self._log_plan(plan)

        # Critic prüft den Plan
        plan_critique, plan_crit_receipt = self.critic.critique_plan(plan, goal)
        self.receipts.append(plan_crit_receipt)

        if not plan_critique.passed and self.verbose:
            console.print(
                Panel(
                    f"[yellow]Plan-Critic hat Issues gefunden[/yellow]\n"
                    + "\n".join(f"• {i}" for i in plan_critique.issues),
                    title="Critic (Plan)",
                )
            )

        # 2. ROUTE + EXECUTE + VERIFY
        step_results: list[tuple[str, str]] = []
        all_critiques = [plan_critique]

        if self.parallel and len(plan.steps) > 1:
            step_results, extra_critiques, extra_receipts = self._run_steps_parallel(
                plan, goal, memory_context
            )
            all_critiques.extend(extra_critiques)
            self.receipts.extend(extra_receipts)
        else:
            for i, step in enumerate(plan.steps, 1):
                self._log_step_start(i, step)

                specialist = Specialist(
                    self.llm,
                    specialist_type=step.specialist_type,
                    tools=self.tools,
                )

                parts = []
                if memory_context:
                    parts.append("### Früheres Wissen aus Memory\n" + memory_context)
                if step_results:
                    parts.append("\n\n".join(r for _, r in step_results))
                context = "\n\n".join(parts)

                result_text, exec_receipt = specialist.run(step, context=context)
                self.receipts.append(exec_receipt)

                critique, crit_receipt = self.critic.critique_result(
                    result_text, step.description, goal
                )
                self.receipts.append(crit_receipt)
                all_critiques.append(critique)

                if critique.passed:
                    step.result = result_text
                    step.status = TaskStatus.COMPLETED
                    step_results.append((step.description, result_text))
                    self.memory.add(
                        content=result_text,
                        source=f"specialist:{step.specialist_type}",
                        validity="valid",
                        confidence=critique.score,
                        related_goal_id=goal.id,
                        metadata={"step_id": step.id},
                    )
                    self._log_step_ok(i, critique.score)
                else:
                    step.status = TaskStatus.FAILED
                    self._log_step_fail(i, critique)

                    for attempt in range(self.max_retries):
                        if self.verbose:
                            console.print(
                                f"  [yellow]→ Retry {attempt + 1}/{self.max_retries}[/yellow]"
                            )
                        result_text, exec_receipt = specialist.run(step, context=context)
                        self.receipts.append(exec_receipt)
                        critique, crit_receipt = self.critic.critique_result(
                            result_text, step.description, goal
                        )
                        self.receipts.append(crit_receipt)
                        all_critiques.append(critique)

                        if critique.passed:
                            step.result = result_text
                            step.status = TaskStatus.COMPLETED
                            step_results.append((step.description, result_text))
                            self.memory.add(
                                content=result_text,
                                source=f"specialist:{step.specialist_type}",
                                validity="valid",
                                confidence=critique.score,
                                related_goal_id=goal.id,
                            )
                            self._log_step_ok(i, critique.score)
                            break
                    else:
                        step.result = result_text
                        step_results.append(
                            (
                                step.description,
                                result_text + "\n\n[Critic: nicht bestanden]",
                            )
                        )
                        self.memory.add(
                            content=result_text,
                            source=f"specialist:{step.specialist_type}",
                            validity="invalid",
                            confidence=critique.score,
                            related_goal_id=goal.id,
                        )

                # 3. SYNTHESIZE
        critiques_summary = self._summarize_critiques(all_critiques)
        final_answer, synth_receipt = self.synthesizer.run(
            goal, step_results, critiques_summary
        )
        self.receipts.append(synth_receipt)

        # Finale Critic-Prüfung der Gesamtheit
        final_critique, final_crit_receipt = self.critic.critique_result(
            final_answer, "Gesamtergebnis / Synthese", goal
        )
        self.receipts.append(final_crit_receipt)
        all_critiques.append(final_critique)

        # 4. LEARN — Kurzzeit + Langzeit
        self.memory.add(
            content=f"Run abgeschlossen für Ziel: {goal.description}\nAntwort: {final_answer[:300]}",
            source="system:run",
            validity="valid" if final_critique.passed else "unknown",
            confidence=final_critique.score,
            related_goal_id=goal.id,
            metadata={"num_receipts": len(self.receipts)},
        )

        duration = time.perf_counter() - start
        status = TaskStatus.COMPLETED if final_critique.passed else TaskStatus.FAILED
        goal.status = status

        result = RunResult(
            goal_id=goal.id,
            final_answer=final_answer,
            status=status,
            plan=plan,
            critiques=all_critiques,
            receipts=self.receipts,
            memory_entries_created=len(self.memory.get_by_goal(goal.id)),
            duration_seconds=round(duration, 3),
        )

        # Langzeit-Memory: Episode speichern + konsolidieren + ggf. Procedure
        if self.ltm:
            run_id = self.ltm.store_episode(result, goal.description)
            created = self.ltm.consolidate(run_id, llm=self.llm)
            if final_critique.passed and plan is not None:
                self.ltm.promote_to_procedure(
                    plan, success_score=final_critique.score, goal_description=goal.description
                )
            # Leichte Retention (nur sehr alte/schwache Einträge)
            try:
                ret_stats = self.ltm.apply_retention(
                    warm_after_days=14,
                    cold_after_days=90,
                    min_confidence_for_hot=0.3,
                )
            except Exception:
                ret_stats = {}
            if self.verbose:
                console.print(
                    f"[dim]🧠 LTM aktualisiert: +{len(created)} Semantic | {self.ltm.summary()}[/dim]"
                )
                if ret_stats.get("to_cold") or ret_stats.get("to_warm"):
                    console.print(f"[dim]   Retention: {ret_stats}[/dim]")

        if self.run_store is not None:
            try:
                self.run_store.append(result, goal.description)
            except Exception:
                pass

        self._log_result(result)
        return result


    def _run_steps_parallel(self, plan, goal, memory_context: str):
        """Führt alle Plan-Schritte parallel aus (gemeinsamer Memory-Kontext)."""
        if self.verbose:
            console.print(f"[cyan]⚡ Parallele Ausführung von {len(plan.steps)} Schritten[/cyan]")

        step_results = []
        critiques = []
        receipts = []
        context = memory_context or ""

        def execute_one(idx_step):
            i, step = idx_step
            specialist = Specialist(
                self.llm, specialist_type=step.specialist_type, tools=self.tools
            )
            result_text, exec_receipt = specialist.run(step, context=context)
            critique, crit_receipt = self.critic.critique_result(
                result_text, step.description, goal
            )
            return i, step, result_text, exec_receipt, critique, crit_receipt

        with ThreadPoolExecutor(max_workers=min(8, len(plan.steps))) as pool:
            futures = {
                pool.submit(execute_one, (i, s)): i
                for i, s in enumerate(plan.steps, 1)
            }
            results_map = {}
            for fut in as_completed(futures):
                i, step, result_text, exec_receipt, critique, crit_receipt = fut.result()
                results_map[i] = (step, result_text, exec_receipt, critique, crit_receipt)

        for i in sorted(results_map.keys()):
            step, result_text, exec_receipt, critique, crit_receipt = results_map[i]
            receipts.extend([exec_receipt, crit_receipt])
            critiques.append(critique)
            self._log_step_start(i, step)
            if critique.passed:
                step.result = result_text
                step.status = TaskStatus.COMPLETED
                step_results.append((step.description, result_text))
                self.memory.add(
                    content=result_text,
                    source=f"specialist:{step.specialist_type}",
                    validity="valid",
                    confidence=critique.score,
                    related_goal_id=goal.id,
                )
                self._log_step_ok(i, critique.score)
            else:
                step.result = result_text
                step.status = TaskStatus.FAILED
                step_results.append((step.description, result_text + "\n\n[Critic: nicht bestanden]"))
                self._log_step_fail(i, critique)

        return step_results, critiques, receipts

    # ────────────────────────── Logging-Helfer ──────────────────────────


    def _log_header(self, goal: Goal) -> None:
        if not self.verbose:
            return
        console.print()
        console.print(
            Panel(
                f"[bold]{goal.description}[/bold]\n\n"
                f"[dim]Definition of Done:[/dim] {goal.definition_of_done}",
                title="🎯 TankAI — Neues Ziel",
                border_style="cyan",
            )
        )

    def _log_plan(self, plan: Plan) -> None:
        if not self.verbose:
            return
        table = Table(title="📋 Plan", show_header=True, header_style="bold")
        table.add_column("#", style="dim", width=3)
        table.add_column("Typ", style="magenta")
        table.add_column("Beschreibung")
        for i, s in enumerate(plan.steps, 1):
            table.add_row(str(i), s.specialist_type, s.description)
        console.print(table)
        console.print(f"[dim]Rationale: {plan.rationale}[/dim]\n")

    def _log_step_start(self, i: int, step) -> None:
        if not self.verbose:
            return
        console.print(f"[bold cyan]▶ Schritt {i}[/bold cyan] [{step.specialist_type}] {step.description}")

    def _log_step_ok(self, i: int, score: float) -> None:
        if not self.verbose:
            return
        console.print(f"  [green]✓ bestanden[/green] (score={score:.2f})")

    def _log_step_fail(self, i: int, critique) -> None:
        if not self.verbose:
            return
        console.print(f"  [red]✗ nicht bestanden[/red] (score={critique.score:.2f})")
        for issue in critique.issues:
            console.print(f"    • {issue}")

    def _log_result(self, result: RunResult) -> None:
        if not self.verbose:
            return
        status_style = "green" if result.status == TaskStatus.COMPLETED else "red"
        console.print()
        console.print(
            Panel(
                result.final_answer,
                title=f"[{status_style}]Finale Antwort[/{status_style}]",
                border_style=status_style,
            )
        )
        console.print(
            f"[dim]Status: {result.status.value} | "
            f"Dauer: {result.duration_seconds}s | "
            f"Receipts: {len(result.receipts)} | "
            f"Memory-Einträge: {result.memory_entries_created}[/dim]"
        )
        console.print(f"[dim]{self.memory.summary()}[/dim]\n")

    def _summarize_critiques(self, critiques) -> str:
        issues = []
        for c in critiques:
            issues.extend(c.issues)
        if not issues:
            return "Keine kritischen Issues gefunden."
        return "Gefundene Issues:\n" + "\n".join(f"- {i}" for i in issues[:8])
