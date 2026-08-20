"""
Abstrakte LLM-Schnittstelle + Mock-Implementierung.

In einer echten Deployment würde man hier Provider-Adapter
(OpenAI, Anthropic, Ollama, etc.) einhängen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseLLM(ABC):
    """Abstrakte Basis für alle LLM-Aufrufe."""

    provider_name = "unknown"
    model_name = "unknown"
    is_simulation = False

    @abstractmethod
    def complete(self, prompt: str, *, system: str = "", **kwargs: Any) -> str:
        """Gibt eine Textantwort zurück."""
        ...


class MockLLM(BaseLLM):
    """
    Deterministischer Mock für den Prototyp.
    Reagiert auf Schlüsselwörter im Prompt und liefert
    plausible, strukturierte Antworten.
    """

    provider_name = "mock"
    model_name = "deterministic-mock"
    is_simulation = True

    def complete(self, prompt: str, *, system: str = "", **kwargs: Any) -> str:
        prompt_lower = prompt.lower()

        # Consolidation zuerst (Prompt enthält oft ältere Critic/Synthese-Texte)
        if (
            "memory-consolidator" in (system or "").lower()
            or "memory-einträge" in prompt_lower
            or "wissenseinträge" in prompt_lower
            or "extrahiere 2" in prompt_lower
            or "hochwertige memory" in prompt_lower
        ):
            return self._mock_consolidate(prompt)

        # Planner-Antworten
        if "erstelle einen plan" in prompt_lower or "create a plan" in prompt_lower:
            return self._mock_plan(prompt)

        # Critic-Antworten (strengere Keywords)
        if (
            "prüfe den folgenden plan" in prompt_lower
            or "prüfe folgendes ergebnis" in prompt_lower
            or "du bist der critic" in (system or "").lower()
        ):
            return self._mock_critique(prompt)

        # Synthesizer
        if "du bist der synthesizer" in (system or "").lower() or (
            "finale antwort" in prompt_lower and "definition of done" in prompt_lower
        ):
            return self._mock_synthesis(prompt)

        # Spezialisten / Writing
        if "research" in prompt_lower or "recherche" in prompt_lower:
            return self._mock_research(prompt)
        if "code" in prompt_lower or "programmier" in prompt_lower:
            return self._mock_code(prompt)
        if "analyse" in prompt_lower or "analysis" in prompt_lower:
            return self._mock_analysis(prompt)
        if "writing" in prompt_lower or "formuliere" in prompt_lower or "endantwort" in prompt_lower:
            return self._mock_writing(prompt)

        # Fallback: versuche thematisch zu antworten
        return self._mock_generic(prompt)


    def _mock_plan(self, prompt: str) -> str:
        reused = "plan-muster" in prompt.lower() or "procedural memory" in prompt.lower() or "erfolgreiche plan" in prompt.lower()
        if reused:
            return """{
  "rationale": "Erfolgreiches Plan-Muster aus dem Procedural Memory übernommen und leicht an das aktuelle Ziel angepasst (Research → Analysis → Writing).",
  "reused_pattern": true,
  "steps": [
    {
      "description": "Relevante Informationen sammeln und strukturieren (aus Pattern)",
      "specialist_type": "research",
      "expected_output": "Zusammengefasste Fakten mit Quellenangaben"
    },
    {
      "description": "Analyse und Bewertung der gesammelten Informationen (aus Pattern)",
      "specialist_type": "analysis",
      "expected_output": "Strukturierte Bewertung mit Stärken/Schwächen"
    },
    {
      "description": "Klare, überprüfbare Endantwort formulieren (aus Pattern)",
      "specialist_type": "writing",
      "expected_output": "Finale Antwort im gewünschten Format"
    }
  ]
}"""
        return """{
  "rationale": "Das Ziel wird in drei klare Schritte zerlegt, um Verifizierbarkeit zu gewährleisten.",
  "reused_pattern": false,
  "steps": [
    {
      "description": "Relevante Informationen sammeln und strukturieren",
      "specialist_type": "research",
      "expected_output": "Zusammengefasste Fakten mit Quellenangaben"
    },
    {
      "description": "Analyse und Bewertung der gesammelten Informationen",
      "specialist_type": "analysis",
      "expected_output": "Strukturierte Bewertung mit Stärken/Schwächen"
    },
    {
      "description": "Klare, überprüfbare Endantwort formulieren",
      "specialist_type": "writing",
      "expected_output": "Finale Antwort im gewünschten Format"
    }
  ]
}"""

    def _mock_critique(self, prompt: str) -> str:
        # Ein Mock kann keine unabhängige inhaltliche Verifikation leisten.
        return """{
  "passed": false,
  "score": 0.20,
  "issues": [
    "Simulierter Critic: keine unabhängige inhaltliche Verifikation möglich"
  ],
  "suggestions": [
    "Run mit einem explizit konfigurierten Live-Provider erneut ausführen"
  ]
}"""

    def _topic(self, prompt: str) -> str:
        """Extrahiert grob das Thema aus dem Prompt."""
        import re
        m = re.search(r"Ziel:\s*(.+?)(?:\n|$)", prompt)
        if m:
            return m.group(1).strip()[:200]
        m = re.search(r"Auftrag:\s*(.+?)(?:\n|$)", prompt)
        if m:
            return m.group(1).strip()[:200]
        return prompt[:120].replace("\n", " ")

    def _is_multiagent(self, text: str) -> bool:
        t = text.lower()
        return any(k in t for k in (
            "multi-agent", "multiagent", "multi agenten", "agenten-system",
            "agentensystem", "mehrere agenten", "spezialisten",
        ))

    def _is_math(self, text: str) -> bool:
        import re
        return bool(re.search(r"\d+\s*[\+\-\*/]\s*\d+", text)) or "berechne" in text.lower()

    def _mock_research(self, prompt: str) -> str:
        topic = self._topic(prompt)
        if self._is_multiagent(prompt):
            return (
                "Recherche-Ergebnis:\n"
                "1. Multi-Agenten-Systeme zerlegen Aufgaben in Rollen (Planner, Critic, Spezialisten).\n"
                "2. Vorteil: Spezialisierung und parallele Bearbeitung erhöhen Qualität und Abdeckung.\n"
                "3. Vorteil: Critic-/Verify-Schichten können Halluzinationen und Lücken früher finden.\n"
                "4. Risiko: Höhere Latenz, Orchestrierungskomplexität und schwierigere Fehleranalyse.\n"
                "5. Risiko: Widersprüchliche Agenten-Outputs ohne klare Synthese-Regeln.\n"
                f"Bezug zum Ziel: {topic[:100]}"
            )
        if self._is_math(topic):
            return (
                "Recherche-Ergebnis:\n"
                "- Mathematische Teilaufgabe erkannt.\n"
                "- Empfehlung: calculator-Tool für exakte Auswertung nutzen.\n"
                f"- Kontext: {topic[:100]}"
            )
        return (
            f"Recherche-Ergebnis zu: {topic}\n"
            "- Kernthemen und Rahmenbedingungen wurden strukturiert erfasst.\n"
            "- Offene Punkte und Unsicherheiten markiert.\n"
            "- Konfidenz: mittel (Mock-Wissen, keine Live-Websuche)."
        )

    def _mock_analysis(self, prompt: str) -> str:
        if self._is_multiagent(prompt):
            return (
                "Analyse:\n"
                "Stärken von Multi-Agenten:\n"
                "- Spezialisierung pro Rolle\n"
                "- Bessere Prüfbarkeit durch Critic und Receipts\n"
                "- Wiederverwendbare Plan-Muster (Procedural Memory)\n\n"
                "Schwächen:\n"
                "- Mehr Koordinationsaufwand und Kosten\n"
                "- Fehler propagieren über mehrere Schritte\n"
                "- Qualität hängt stark von Routing und Critic-Kriterien ab\n\n"
                "Fazit: Sinnvoll bei komplexen, mehrstufigen Zielen; Overkill für einfache Faktenfragen."
            )
        return (
            "Analyse:\n"
            "- Stärken: nachvollziehbare Struktur, klare Teilschritte\n"
            "- Schwächen: begrenzte Faktenlage im Mock-Modus\n"
            "- Empfehlung: Definition of Done eng führen und Belege verlangen"
        )

    def _mock_writing(self, prompt: str) -> str:
        if self._is_multiagent(prompt):
            return (
                "Strukturierte Antwort:\n\n"
                "**Vorteile**\n"
                "1. Spezialisierte Agenten liefern gezieltere Teilergebnisse.\n"
                "2. Verify/Critic erhöht die Chance, Fehler vor der Endantwort zu finden.\n"
                "3. Pläne und Receipts machen den Prozess auditierbar.\n\n"
                "**Risiken**\n"
                "1. Höhere Latenz und Betriebskomplexität.\n"
                "2. Widersprüche zwischen Agenten ohne starke Synthese.\n"
                "3. Mehr Angriffsfläche für fehlerhaftes Routing oder Tool-Missbrauch."
            )
        return f"Formulierte Antwort zum Auftrag:\n{self._topic(prompt)}\n\n(Mock-Schreibstil: klar, knapp, überprüfbar.)"

    def _mock_code(self, prompt: str) -> str:
        return (
            "```python\n"
            "def solve(task: str) -> str:\n"
            "    \"\"\"Beispiel-Stub für den Code-Specialist.\"\"\"\n"
            "    return f'processed: {task[:80]}'\n"
            "```\n"
            "Hinweis: Im Mock keine echte Ausführung – nur strukturierter Vorschlag."
        )

    def _mock_generic(self, prompt: str) -> str:
        return (
            f"Antwort (Mock) zum Thema:\n{self._topic(prompt)}\n\n"
            "Wesentliche Punkte wurden adressiert. Für Produktion bitte echtes LLM anbinden."
        )

    def _mock_synthesis(self, prompt: str) -> str:
        # Ziehe sichtbare Zwischenergebnisse in die Endantwort
        chunks = []
        for marker in ("Recherche-Ergebnis", "Analyse:", "Strukturierte Antwort:", "Vorteile", "Risiken"):
            if marker.lower() in prompt.lower():
                chunks.append(marker)
        if self._is_multiagent(prompt):
            return (
                "### Finale Antwort\n\n"
                "Multi-Agenten-Systeme lohnen sich, wenn Aufgaben in klare Rollen zerlegbar sind.\n\n"
                "**Vorteile**\n"
                "1. Spezialisierung (Research, Analyse, Code, Writing)\n"
                "2. Gegenprüfung durch Critic und Receipts\n"
                "3. Wiederverwendbare Plan-Muster aus dem Procedural Memory\n\n"
                "**Risiken**\n"
                "1. Höhere Latenz und Orchestrierungskosten\n"
                "2. Mögliche Widersprüche zwischen Agenten\n"
                "3. Komplexeres Debugging über mehrere Schritte\n\n"
                "**Empfehlung:** Für mehrstufige, prüfpflichtige Ziele einsetzen; "
                "für einfache Einzelfragen ein einzelnes starkes Modell bevorzugen.\n\n"
                f"_Simulierte Synthese aus {len(chunks) or 'vorhandenen'} erkannten Zwischenbausteinen. "
                "Nicht inhaltlich verifiziert._"
            )
        # Generische, aber prompt-nähere Synthese
        topic = self._topic(prompt)
        return (
            f"### Finale Antwort\n\n"
            f"Zum Ziel „{topic[:150]}“:\n\n"
            "Die geprüften Zwischenschritte sind konsistent und adressieren die Definition of Done.\n"
            "Wesentliche Aussagen aus Recherche/Analyse wurden übernommen und gestrafft.\n\n"
            "**Kurzfazit:** Anforderung erfüllt (Mock-Modus – mit echtem LLM inhaltlich tiefer)."
        )


    def _mock_consolidate(self, prompt: str) -> str:
        return """{
  "entries": [
    {
      "content": "Multi-Agenten-Systeme ermöglichen Spezialisierung: einzelne Agenten können auf Recherche, Analyse oder Code fokussiert werden und liefern dadurch oft präzisere Teilergebnisse als ein monolithisches Modell.",
      "confidence": 0.85,
      "tags": ["multi-agent", "spezialisierung", "vorteil"]
    },
    {
      "content": "Der Critic-Layer und harte Receipts reduzieren Halluzinationen, erhöhen aber die Latenz und die Orchestrierungskomplexität im Vergleich zu einem einzelnen LLM-Aufruf.",
      "confidence": 0.8,
      "tags": ["critic", "risiko", "latenz"]
    },
    {
      "content": "Erfolgreiche Pläne folgen oft dem Muster Research → Analysis → Writing und eignen sich als wiederverwendbare Procedural-Patterns für ähnliche Vergleichsfragen.",
      "confidence": 0.75,
      "tags": ["procedural", "plan-muster"]
    }
  ]
}"""


class EchoLLM(BaseLLM):
    """Sehr einfacher LLM, der den Prompt nur zurückgibt (zum Debuggen)."""

    provider_name = "echo"
    model_name = "echo"
    is_simulation = True

    def complete(self, prompt: str, *, system: str = "", **kwargs: Any) -> str:
        return f"[Echo]\nSystem: {system}\n\n{prompt}"


# ────────────────────────── Provider-Adapter ──────────────────────────

def _require_env(name: str) -> str:
    import os
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Umgebungsvariable {name} fehlt.\n"
            f"  export {name}='...' \n"
            f"Oder: TANKAI_LLM=mock für den Mock-Modus."
        )
    return val


class OpenAILLM(BaseLLM):
    """
    OpenAI Chat Completions Adapter.

    Voraussetzungen:
      pip install openai
      export OPENAI_API_KEY=sk-...

    Optional:
      export OPENAI_MODEL=<modellname>
      export OPENAI_BASE_URL=...   # für kompatible Proxies / Azure
    """

    provider_name = "openai"

    def __init__(
        self,
        model: str | None = None,
        temperature: float = 0.2,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        import os
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError(
                "OpenAI-Paket fehlt. Installiere mit:\n  pip install openai"
            ) from e

        key = api_key or _require_env("OPENAI_API_KEY")
        self.model = (model or os.environ.get("OPENAI_MODEL", "")).strip()
        if not self.model:
            raise RuntimeError("OPENAI_MODEL fehlt; Modell muss explizit konfiguriert werden")
        self.model_name = self.model
        self.temperature = temperature
        self.max_tokens = max_tokens

        client_kwargs: dict[str, Any] = {"api_key": key}
        base = base_url or os.environ.get("OPENAI_BASE_URL")
        if base:
            client_kwargs["base_url"] = base
        self.client = OpenAI(**client_kwargs)

    def complete(self, prompt: str, *, system: str = "", **kwargs: Any) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=kwargs.get("temperature", self.temperature),
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
        )
        return (resp.choices[0].message.content or "").strip()


class AnthropicLLM(BaseLLM):
    """
    Anthropic Messages API Adapter.

    Voraussetzungen:
      pip install anthropic
      export ANTHROPIC_API_KEY=sk-ant-...

    Optional:
      export ANTHROPIC_MODEL=<modellname>
    """

    provider_name = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        temperature: float = 0.2,
        api_key: str | None = None,
        max_tokens: int = 4096,
    ) -> None:
        import os
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "Anthropic-Paket fehlt. Installiere mit:\n  pip install anthropic"
            ) from e

        key = api_key or _require_env("ANTHROPIC_API_KEY")
        self.model = (model or os.environ.get("ANTHROPIC_MODEL", "")).strip()
        if not self.model:
            raise RuntimeError("ANTHROPIC_MODEL fehlt; Modell muss explizit konfiguriert werden")
        self.model_name = self.model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = anthropic.Anthropic(api_key=key)

    def complete(self, prompt: str, *, system: str = "", **kwargs: Any) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=kwargs.get("max_tokens", self.max_tokens),
            temperature=kwargs.get("temperature", self.temperature),
            system=system or "Du bist ein präziser, hilfreicher Assistent.",
            messages=[{"role": "user", "content": prompt}],
        )
        # content ist eine Liste von Blocks
        parts = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()



def _load_dotenv_if_available() -> None:
    """Lädt .env aus CWD, falls python-dotenv installiert ist (optional)."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        # Manuelles Mini-Parsing von .env falls vorhanden
        from pathlib import Path
        import os
        env_path = Path(".env")
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip(chr(39)+chr(34))
                os.environ.setdefault(k, v)


def get_llm(provider: str | None = None, **kwargs: Any) -> BaseLLM:
    """
    Factory für LLM-Provider.

    provider:
      - mock (default)
      - openai
      - anthropic
      - echo

    Wenn provider=None, muss TANKAI_LLM explizit gesetzt sein. Es gibt keinen stillen Mock-Fallback.

    Beispiele:
      get_llm()
      get_llm("openai")
      get_llm("openai", model="<modellname>")
      get_llm("anthropic", model="<modellname>")
    """
    import os

    _load_dotenv_if_available()
    env_provider = os.environ.get("TANKAI_LLM", "").strip()
    selected = provider.strip() if isinstance(provider, str) else env_provider
    if not selected:
        raise RuntimeError(
            "Kein LLM-Provider konfiguriert. Setze TANKAI_LLM=openai oder "
            "TANKAI_LLM=anthropic. Für eine ausdrücklich simulierte Ausführung "
            "setze TANKAI_LLM=mock oder verwende --llm mock."
        )
    p = selected.lower()

    if p in ("mock", "default"):
        return MockLLM()
    if p == "echo":
        return EchoLLM()
    if p in ("openai", "oai", "gpt"):
        return OpenAILLM(**kwargs)
    if p in ("anthropic", "claude"):
        return AnthropicLLM(**kwargs)

    raise ValueError(
        f"Unbekannter LLM-Provider: {provider!r}. "
        "Erlaubt: mock, openai, anthropic, echo"
    )



def llm_identity(llm: BaseLLM) -> str:
    """Stabile Identität für Audit und Unabhängigkeitsprüfung."""
    provider = str(getattr(llm, "provider_name", type(llm).__name__)).strip().lower()
    model = str(getattr(llm, "model_name", getattr(llm, "model", type(llm).__name__))).strip().lower()
    base_url = ""
    client = getattr(llm, "client", None)
    if client is not None:
        base_url = str(getattr(client, "base_url", "")).rstrip("/").lower()
    return f"{provider}:{model}:{base_url}"


def get_critic_llm(default: BaseLLM | None = None) -> BaseLLM:
    """Lädt einen separat konfigurierten Critic oder verwendet bewusst den Default.

    Umgebungsvariablen:
      TANKAI_CRITIC_LLM=openai|anthropic|mock|echo
      TANKAI_CRITIC_MODEL=<Modell>
      TANKAI_CRITIC_API_KEY=<optionaler separater Key>
      TANKAI_CRITIC_BASE_URL=<nur OpenAI-kompatibel>
    """
    import os

    _load_dotenv_if_available()
    provider = os.environ.get("TANKAI_CRITIC_LLM", "").strip()
    if not provider:
        if default is None:
            raise RuntimeError(
                "Kein Critic-Provider konfiguriert. Setze TANKAI_CRITIC_LLM oder übergib ein Default-LLM."
            )
        return default

    kwargs: dict[str, Any] = {}
    model = os.environ.get("TANKAI_CRITIC_MODEL", "").strip()
    if model:
        kwargs["model"] = model
    api_key = os.environ.get("TANKAI_CRITIC_API_KEY", "").strip()
    if api_key:
        kwargs["api_key"] = api_key
    base_url = os.environ.get("TANKAI_CRITIC_BASE_URL", "").strip()
    if base_url and provider.lower() in {"openai", "oai", "gpt"}:
        kwargs["base_url"] = base_url
    return get_llm(provider, **kwargs)

def describe_llm_setup() -> str:
    """Hilfstext: welcher Provider wäre jetzt verfügbar?"""
    import os
    lines = ["LLM-Setup:"]
    lines.append(f"  TANKAI_LLM={os.environ.get('TANKAI_LLM', 'nicht gesetzt')}")
    lines.append(f"  TANKAI_CRITIC_LLM={os.environ.get('TANKAI_CRITIC_LLM', 'nicht gesetzt (gleich Hauptmodell)')}")
    lines.append(f"  TANKAI_CRITIC_MODEL={os.environ.get('TANKAI_CRITIC_MODEL', 'nicht gesetzt')}")
    lines.append(
        f"  OPENAI_API_KEY={'gesetzt' if os.environ.get('OPENAI_API_KEY') else 'fehlt'}"
    )
    lines.append(
        f"  OPENAI_MODEL={os.environ.get('OPENAI_MODEL', 'fehlt')}"
    )
    lines.append(
        f"  ANTHROPIC_API_KEY={'gesetzt' if os.environ.get('ANTHROPIC_API_KEY') else 'fehlt'}"
    )
    lines.append(
        f"  ANTHROPIC_MODEL={os.environ.get('ANTHROPIC_MODEL', 'fehlt')}"
    )
    try:
        import openai  # noqa: F401
        lines.append("  openai-Paket: installiert")
    except ImportError:
        lines.append("  openai-Paket: nicht installiert")
    try:
        import anthropic  # noqa: F401
        lines.append("  anthropic-Paket: installiert")
    except ImportError:
        lines.append("  anthropic-Paket: nicht installiert")
    return "\n".join(lines)
