"""Pinned NVIDIA skill catalog and read-only original documents; no code executes."""
import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3

REVISION = "d90390a08ce6105c45938785fb07864ee6011af5"
_DOCUMENT_ROOT = Path(__file__).with_name("nvidia_skill_docs")
_BLOBS = {
    "rag-blueprint": "2f8ce918131063e11e74a5558e25ecf3802e6777",
    "data-designer": "e3a2777269bb097751284dfba00cebc0832e4358",
    "rag-eval": "010478c61d867cdedb7b1764ee0edae7218ca039",
    "nemo-retriever": "f35badee5781315eed6b89c7a839367fc39b7c39"
}
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
         "revision": REVISION, "repository_license": "Apache-2.0", "documentation_license": "CC-BY-4.0",
         "document_available": key in states, "imported": key in states,
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


def document(root: Path, user_id: str, skill_id: str) -> dict:
    if skill_id not in _BLOBS:
        raise ValueError("Unbekannter Skill")
    if not any(s["id"] == skill_id and s["imported"] for s in catalog(root, user_id)):
        raise PermissionError("Skill zuerst importieren")
    raw = (_DOCUMENT_ROOT / skill_id / "SKILL.md").read_bytes()
    if len(raw) > 64_000:
        raise RuntimeError("Skill-Dokument zu groß")
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\x00" + raw, usedforsecurity=False).hexdigest()
    if blob != _BLOBS[skill_id]:
        raise RuntimeError("Skill-Dokument entspricht nicht der fixierten Quelle")
    return {"id": skill_id, "revision": REVISION, "blob_sha": blob,
            "content": raw.decode("utf-8"), "execution_available": False}
