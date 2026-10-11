from __future__ import annotations

import json

from tankai.agents.planner import Planner
from tankai.agents.critic import Critic
from tankai.agents.synthesizer import Synthesizer
from tankai.core.llm import BaseLLM
from tankai.core.models import Goal


class RecordingLLM(BaseLLM):
    provider_name = "test"
    model_name = "goal-analysis-v1"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt: str, *, system: str = "", **kwargs) -> str:
        self.calls.append((prompt, system))
        return self.response


def workshop_payload() -> dict[str, object]:
    return {
        "rationale": "Compare mechanisms before committing the small budget.",
        "reused_pattern": False,
        "goal_analysis": {
            "desired_outcome": "Mehr junge Erwachsene besuchen messbar die Veranstaltungen.",
            "analysis_status": "workshop",
            "requested_method": None,
            "alternatives_warranted": True,
            "alternatives_reason": "Mehrere risikoarme Akquisewege sind unter dem Budget möglich.",
            "options": [
                {
                    "option_id": "peer-hosts",
                    "title": "Peer-Gastgeber",
                    "approach": "Junge lokale Gruppen gestalten einen Themenabend mit.",
                    "benefit": "Bestehende Gemeinschaften bringen glaubwürdige Reichweite.",
                    "tradeoff": "Koordination benötigt Vorlauf.",
                },
                {
                    "option_id": "campus-pop-up",
                    "title": "Campus-Pop-up",
                    "approach": "Eine kurze Vorschau findet am Berufskolleg statt.",
                    "benefit": "Das Format erreicht die Zielgruppe direkt.",
                    "tradeoff": "Erfordert eine externe Ortsfreigabe.",
                },
                {
                    "option_id": "after-hours",
                    "title": "Später Termin",
                    "approach": "Ein bestehendes Format wird abends getestet.",
                    "benefit": "Geringster organisatorischer Zusatzaufwand.",
                    "tradeoff": "Der Termin allein löst das Themenproblem nicht.",
                },
            ],
            "critical_uncertainty": "Ob Thema oder Uhrzeit die größere Teilnahmehürde ist.",
            "recommended_option_id": "peer-hosts",
            "smallest_experiment": "Einen Peer-Abend binnen zwei Wochen ankündigen und Anmeldungen zählen.",
        },
        "steps": [
            {
                "description": "Varianten gegen Budget und Ziel vergleichen",
                "specialist_type": "analysis",
                "expected_output": "Begründete Auswahl und Versuchsplan",
            }
        ],
    }


def test_planner_builds_bounded_workshop_without_extra_model_call() -> None:
    llm = RecordingLLM(json.dumps(workshop_payload()))
    goal = Goal(
        description="Entwickle einen Weg zu mehr jungen Veranstaltungsgästen.",
        definition_of_done="Varianten, Empfehlung, Unsicherheit und Kleinversuch liegen vor.",
        constraints=["höchstens 150 EUR", "keine bezahlte Werbung"],
    )

    plan, receipt = Planner(llm).run(goal)

    assert len(llm.calls) == 1
    assert "höchstens 150 EUR" in llm.calls[0][0]
    assert plan.goal_analysis is not None
    assert plan.goal_analysis.alternatives_warranted is True
    assert plan.goal_analysis.analysis_status == "workshop"
    assert len(plan.goal_analysis.options) == 3
    assert plan.goal_analysis.recommended_option_id == "peer-hosts"
    assert plan.goal_analysis.critical_uncertainty
    assert plan.goal_analysis.smallest_experiment
    assert receipt.details["alternative_count"] == 3
    assert receipt.details["alternatives_warranted"] is True


def test_trivial_goal_does_not_invent_alternatives() -> None:
    payload = workshop_payload()
    payload["goal_analysis"] = {
        "desired_outcome": "Die Zahl 7 wird ausgegeben.",
        "requested_method": "Python verwenden",
        "alternatives_warranted": False,
        "alternatives_reason": "Direkte, vollständig vorgegebene Aufgabe.",
        "options": [],
        "critical_uncertainty": None,
        "recommended_option_id": None,
        "smallest_experiment": None,
    }
    llm = RecordingLLM(json.dumps(payload))

    plan, _ = Planner(llm).run(
        Goal(
            description="Gib die Zahl 7 mit Python aus.",
            definition_of_done="Die Ausgabe ist exakt 7.",
        )
    )

    assert plan.goal_analysis is not None
    assert plan.goal_analysis.alternatives_warranted is False
    assert plan.goal_analysis.options == []
    assert plan.goal_analysis.analysis_status == "direct"


def test_incomplete_workshop_fails_closed_to_direct_goal() -> None:
    payload = workshop_payload()
    payload["goal_analysis"]["recommended_option_id"] = "missing"  # type: ignore[index]
    llm = RecordingLLM(json.dumps(payload))
    goal = Goal(
        description="Entwickle eine begrenzte Testidee.",
        definition_of_done="Ein überprüfbarer Vorschlag liegt vor.",
    )

    plan, receipt = Planner(llm).run(goal)

    assert plan.goal_analysis is not None
    assert plan.goal_analysis.alternatives_warranted is False
    assert plan.goal_analysis.options == []
    assert plan.goal_analysis.analysis_status == "fallback"
    assert plan.goal_analysis.desired_outcome == goal.description
    assert receipt.details["alternative_count"] == 0

    overlong = workshop_payload()
    options = overlong["goal_analysis"]["options"]  # type: ignore[index]
    options.append(dict(options[0], option_id="fourth"))  # type: ignore[union-attr, index]
    second_plan, _ = Planner(RecordingLLM(json.dumps(overlong))).run(goal)
    assert second_plan.goal_analysis is not None
    assert second_plan.goal_analysis.analysis_status == "fallback"
    assert second_plan.goal_analysis.options == []


def test_synthesizer_receives_recommendation_and_experiment() -> None:
    planner_llm = RecordingLLM(json.dumps(workshop_payload()))
    goal = Goal(
        description="Entwickle eine begrenzte Testidee.",
        definition_of_done="Ein überprüfbarer Vorschlag liegt vor.",
    )
    plan, _ = Planner(planner_llm).run(goal)
    synthesis_llm = RecordingLLM("Final")

    Synthesizer(synthesis_llm).run(
        goal,
        [("Analyse", "Ergebnis")],
        goal_analysis=plan.goal_analysis,
    )

    prompt = synthesis_llm.calls[0][0]
    assert "Empfohlene Option: peer-hosts" in prompt
    assert "Entscheidende Unsicherheit" in prompt
    assert "Peer-Abend binnen zwei Wochen" in prompt


def test_critic_receives_bounded_workshop_contract() -> None:
    planner_llm = RecordingLLM(json.dumps(workshop_payload()))
    goal = Goal(
        description="Entwickle eine begrenzte Testidee.",
        definition_of_done="Ein überprüfbarer Vorschlag liegt vor.",
    )
    plan, _ = Planner(planner_llm).run(goal)
    critic_llm = RecordingLLM(
        json.dumps({"passed": True, "score": 0.9, "issues": [], "suggestions": []})
    )

    critique, _ = Critic(critic_llm).critique_plan(plan, goal)

    assert critique.passed is True
    prompt = critic_llm.calls[0][0]
    assert "Analyse-Status: workshop" in prompt
    assert "Empfehlung: peer-hosts" in prompt
    assert "Kleinster Versuch" in prompt
