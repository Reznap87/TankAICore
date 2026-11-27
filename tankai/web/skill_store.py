"""Pinned NVIDIA skill catalog and read-only original documents; no code executes."""
import hashlib
import re
from collections import Counter
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

_ADAPTERS = {
    "rag-eval": "tankai_local_retrieval_metrics",
    "nemo-retriever": "tankai_local_text_search",
    "data-designer": "tankai_local_dataset_validation",
    "rag-blueprint": "tankai_local_rag_preflight",
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
         "local_adapter_available": True,
         "local_adapter": _ADAPTERS[key],
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


def run_local(root: Path, user_id: str, skill_id: str, payload: dict) -> dict:
    """Explicit local adapter, not NVIDIA RAGAS. No network, shell or model calls."""
    if skill_id not in _ADAPTERS:
        raise ValueError("Für diesen Skill ist kein lokaler Runner verfügbar")
    if not any(s["id"] == skill_id and s["enabled"] for s in catalog(root, user_id)):
        raise PermissionError("Skill zuerst aktivieren")
    if not isinstance(payload, dict):
        raise ValueError("JSON-Objekt erforderlich")
    if skill_id != "rag-eval":
        return {"skill_id": skill_id, "adapter": _ADAPTERS[skill_id],
                "nvidia_execution": False, "external_calls": 0,
                **_run_adapter(skill_id, payload)}
    if set(payload) != {"ranked_ids", "relevant_ids"}:
        raise ValueError("ranked_ids und relevant_ids erforderlich")
    ranked, relevant = payload["ranked_ids"], payload["relevant_ids"]
    for values in (ranked, relevant):
        if not isinstance(values, list) or len(values) > 1000 or any(not isinstance(v, str) or not v or len(v) > 200 for v in values):
            raise ValueError("Ungültige Dokument-IDs")
        if len(set(values)) != len(values):
            raise ValueError("Doppelte Dokument-IDs")
    if not relevant:
        raise ValueError("Mindestens eine relevante Dokument-ID erforderlich")
    truth = set(relevant)
    hits = len(truth.intersection(ranked))
    first = next((i for i, v in enumerate(ranked, 1) if v in truth), None)
    return {"skill_id": skill_id, "adapter": "tankai_local_retrieval_metrics",
            "nvidia_execution": False, "external_calls": 0, "retrieved": len(ranked),
            "relevant": len(relevant), "hits": hits,
            "precision": hits / len(ranked) if ranked else 0.0,
            "recall": hits / len(relevant), "reciprocal_rank": 1 / first if first else 0.0}

def _tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.casefold())

def _run_adapter(skill_id: str, payload: dict) -> dict:
    if skill_id == "nemo-retriever":
        if set(payload) != {"query", "documents"}:
            raise ValueError("query und documents erforderlich")
        query, docs = payload["query"], payload["documents"]
        if not isinstance(query, str) or not 0 < len(query) <= 1000 or not _tokens(query):
            raise ValueError("Ungültige Suchanfrage")
        if not isinstance(docs, list) or not 0 < len(docs) <= 50:
            raise ValueError("1 bis 50 Dokumente erforderlich")
        terms = set(_tokens(query))
        matches = []
        seen = set()
        for doc in docs:
            if not isinstance(doc, dict) or set(doc) != {"id", "text"}:
                raise ValueError("Dokument benötigt id und text")
            key, text = doc["id"], doc["text"]
            if not isinstance(key, str) or not 0 < len(key) <= 200 or key in seen or not isinstance(text, str) or len(text) > 8000:
                raise ValueError("Ungültiges oder doppeltes Dokument")
            seen.add(key)
            words = set(_tokens(text))
            hits = len(terms & words)
            if hits:
                matches.append({"id": key, "matched_terms": hits, "query_coverage": hits / len(terms)})
        matches.sort(key=lambda x: (-x["matched_terms"], x["id"]))
        return {"method": "lexical_token_overlap", "searched": len(docs), "matches": matches[:10]}
    if skill_id == "data-designer":
        if set(payload) != {"required_fields", "rows"}:
            raise ValueError("required_fields und rows erforderlich")
        fields, rows = payload["required_fields"], payload["rows"]
        if not isinstance(fields, list) or not 0 < len(fields) <= 50 or any(not isinstance(f, str) or not 0 < len(f) <= 100 for f in fields) or len(set(fields)) != len(fields):
            raise ValueError("Ungültige Pflichtfelder")
        if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(r, dict) or len(r) > 100 for r in rows):
            raise ValueError("Ungültige Datensätze")
        issues = []
        counts = Counter()
        for i, row in enumerate(rows):
            missing = [f for f in fields if f not in row or row[f] is None or (isinstance(row[f], str) and not row[f].strip())]
            counts.update(missing)
            if missing:
                issues.append({"row": i + 1, "missing_fields": missing})
        return {"rows": len(rows), "valid_rows": len(rows) - len(issues), "missing_counts": dict(counts), "issues": issues[:100], "issues_truncated": len(issues) > 100}
    if skill_id == "rag-blueprint":
        checks = ("source_inventory", "access_control", "index_strategy", "retrieval_evaluation", "deletion_policy")
        if set(payload) != set(checks) or any(type(payload[k]) is not bool for k in checks):
            raise ValueError("Alle fünf Prüfpunkte als true/false erforderlich")
        missing = [k for k in checks if not payload[k]]
        return {"method": "self_reported_checklist", "missing": missing, "completed": len(checks) - len(missing),
                "total": len(checks), "deployment_verified": False}
    raise ValueError("Unbekannter Adapter")
