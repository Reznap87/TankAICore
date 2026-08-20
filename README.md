# TankAI — Web Intelligence OS (Prototyp v0.5)

Modularer Multi-Agenten-Kern in Python.

## Features

| Bereich | Status |
|---------|--------|
| PLAN → ROUTE → VERIFY → LEARN | ✅ |
| Critic + Receipts | ✅ |
| Langzeitgedächtnis (Episodic / Semantic / Procedural) | ✅ |
| Vector-Suche + Embeddings (hashing / torch / ST / OpenAI) | ✅ |
| LLM-Consolidation | ✅ |
| Retention + Cold-Storage | ✅ |
| Procedural Memory im Planner | ✅ |
| Tool-Use (Calculator, DateTime, MemorySearch) | ✅ |
| Parallele Specialists | ✅ |
| LLM-Adapter (Mock / OpenAI / Anthropic) | ✅ |
| Web-UI (stdlib) | ✅ |

## Schnellstart

```bash
cd /home/workdir/artifacts
python demo.py                  # Basis-Demo
python demo_ltm.py              # Langzeitgedächtnis
python demo_procedural.py       # Plan-Muster wiederverwenden
python demo_retention.py        # Retention / Cold
python -m tankai.web.server     # Web-UI → http://127.0.0.1:8765
```

## Code-Beispiel

```python
from tankai import TankAI, get_llm
from tankai.core.long_term_memory import LongTermMemory

tank = TankAI(
    llm=get_llm("mock"),          # oder "openai" / "anthropic"
    use_ltm=True,
    parallel=True,                # parallele Specialists
    enable_tools=True,
    verbose=True,
)
tank.ltm = LongTermMemory(in_memory=True, embedder="hashing")

result = tank.run(
    goal_description="Dein Ziel",
    definition_of_done="Was als fertig gilt",
)
print(result.final_answer)
```

## Echte LLMs

```python
# pip install openai
tank = TankAI(llm=get_llm("openai", model="gpt-4o-mini"))

# pip install anthropic
tank = TankAI(llm=get_llm("anthropic"))
```

## Tools

Specialists können Tools im Format `TOOL:name{param=value}` aufrufen:

- `calculator` — sichere Math-Ausdrücke
- `datetime` — UTC-Zeit
- `memory_search` — LTM-Suche

## Projektstruktur

```
tankai/
├── core/
│   ├── models.py, memory.py, long_term_memory.py
│   ├── vector_store.py, embeddings.py
│   ├── llm.py, tools.py, loop.py
├── agents/
│   ├── planner.py, specialist.py, critic.py, synthesizer.py
└── web/
    └── server.py
```

## CLI

```bash
python -m tankai "Dein Ziel hier"
python -m tankai --demo
python -m tankai --parallel --llm mock "Ziel"
python -m tankai --web          # Web-UI auf :8765
```

## Web-UI (v2)

- Ziel + Definition of Done
- Anzeige von Antwort, Plan, Receipts
- Verlauf der letzten Runs
- Parallel / Tools umschaltbar
- LTM-Status

```bash
python -m tankai --web
# → http://127.0.0.1:8765
```

## Echte LLMs anbinden

### OpenAI

```bash
pip install openai
export OPENAI_API_KEY=sk-...
export TANKAI_LLM=openai          # optional
export OPENAI_MODEL=gpt-4o-mini   # optional

python -m tankai --llm openai "Erkläre Multi-Agenten-Systeme"
python -m tankai --llm openai --model gpt-4o "..."
```

### Anthropic

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
export TANKAI_LLM=anthropic

python -m tankai --llm anthropic "..."
```

### Status prüfen

```bash
python -m tankai --setup
```

### Im Code

```python
from tankai import TankAI, get_llm

tank = TankAI(llm=get_llm("openai", model="gpt-4o-mini"), use_ltm=True)
# oder über Env: TANKAI_LLM=openai
tank = TankAI(llm=get_llm(), use_ltm=True)
```

Siehe auch `.env.example`.

## Deploy

Siehe [DEPLOY.md](DEPLOY.md) – Nginx, systemd, Docker, Security-Checkliste.

## Entwicklungsstatus

TankAI Core ist aktuell ein funktionsfähiger Python-Prototyp. Die Anwendung läuft
als langlebiger HTTP-Prozess und kann deshalb nicht unverändert als Cloudflare
Worker betrieben werden. Für die öffentliche Domain wird ein Container-Host oder
VPS benötigt; Cloudflare kann DNS, TLS und Proxying übernehmen.
