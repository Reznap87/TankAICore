"""Pinned NVIDIA skill catalog. Import registers metadata only; no remote code executes."""
from contextlib import closing
from pathlib import Path
import sqlite3

REVISION = "d90390a08ce6105c45938785fb07864ee6011af5"
_CATALOG = {
    "rag-blueprint": ("Wissensdatenbank und RAG", "Plane eine RAG-Architektur; benenne Quellen, Indexierung, Zugriffsschutz und Validierung."),
    "data-designer": ("Synthetische Trainingsdaten", "Entwirf ein Datenschema und einen Plan zur synthetischen Datenerzeugung mit Qualitäts- und Lizenzprüfung."),
    "rag-eval": ("RAG-Qualität prüfen", "Entwirf eine Evaluation mit Testfragen, Referenzantworten, Retrieval-Metriken und Fehleranalyse."),
    "nemo-retriever": ("Dokumentsuche und Extraktion", "Plane die Dokumentextraktion und Suche; benenne Formate, Datenschutz und notwendige Abhängigkeiten."),
}

def _connect(root: Path):
    db = sqlite3.connect(root / "skill_store.sqlite3", timeout=5)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS skills (user_id TEXT NOT NULL, skill_id TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)), PRIMARY KEY(user_id, skill_id))")
        return db
    except Exception:
        db.close()
        raise

def catalog(root: Path, user_id: str) -> list[dict]:
    with closing(_connect(root)) as db:
        states = dict(db.execute("SELECT skill_id, enabled FROM skills WHERE user_id = ?", (user_id,)))
    return [
        {"id": key, "title": title, "source": f"https://github.com/NVIDIA/skills/blob/{REVISION}/skills/{key}/SKILL.md",
         "revision": REVISION, "repository_license": "Apache-2.0", "imported": key in states,
         "enabled": states.get(key) == 1, "execution_available": False,
         "guidance": guidance if states.get(key) == 1 else None}
        for key, (title, guidance) in _CATALOG.items()
    ]

def change(root: Path, user_id: str, skill_id: str, action: str) -> list[dict]:
    if skill_id not in _CATALOG or action not in ("import", "enable", "disable"):
        raise ValueError("Unbekannter Skill oder Aktion")
    with closing(_connect(root)) as db, db:
        if action == "import":
            db.execute("INSERT OR IGNORE INTO skills VALUES (?, ?, 0)", (user_id, skill_id))
        else:
            updated = db.execute("UPDATE skills SET enabled = ? WHERE user_id = ? AND skill_id = ?",
                                 (int(action == "enable"), user_id, skill_id))
            if updated.rowcount != 1:
                raise ValueError("Skill zuerst importieren")
    return catalog(root, user_id)
