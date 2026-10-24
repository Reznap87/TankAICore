"""Bounded, deterministic CSV jobs. No model or external service is called."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

MAX_BYTES = 2_000_000
MAX_ROWS = 5_000
MAX_COLUMNS = 40
MAX_FIELD = 100_000
MAX_EXPORT = 4_000_000
MAX_STORED_BYTES = 40_000_000
DEFAULT_OPTIONS = {"trim": False, "removeBlank": False, "removeDuplicates": False,
                   "protectFormulas": True}


class FileJobError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise FileJobError("Ungültige Auftragskennung") from None
    return value


def _options(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_OPTIONS):
        raise FileJobError("Alle vier Bereinigungsoptionen müssen angegeben werden")
    if any(type(item) is not bool for item in value.values()):
        raise FileJobError("Bereinigungsoptionen müssen Ja/Nein-Werte sein")
    return {key: value[key] for key in DEFAULT_OPTIONS}


def _risky(value):
    return bool(re.match(r"^[\s\ufeff]*[=+@-]", value) or re.match(r"^[\t\r\n]", value))


def parse_csv(raw: bytes, delimiter="auto"):
    """Strict lexical parser, retaining physical blank records and every cell string."""
    if not raw or len(raw) > MAX_BYTES:
        raise FileJobError("Die CSV muss 1 Byte bis 2 MB groß sein", 413)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise FileJobError("Bitte eine CSV in UTF-8 verwenden") from None
    if "\x00" in text:
        raise FileJobError("Nullzeichen sind in CSV-Dateien nicht erlaubt")
    if delimiter not in ("auto", ",", ";", "\t"):
        raise FileJobError("Ungültiges Trennzeichen")
    if delimiter == "auto":
        counts = {",": 0, ";": 0, "\t": 0}
        quoted = False
        index = 0
        while index < len(text):
            char = text[index]
            if char == '"':
                if quoted and index + 1 < len(text) and text[index + 1] == '"':
                    index += 2
                    continue
                quoted = not quoted
            elif not quoted:
                if char in "\r\n":
                    break
                if char in counts:
                    counts[char] += 1
            index += 1
        delimiter = max(counts, key=counts.get)
    records, physical_blank = [], []
    row, field = [], []
    state, touched, index = "start", False, 0

    def end_field():
        row.append("".join(field))
        field.clear()
        if len(row) > MAX_COLUMNS:
            raise FileJobError("Höchstens 40 Spalten sind erlaubt")

    def end_row():
        end_field()
        records.append(row.copy())
        physical_blank.append(not touched)
        row.clear()
        if len(records) > MAX_ROWS + 1:
            raise FileJobError("Höchstens 5000 Datenzeilen sind erlaubt")

    while index < len(text):
        char = text[index]
        if state == "quoted":
            if char == '"':
                state = "after_quote"
            else:
                field.append(char)
        elif char == delimiter:
            touched = True
            end_field()
            state = "start"
        elif char in "\r\n":
            end_row()
            touched = False
            state = "start"
            if char == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                index += 1
        elif char == '"':
            touched = True
            if state == "after_quote":
                field.append('"')
                state = "quoted"
            elif state == "start":
                state = "quoted"
            else:
                raise FileJobError("Anführungszeichen mitten in einem ungeschützten Feld")
        elif state == "after_quote":
            raise FileJobError("Unerlaubtes Zeichen nach einem geschlossenen Feld")
        else:
            touched = True
            field.append(char)
            state = "unquoted"
        if len(field) > MAX_FIELD:
            raise FileJobError("Ein Feld ist länger als 100000 Zeichen")
        index += 1
    if state == "quoted":
        raise FileJobError("Ein Feld mit Anführungszeichen wurde nicht geschlossen")
    if touched or row or field:
        end_row()
    if not records:
        raise FileJobError("Die CSV enthält keine Kopfzeile")
    return records, physical_blank, delimiter


def preview_csv(raw: bytes, delimiter: str, options: dict):
    options = _options(options)
    records, physical_blank, delimiter = parse_csv(raw, delimiter)
    header = [cell.strip() if options["trim"] else cell for cell in records[0]]
    input_rows = len(records) - 1
    findings = {
        "blankRows": sum(all(not cell.strip() for cell in row) for row in records[1:]),
        "duplicateRows": len(records[1:]) - len({tuple(row) for row in records[1:]}),
        "unevenRows": sum(len(row) != len(header) for row in records[1:]),
    }
    errors = []
    if any(not cell.strip() for cell in header) or len(set(header)) != len(header):
        errors.append("Spaltennamen müssen eindeutig und nicht leer sein")
    output, seen = [], set()
    blanks = duplicates = 0
    changed = sum(a != b for a, b in zip(header, records[0]))
    for number, original in enumerate(records[1:], 2):
        if options["removeBlank"] and (physical_blank[number - 1] or
                                       all(not cell.strip() for cell in original)):
            # A delimiter-only row with the wrong width remains an error.
            if physical_blank[number - 1] or len(original) == len(header):
                blanks += 1
                continue
        if len(original) != len(header):
            if len(errors) < 10:
                errors.append(f"Datensatz {number} hat nicht {len(header)} Felder")
            continue
        row = tuple(cell.strip() if options["trim"] else cell for cell in original)
        if options["removeDuplicates"] and row in seen:
            duplicates += 1
            continue
        seen.add(row)
        changed += sum(a != b for a, b in zip(row, original))
        output.append(list(row))
    formula_cells = sum(_risky(cell) for row in [header, *output] for cell in row)
    report = {
        "inputRows": input_rows, "outputRows": len(output), "columns": len(header),
        "blankRemoved": blanks, "duplicatesRemoved": duplicates, "changedCells": changed,
        "formulaCells": formula_cells,
        "formulaCellsChangedOnCsvExport": formula_cells if options["protectFormulas"] else 0,
        "duplicateComparison": "after-trimming" if options["trim"] else "original-cell-values",
        "options": options, "effectiveSeparator": delimiter,
    }
    preview = {"header": [cell[:200] for cell in header[:8]],
               "rows": [[cell[:200] for cell in row[:8]] for row in output[:20]],
               "truncated": len(output) > 20 or len(header) > 8 or any(
                   len(cell) > 200 for row in [header, *output[:20]] for cell in row[:8])}
    result = {"valid": not errors, "errors": errors, "findings": findings,
              "report": report, "preview": preview}
    if errors:
        return result, None
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
    for row in [header, *output]:
        writer.writerow(["'" + cell if options["protectFormulas"] and _risky(cell) else cell
                         for cell in row])
    content = b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")
    if len(content) > MAX_EXPORT:
        raise FileJobError("Die Ergebnisdatei überschreitet 4 MB", 413)
    return result, content


class FileJobStore:
    """A bounded local store. Scope always comes from a resolved session, never a body."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents permissions from being open during first creation.
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self.path.is_symlink() or not self.path.is_file():
                raise RuntimeError("CSV-Auftragsspeicher ist keine reguläre Datei")
        else:
            os.close(descriptor)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS csv_jobs (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, workspace TEXT NOT NULL,
                owner TEXT NOT NULL, request_id TEXT NOT NULL, name TEXT NOT NULL,
                goal TEXT NOT NULL, source BLOB NOT NULL, delimiter TEXT NOT NULL,
                options TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                created TEXT NOT NULL, csv BLOB, report TEXT,
                UNIQUE(tenant,workspace,owner,request_id))""")

    @contextmanager
    def _db(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _scope(actor):
        return actor.tenant_id, actor.workspace_id, actor.user_id

    def _row(self, db, actor, job_id):
        row = db.execute("SELECT * FROM csv_jobs WHERE id=? AND tenant=? AND workspace=? AND owner=?",
                         (_uuid(job_id), *self._scope(actor))).fetchone()
        if row is None:
            raise FileJobError("Auftrag nicht gefunden", 404)
        return row

    @staticmethod
    def _metadata(row):
        return {"id": row["id"], "name": row["name"], "goal": row["goal"],
                "createdAt": row["created"], "revision": row["revision"],
                "completed": row["csv"] is not None, "options": json.loads(row["options"])}

    def list(self, actor):
        with self._db() as db:
            rows = db.execute("""SELECT id,name,goal,created,revision,options,
                CASE WHEN csv IS NULL THEN NULL ELSE 1 END AS csv FROM csv_jobs
                WHERE tenant=? AND workspace=? AND owner=? ORDER BY created DESC LIMIT 10""",
                              self._scope(actor)).fetchall()
            return [self._metadata(row) for row in rows]

    def create(self, actor, data):
        if set(data) != {"requestId", "name", "goal", "source", "delimiter"}:
            raise FileJobError("Ungültige Felder im Dateiauftrag")
        request_id = _uuid(data["requestId"])
        name, goal, source = data["name"], data["goal"], data["source"]
        if (not isinstance(name, str) or not name.lower().endswith(".csv") or
                not 1 <= len(name) <= 120 or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name)):
            raise FileJobError("Bitte einen einfachen CSV-Dateinamen verwenden")
        if not isinstance(goal, str) or len(goal) > 1000:
            raise FileJobError("Die Auftragsnotiz darf höchstens 1000 Zeichen enthalten")
        try:
            name.encode("utf-8")
            goal.encode("utf-8")
        except UnicodeEncodeError:
            raise FileJobError("Dateiname und Notiz müssen gültigen Unicode enthalten") from None
        if not isinstance(source, str) or len(source) > MAX_BYTES:
            raise FileJobError("Die CSV ist zu groß", 413)
        try:
            raw = source.encode("utf-8")
        except UnicodeEncodeError:
            raise FileJobError("Ungültiger UTF-8-Dateiinhalt") from None
        _, _, delimiter = parse_csv(raw, data["delimiter"])
        with self._db(write=True) as db:
            existing = db.execute("""SELECT * FROM csv_jobs WHERE tenant=? AND workspace=?
                AND owner=? AND request_id=?""", (*self._scope(actor), request_id)).fetchone()
            if existing is not None:
                if (existing["source"], existing["name"], existing["goal"], existing["delimiter"]) != (raw, name, goal, delimiter):
                    raise FileJobError("Die Übertragungskennung wurde schon anders verwendet", 409)
                return self._metadata(existing)
            count, size = db.execute("SELECT COUNT(*), COALESCE(SUM(length(source)+COALESCE(length(csv),0)),0) FROM csv_jobs").fetchone()
            own_count = db.execute("SELECT COUNT(*) FROM csv_jobs WHERE tenant=? AND workspace=? AND owner=?",
                                   self._scope(actor)).fetchone()[0]
            if count >= 100 or own_count >= 10 or size + len(raw) > MAX_STORED_BYTES:
                raise FileJobError("Der Auftragsspeicher ist voll. Bitte alte Aufträge löschen", 409)
            job_id = str(uuid4())
            db.execute("""INSERT INTO csv_jobs
                (id,tenant,workspace,owner,request_id,name,goal,source,delimiter,options,created)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (job_id, *self._scope(actor), request_id,
                name, goal, raw, delimiter, json.dumps(DEFAULT_OPTIONS), _now()))
            return self._metadata(self._row(db, actor, job_id))

    def get(self, actor, job_id):
        with self._db() as db:
            row = self._row(db, actor, job_id)
            detail, _ = preview_csv(row["source"], row["delimiter"], json.loads(row["options"]))
            return {**self._metadata(row), **detail,
                    "deliveryReport": json.loads(row["report"]) if row["report"] else None}

    def update(self, actor, job_id, data, *, complete=False):
        expected_keys = {"revision"} if complete else {"revision", "options"}
        if set(data) != expected_keys or type(data.get("revision")) is not int or data["revision"] < 1:
            raise FileJobError("Eine gültige Vorschauversion ist erforderlich")
        options = None if complete else _options(data["options"])
        with self._db(write=True) as db:
            row = self._row(db, actor, job_id)
            if row["revision"] != data["revision"]:
                raise FileJobError("Der Auftrag wurde geändert. Bitte neu laden und prüfen", 409)
            if row["csv"] is not None:
                if complete:
                    return self._metadata(row)
                raise FileJobError("Abgeschlossene Aufträge sind unveränderlich", 409)
            if not complete:
                # Parse before changing the draft, including bounded export validation.
                preview_csv(row["source"], row["delimiter"], options)
                if options != json.loads(row["options"]):
                    db.execute("UPDATE csv_jobs SET options=?,revision=revision+1 WHERE id=?",
                               (json.dumps(options), job_id))
            else:
                detail, content = preview_csv(row["source"], row["delimiter"], json.loads(row["options"]))
                if content is None:
                    raise FileJobError("Die Vorschau enthält noch Fehler", 409)
                size = db.execute("SELECT COALESCE(SUM(length(source)+COALESCE(length(csv),0)),0) FROM csv_jobs").fetchone()[0]
                if size + len(content) > MAX_STORED_BYTES:
                    raise FileJobError("Der Auftragsspeicher ist voll", 409)
                report = {**detail["report"], "jobId": job_id, "revision": row["revision"],
                    "sourceFile": row["name"], "sourceSha256": hashlib.sha256(row["source"]).hexdigest(),
                    "csvSha256": hashlib.sha256(content).hexdigest(), "csvByteLength": len(content),
                    "generatedAt": _now(), "encoding": "UTF-8 with BOM", "lineEnding": "CRLF",
                    "processing": "deterministic-csv-tool", "externalModelCalls": 0,
                    "semanticCorrectnessVerified": False}
                db.execute("UPDATE csv_jobs SET csv=?,report=? WHERE id=?",
                           (content, json.dumps(report, ensure_ascii=False), job_id))
            return self._metadata(self._row(db, actor, job_id))

    def download(self, actor, job_id, kind):
        with self._db() as db:
            row = self._row(db, actor, job_id)
            if row["csv"] is None:
                raise FileJobError("Bitte zuerst die geprüfte Vorschau übernehmen", 409)
            if kind == "csv":
                return row["csv"], "text/csv; charset=utf-8", f"TankAI-{job_id}.csv"
            if kind == "report":
                return row["report"].encode("utf-8"), "application/json; charset=utf-8", f"TankAI-{job_id}-bericht.json"
            raise FileJobError("Download nicht gefunden", 404)

    def delete(self, actor, job_id):
        with self._db(write=True) as db:
            self._row(db, actor, job_id)
            db.execute("DELETE FROM csv_jobs WHERE id=?", (job_id,))
