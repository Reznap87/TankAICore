"""
TankAI Kern-Loop: PLAN → ROUTE → VERIFY → LEARN

Der Commander hält das Ziel und orchestriert den gesamten Ablauf.
"""

from __future__ import annotations

import re
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
from .llm import BaseLLM, BudgetedLLM, LLMCallBudget, get_llm, llm_identity
from .memory import Memory
from .long_term_memory import LongTermMemory
from .tools import ToolRegistry
from .run_store import RunStore
from .models import (
    Critique,
    Goal,
    Plan,
    Receipt,
    Role,
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
        critic_llm: Optional[BaseLLM] = None,
        require_independent_critic: bool = False,
        require_research_evidence: bool = True,
        max_retries: int = 2,
        max_llm_calls_per_run: int = 40,
        verbose: bool = True,
        memory_db: Optional[str] = None,
        use_ltm: bool = False,
        ltm_db: str = "tankai_ltm.db",
        ltm_vectors: str = "tankai_vectors.npz",
        parallel: bool = False,
        enable_tools: bool = True,
        strict_web_research: bool = False,
        run_store_path: str | None = "tankai_runs.jsonl",
    ) -> None:
        main_llm = llm if llm is not None else get_llm()
        raw_critic_llm = critic_llm if critic_llm is not None else main_llm
        self.main_llm_identity = llm_identity(main_llm)
        self.critic_llm_identity = llm_identity(raw_critic_llm)
        self.critic_independent = self.main_llm_identity != self.critic_llm_identity
        self.llm_call_budget = LLMCallBudget(max_llm_calls_per_run)
        self.llm = BudgetedLLM(main_llm, self.llm_call_budget)
        self.critic_llm = (
            self.llm
            if raw_critic_llm is main_llm
            else BudgetedLLM(raw_critic_llm, self.llm_call_budget)
        )
        self.require_independent_critic = bool(require_independent_critic)
        if self.require_independent_critic and not self.critic_independent:
            raise RuntimeError(
                "Unabhängiger Critic erforderlich, aber Hauptmodell und Critic haben dieselbe "
                f"Identität: {self.main_llm_identity}"
            )
        self.require_research_evidence = bool(require_research_evidence)
        self.memory = Memory(db_path=memory_db)
        self.ltm: Optional[LongTermMemory] = None
        if use_ltm:
            self.ltm = LongTermMemory(db_path=ltm_db, vector_path=ltm_vectors)
        self.max_retries = max_retries
        self.verbose = verbose
        self.parallel = parallel
        self.run_store = RunStore(run_store_path) if run_store_path else None

        # Tools
        self.tools = ToolRegistry()
        if enable_tools:
            self.tools.register_defaults(
                ltm=self.ltm,
                enable_web_research=True,
                strict_web_research=strict_web_research,
            )

        # Agenten
        self.planner = Planner(self.llm)
        self.critic = Critic(self.critic_llm)
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
        self.llm_call_budget.reset()

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

        plan, plan_receipt = self.planner.run(
            goal, procedural_context=procedural_context or None
        )
        self.receipts.append(plan_receipt)
        self._log_plan(plan)

        # Critic prüft den Plan. Abgelehnte Pläne werden mit konkretem Feedback repariert.
        plan_critiques = []
        plan_critique, plan_crit_receipt = self.critic.critique_plan(plan, goal)
        self.receipts.append(plan_crit_receipt)
        plan_critiques.append(plan_critique)
        for repair_attempt in range(self.max_retries):
            if plan_critique.passed:
                break
            feedback = "\n".join(
                [*(f"Issue: {item}" for item in plan_critique.issues),
                 *(f"Vorschlag: {item}" for item in plan_critique.suggestions)]
            ) or "Der Plan erfüllt die Definition of Done nicht."
            if self.verbose:
                console.print(
                    Panel(
                        f"[yellow]Plan abgelehnt — Reparatur {repair_attempt + 1}/{self.max_retries}[/yellow]\n"
                        + feedback,
                        title="Critic (Plan)",
                    )
                )
            plan, plan_receipt = self.planner.run(
                goal,
                procedural_context=procedural_context or None,
                critic_feedback=feedback,
            )
            plan.version = repair_attempt + 2
            self.receipts.append(plan_receipt)
            plan_critique, plan_crit_receipt = self.critic.critique_plan(plan, goal)
            self.receipts.append(plan_crit_receipt)
            plan_critiques.append(plan_critique)
        plan_gate_passed = bool(plan_critique.passed)
        self._log_plan(plan)

        # 2. ROUTE + EXECUTE + VERIFY
        step_results: list[tuple[str, str]] = []
        all_critiques = list(plan_critiques)

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

                result_text, exec_receipt, critique, crit_receipt = self._execute_and_critique(
                    specialist, step, goal, context
                )
                self.receipts.append(exec_receipt)

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
                        feedback_lines = [
                            *(f"Issue: {item}" for item in critique.issues),
                            *(f"Vorschlag: {item}" for item in critique.suggestions),
                        ]
                        retry_context = context + "\n\n### Verbindliches Critic-Feedback für den Retry\n" + (
                            "\n".join(feedback_lines) or "Ergebnis präzisieren und Definition of Done vollständig erfüllen."
                        )
                        result_text, exec_receipt, critique, crit_receipt = self._execute_and_critique(
                            specialist, step, goal, retry_context
                        )
                        self.receipts.append(exec_receipt)
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
        final_answer = self._append_source_catalog(final_answer)
        synth_receipt.output_summary = final_answer[:300]
        self.receipts.append(synth_receipt)

        # Finale Critic-Prüfung der Gesamtheit
        final_critique, final_crit_receipt = self.critic.critique_result(
            final_answer, "Gesamtergebnis / Synthese", goal
        )

        # Deterministische Quellenprüfung der Synthese. Ein LLM-Critic darf fehlende
        # Provenance nicht versehentlich durchwinken.
        final_critique = self._apply_final_research_gate(final_critique, final_answer, plan)
        final_critique = self._apply_execution_gate(
            final_critique, plan, plan_gate_passed=plan_gate_passed
        )
        final_crit_receipt.success = final_critique.passed
        final_crit_receipt.output_summary = (
            f"passed={final_critique.passed}, score={final_critique.score}"
        )
        final_crit_receipt.details["deterministic_research_gate"] = True
        final_crit_receipt.details["deterministic_execution_gate"] = True
        final_crit_receipt.details["plan_gate_passed"] = plan_gate_passed
        final_crit_receipt.details["failed_step_ids"] = [
            step.id for step in plan.steps if step.status != TaskStatus.COMPLETED
        ]
        self.receipts.append(final_crit_receipt)
        all_critiques.append(final_critique)

        # 4. LEARN — Kurzzeit + Langzeit
        main_simulation = bool(getattr(self.llm, "is_simulation", False))
        critic_simulation = bool(getattr(self.critic_llm, "is_simulation", False))
        any_simulation = main_simulation or critic_simulation
        if main_simulation and critic_simulation:
            execution_mode = "simulation"
        elif any_simulation:
            execution_mode = "mixed"
        else:
            execution_mode = "live"

        self.memory.add(
            content=f"Run abgeschlossen für Ziel: {goal.description}\nAntwort: {final_answer[:300]}",
            source="system:run",
            validity="valid" if final_critique.passed and not any_simulation else "unknown",
            confidence=final_critique.score if not any_simulation else min(final_critique.score, 0.25),
            related_goal_id=goal.id,
            metadata={
                "num_receipts": len(self.receipts),
                "main_llm": self.main_llm_identity,
                "critic_llm": self.critic_llm_identity,
                "critic_independent": self.critic_independent,
            },
        )

        duration = time.perf_counter() - start
        if any_simulation:
            status = TaskStatus.SIMULATED
        else:
            status = TaskStatus.COMPLETED if final_critique.passed else TaskStatus.FAILED
        goal.status = status

        source_ids, source_urls = self._research_provenance()
        failed_step_ids = [
            step.id for step in plan.steps if step.status != TaskStatus.COMPLETED
        ]
        result = RunResult(
            goal_id=goal.id,
            final_answer=final_answer,
            status=status,
            definition_of_done=goal.definition_of_done,
            execution_mode=execution_mode,
            main_llm_identity=self.main_llm_identity,
            critic_llm_identity=self.critic_llm_identity,
            critic_independent=self.critic_independent,
            verification_passed=final_critique.passed,
            release_ready=final_critique.passed and not any_simulation,
            plan_gate_passed=plan_gate_passed,
            failed_step_ids=failed_step_ids,
            web_research_provider=self.tools.web_research_status(),
            source_ids=source_ids,
            source_urls=source_urls,
            plan=plan,
            critiques=all_critiques,
            receipts=self.receipts,
            memory_entries_created=len(self.memory.get_by_goal(goal.id)),
            duration_seconds=round(duration, 3),
        )

        # Langzeit-Memory: Episode speichern + konsolidieren + ggf. Procedure
        if self.ltm:
            run_id = self.ltm.store_episode(result, goal.description)
            created = []
            if not any_simulation:
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

        budget_snapshot = self.llm_call_budget.snapshot()
        budget_receipt = Receipt(
            action="llm_call_budget",
            actor=Role.COMMANDER,
            input_summary=goal.description,
            output_summary=(
                f"used={budget_snapshot['used']}/{budget_snapshot['max']}, "
                f"remaining={budget_snapshot['remaining']}"
            ),
            success=True,
            details={
                **budget_snapshot,
                "shared_main_and_critic": True,
                "reset_scope": "run",
            },
        )
        self.receipts.append(budget_receipt)
        result.receipts = list(self.receipts)

        if self.run_store is not None:
            try:
                self.run_store.append(result, goal.description)
            except Exception:
                pass

        self._log_result(result)
        return result


    def _execute_and_critique(self, specialist, step, goal, context: str):
        """Kapselt Agentenfehler, damit ein einzelner Schritt den Run nicht unkontrolliert abbricht."""
        try:
            result_text, exec_receipt = specialist.run(step, context=context, goal=goal)
        except Exception as exc:
            result_text = "[Specialist-Ausführung fehlgeschlagen]"
            exec_receipt = Receipt(
                action=f"execute_step:{step.specialist_type}",
                actor=Role.SPECIALIST,
                input_summary=step.description,
                output_summary=result_text,
                success=False,
                details={"step_id": step.id, "error_type": type(exc).__name__},
            )
            critique = Critique(
                target_id=step.id,
                passed=False,
                score=0.0,
                issues=["Specialist-Ausführung fehlgeschlagen"],
                suggestions=["Fehlerursache beheben und Schritt erneut ausführen"],
            )
            crit_receipt = Receipt(
                action="critique_result",
                actor=Role.CRITIC,
                input_summary=step.description,
                output_summary="passed=False, score=0.0",
                success=False,
                details={"critique_id": critique.id, "reason": "specialist_error"},
            )
            return result_text, exec_receipt, critique, crit_receipt

        try:
            critique, crit_receipt = self.critic.critique_result(
                result_text, step.description, goal
            )
        except Exception as exc:
            critique = Critique(
                target_id=step.id,
                passed=False,
                score=0.0,
                issues=["Critic-Prüfung fehlgeschlagen"],
                suggestions=["Critic-Konfiguration prüfen und Ergebnis erneut verifizieren"],
            )
            crit_receipt = Receipt(
                action="critique_result",
                actor=Role.CRITIC,
                input_summary=step.description,
                output_summary="passed=False, score=0.0",
                success=False,
                details={"critique_id": critique.id, "error_type": type(exc).__name__},
            )

        critique = self._apply_step_research_gate(
            critique, step, result_text, exec_receipt
        )
        crit_receipt.success = critique.passed
        crit_receipt.output_summary = f"passed={critique.passed}, score={critique.score}"
        crit_receipt.details["deterministic_research_gate"] = (
            step.specialist_type == "research" and self.require_research_evidence
        )
        return result_text, exec_receipt, critique, crit_receipt

    @staticmethod
    def _citation_ids(text: str) -> set[str]:
        return set(re.findall(r"\[(SRC-[A-F0-9]{8})\]", text or "", flags=re.IGNORECASE))

    def _apply_step_research_gate(
        self,
        critique: Critique,
        step,
        result_text: str,
        exec_receipt: Receipt,
    ) -> Critique:
        if step.specialist_type != "research" or not self.require_research_evidence:
            return critique

        expected_ids = {
            str(item).upper()
            for item in exec_receipt.details.get("source_ids", [])
            if isinstance(item, str)
        }
        cited_ids = {item.upper() for item in self._citation_ids(result_text)}
        issues = list(critique.issues)
        suggestions = list(critique.suggestions)
        passed = critique.passed
        score = critique.score

        research_error = str(exec_receipt.details.get("research_error") or "").strip()
        if research_error or not expected_ids:
            passed = False
            score = min(score, 0.2)
            issues.append(
                "Research-Schritt besitzt keine erfolgreich abgerufenen Webquellen"
                + (f": {research_error}" if research_error else "")
            )
            suggestions.append(
                "Suchanbieter und API-Key konfigurieren und den Research-Schritt erneut ausführen"
            )
        else:
            valid = cited_ids & expected_ids
            unknown = cited_ids - expected_ids
            if not valid:
                passed = False
                score = min(score, 0.35)
                issues.append("Research-Ergebnis zitiert keine der tatsächlich abgerufenen Quellen-IDs")
                suggestions.append("Faktische Aussagen mit den bereitgestellten [SRC-…]-IDs belegen")
            if unknown:
                passed = False
                score = min(score, 0.25)
                issues.append(
                    "Research-Ergebnis enthält unbekannte Quellen-IDs: "
                    + ", ".join(sorted(unknown))
                )
                suggestions.append("Nur Quellen-IDs aus dem Research-Receipt verwenden")

        return Critique(
            id=critique.id,
            target_id=critique.target_id,
            passed=passed,
            issues=list(dict.fromkeys(issues)),
            suggestions=list(dict.fromkeys(suggestions)),
            score=score,
            timestamp=critique.timestamp,
        )

    def _source_catalog(self) -> dict[str, dict[str, str]]:
        catalog: dict[str, dict[str, str]] = {}
        for receipt in self.receipts:
            if receipt.action != "execute_step:research":
                continue
            for item in receipt.details.get("sources", []):
                if not isinstance(item, dict):
                    continue
                source_id = str(item.get("source_id") or "").upper()
                url = str(item.get("url") or "").strip()
                if not source_id or not url:
                    continue
                catalog.setdefault(
                    source_id,
                    {
                        "source_id": source_id,
                        "title": str(item.get("title") or "").strip(),
                        "url": url,
                    },
                )
        return catalog

    def _append_source_catalog(self, final_answer: str) -> str:
        cited = {item.upper() for item in self._citation_ids(final_answer)}
        if not cited:
            return final_answer
        catalog = self._source_catalog()
        selected = [catalog[item] for item in sorted(cited) if item in catalog]
        if not selected:
            return final_answer
        missing = [source for source in selected if source["url"] not in final_answer]
        if not missing:
            return final_answer
        lines = [final_answer.rstrip(), "", "### Quellen"]
        for source in missing:
            title = source["title"] or "Quelle"
            lines.append(f"[{source['source_id']}] {title} — {source['url']}")
        return "\n".join(lines).strip()

    def _research_provenance(self) -> tuple[list[str], list[str]]:
        source_ids: list[str] = []
        source_urls: list[str] = []
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        for receipt in self.receipts:
            if receipt.action != "execute_step:research":
                continue
            for item in receipt.details.get("source_ids", []):
                value = str(item).upper()
                if value and value not in seen_ids:
                    seen_ids.add(value)
                    source_ids.append(value)
            for item in receipt.details.get("source_urls", []):
                value = str(item)
                if value and value not in seen_urls:
                    seen_urls.add(value)
                    source_urls.append(value)
        return source_ids, source_urls

    def _apply_final_research_gate(
        self, critique: Critique, final_answer: str, plan: Plan | None
    ) -> Critique:
        if not self.require_research_evidence or plan is None:
            return critique
        research_steps = [step for step in plan.steps if step.specialist_type == "research"]
        if not research_steps:
            return critique

        expected_ids: set[str] = set()
        research_errors: list[str] = []
        for receipt in self.receipts:
            if receipt.action != "execute_step:research":
                continue
            expected_ids.update(
                str(item).upper()
                for item in receipt.details.get("source_ids", [])
                if isinstance(item, str)
            )
            error = str(receipt.details.get("research_error") or "").strip()
            if error:
                research_errors.append(error)

        cited_ids = {item.upper() for item in self._citation_ids(final_answer)}
        issues = list(critique.issues)
        suggestions = list(critique.suggestions)
        passed = critique.passed
        score = critique.score

        if not expected_ids:
            passed = False
            score = min(score, 0.2)
            issues.append("Finale Antwort beruht auf einem Research-Plan ohne verifizierbare Webquellen")
            if research_errors:
                issues.append("Webrecherche-Fehler: " + "; ".join(research_errors[:3]))
            suggestions.append("Webrecherche erfolgreich ausführen, bevor eine finale Faktenantwort freigegeben wird")
        else:
            valid = cited_ids & expected_ids
            unknown = cited_ids - expected_ids
            if not valid:
                passed = False
                score = min(score, 0.35)
                issues.append("Finale Antwort enthält keine gültige Quellen-ID aus den Research-Receipts")
                suggestions.append("Belege aus den Research-Ergebnissen in der Synthese erhalten")
            if unknown:
                passed = False
                score = min(score, 0.25)
                issues.append("Finale Antwort enthält unbekannte Quellen-IDs: " + ", ".join(sorted(unknown)))
                suggestions.append("Nur tatsächlich abgerufene Quellen-IDs zitieren")

        return Critique(
            id=critique.id,
            target_id=critique.target_id,
            passed=passed,
            issues=list(dict.fromkeys(issues)),
            suggestions=list(dict.fromkeys(suggestions)),
            score=score,
            timestamp=critique.timestamp,
        )


    def _apply_execution_gate(
        self,
        critique: Critique,
        plan: Plan | None,
        *,
        plan_gate_passed: bool,
    ) -> Critique:
        """Blockiert die Freigabe bei abgelehntem Plan oder ungeklärten Schritten."""
        issues = list(critique.issues)
        suggestions = list(critique.suggestions)
        passed = critique.passed
        score = critique.score

        if not plan_gate_passed:
            passed = False
            score = min(score, 0.15)
            issues.append(
                "Der finale Plan wurde vom Critic auch nach den Reparaturversuchen nicht freigegeben"
            )
            suggestions.append(
                "Planfehler beheben, bevor Specialist-Schritte oder die Synthese freigegeben werden"
            )

        failed_steps = [] if plan is None else [
            step for step in plan.steps if step.status != TaskStatus.COMPLETED
        ]
        if failed_steps:
            passed = False
            score = min(score, 0.2)
            issues.append(
                "Nicht erfolgreich verifizierte Plan-Schritte: "
                + ", ".join(step.description[:120] for step in failed_steps)
            )
            suggestions.append(
                "Alle Plan-Schritte erfolgreich ausführen und verifizieren, bevor das Gesamtergebnis freigegeben wird"
            )

        return Critique(
            id=critique.id,
            target_id=critique.target_id,
            passed=passed,
            issues=list(dict.fromkeys(issues)),
            suggestions=list(dict.fromkeys(suggestions)),
            score=score,
            timestamp=critique.timestamp,
        )


    def _run_steps_parallel(self, plan, goal, memory_context: str):
        """Führt unabhängige Plan-Schritte parallel aus und behält Retry-Grenzen bei."""
        if self.verbose:
            console.print(f"[cyan]⚡ Parallele Ausführung von {len(plan.steps)} Schritten[/cyan]")

        step_results: list[tuple[str, str]] = []
        critiques: list[Critique] = []
        receipts: list[Receipt] = []
        context = memory_context or ""

        def execute_one(idx_step):
            i, step = idx_step
            specialist = Specialist(
                self.llm, specialist_type=step.specialist_type, tools=self.tools
            )
            local_receipts: list[Receipt] = []
            local_critiques: list[Critique] = []
            retry_context = context
            result_text = ""
            critique = Critique(target_id=step.id, passed=False, score=0.0)

            for attempt in range(self.max_retries + 1):
                result_text, exec_receipt, critique, crit_receipt = self._execute_and_critique(
                    specialist, step, goal, retry_context
                )
                local_receipts.extend([exec_receipt, crit_receipt])
                local_critiques.append(critique)
                if critique.passed:
                    break
                feedback_lines = [
                    *(f"Issue: {item}" for item in critique.issues),
                    *(f"Vorschlag: {item}" for item in critique.suggestions),
                ]
                retry_context = context + (
                    "\n\n### Verbindliches Critic-Feedback für den Retry\n"
                    + (
                        "\n".join(feedback_lines)
                        or "Ergebnis präzisieren und Definition of Done vollständig erfüllen."
                    )
                )

            return i, step, result_text, critique, local_receipts, local_critiques

        with ThreadPoolExecutor(max_workers=min(8, len(plan.steps))) as pool:
            futures = {
                pool.submit(execute_one, (i, step)): i
                for i, step in enumerate(plan.steps, 1)
            }
            results_map = {}
            for future in as_completed(futures):
                try:
                    item = future.result()
                except Exception as exc:
                    i = futures[future]
                    step = plan.steps[i - 1]
                    critique = Critique(
                        target_id=step.id,
                        passed=False,
                        score=0.0,
                        issues=["Parallele Ausführung ist unerwartet fehlgeschlagen"],
                        suggestions=["Fehlerursache beheben und Schritt erneut ausführen"],
                    )
                    failure_receipt = Receipt(
                        action=f"execute_step:{step.specialist_type}",
                        actor=Role.SPECIALIST,
                        input_summary=step.description,
                        output_summary="[Parallele Ausführung fehlgeschlagen]",
                        success=False,
                        details={"step_id": step.id, "error_type": type(exc).__name__},
                    )
                    item = (i, step, "[Parallele Ausführung fehlgeschlagen]", critique, [failure_receipt], [critique])
                results_map[item[0]] = item[1:]

        for i in sorted(results_map):
            step, result_text, critique, local_receipts, local_critiques = results_map[i]
            receipts.extend(local_receipts)
            critiques.extend(local_critiques)
            self._log_step_start(i, step)
            step.result = result_text
            if critique.passed:
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
                self.memory.add(
                    content=result_text,
                    source=f"specialist:{step.specialist_type}",
                    validity="invalid",
                    confidence=critique.score,
                    related_goal_id=goal.id,
                    metadata={"step_id": step.id},
                )
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
        if result.status == TaskStatus.COMPLETED:
            status_style = "green"
        elif result.status == TaskStatus.SIMULATED:
            status_style = "yellow"
        else:
            status_style = "red"
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
