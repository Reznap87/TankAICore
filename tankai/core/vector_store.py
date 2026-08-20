"""
Lokaler Vector-Store mit austauschbarem Embedder.

Persistenz verwendet ausschließlich nicht-pickelbare NumPy-Datentypen.
Manipulierte oder inkompatible Dateien werden abgelehnt statt ausgeführt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import numpy as np

from .embeddings import BaseEmbedder, HashingEmbedder


class VectorStore:
    """In-Memory Vector-Store mit optionaler, sicherer NPZ-Persistenz."""

    FORMAT_VERSION = 2

    def __init__(
        self,
        dim: int = 384,
        persist_path: Optional[str] = None,
        embedder: Optional[BaseEmbedder] = None,
    ) -> None:
        self.embedder = embedder or HashingEmbedder(dim=dim)
        self.dim = self.embedder.dim
        self.persist_path = persist_path

        self.ids: list[str] = []
        self.vectors: Optional[np.ndarray] = None
        self.metadatas: list[dict] = []

        if persist_path:
            self._load()

    def add(self, id: str, text: str, metadata: Optional[dict] = None) -> None:
        vec = np.asarray(self.embedder.embed(text), dtype=np.float32)
        if vec.shape != (self.dim,):
            raise ValueError(
                f"Embedder lieferte Shape {vec.shape}; erwartet ({self.dim},)"
            )
        if id in self.ids:
            idx = self.ids.index(id)
            assert self.vectors is not None
            self.vectors[idx] = vec
            self.metadatas[idx] = metadata or {}
        else:
            self.ids.append(str(id))
            self.metadatas.append(metadata or {})
            if self.vectors is None:
                self.vectors = vec.reshape(1, -1)
            else:
                self.vectors = np.vstack([self.vectors, vec])

        if self.persist_path:
            self._save()

    def search(
        self,
        query: str,
        k: int = 5,
        min_score: float = 0.05,
    ) -> list[tuple[str, float, dict]]:
        if self.vectors is None or len(self.ids) == 0:
            return []

        q = np.asarray(self.embedder.embed(query), dtype=np.float32)
        if q.shape != (self.dim,):
            raise ValueError(f"Query-Embedding hat ungültige Shape {q.shape}")
        scores = self.vectors @ q
        top_idx = np.argsort(scores)[::-1][: max(0, int(k))]

        results = []
        for i in top_idx:
            score = float(scores[i])
            if score < min_score:
                continue
            results.append((self.ids[i], score, self.metadatas[i]))
        return results

    def delete(self, id: str) -> bool:
        if id not in self.ids:
            return False
        idx = self.ids.index(id)
        self.ids.pop(idx)
        self.metadatas.pop(idx)
        assert self.vectors is not None
        self.vectors = np.delete(self.vectors, idx, axis=0)
        if len(self.ids) == 0:
            self.vectors = None
        if self.persist_path:
            self._save()
        return True

    def __len__(self) -> int:
        return len(self.ids)

    def _save(self) -> None:
        if not self.persist_path:
            return
        path = Path(self.persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp.npz")

        vectors = (
            self.vectors
            if self.vectors is not None
            else np.empty((0, self.dim), dtype=np.float32)
        )
        ids = np.asarray(self.ids, dtype=np.str_)
        metadata_json = np.asarray(
            [json.dumps(m, ensure_ascii=False, sort_keys=True) for m in self.metadatas],
            dtype=np.str_,
        )
        np.savez_compressed(
            tmp,
            format_version=np.asarray([self.FORMAT_VERSION], dtype=np.int64),
            dim=np.asarray([self.dim], dtype=np.int64),
            ids=ids,
            vectors=np.asarray(vectors, dtype=np.float32),
            metadatas=metadata_json,
        )
        os.replace(tmp, path)

    def _load(self) -> None:
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with np.load(self.persist_path, allow_pickle=False) as data:
                required = {"format_version", "dim", "ids", "vectors", "metadatas"}
                if not required.issubset(data.files):
                    raise ValueError(
                        "Unsicheres oder altes Vector-Format. Datei löschen und Index neu aufbauen."
                    )
                version = int(data["format_version"][0])
                stored_dim = int(data["dim"][0])
                if version != self.FORMAT_VERSION:
                    raise ValueError(f"Vector-Format {version} wird nicht unterstützt")
                if stored_dim != self.dim:
                    raise ValueError(
                        f"Vector-Dimension {stored_dim} passt nicht zum Embedder ({self.dim})"
                    )

                ids = [str(x) for x in data["ids"].tolist()]
                vectors = np.asarray(data["vectors"], dtype=np.float32)
                metadata_raw = [str(x) for x in data["metadatas"].tolist()]
                if vectors.ndim != 2 or vectors.shape[1] != self.dim:
                    raise ValueError(f"Ungültige Vector-Matrix: {vectors.shape}")
                if not (len(ids) == len(metadata_raw) == vectors.shape[0]):
                    raise ValueError("Vector-Datei enthält inkonsistente Längen")
                metadatas = [json.loads(item) for item in metadata_raw]
                if not all(isinstance(item, dict) for item in metadatas):
                    raise ValueError("Vector-Metadaten müssen JSON-Objekte sein")

                self.ids = ids
                self.vectors = vectors if len(ids) else None
                self.metadatas = metadatas
        except Exception as exc:
            raise RuntimeError(
                f"Vector-Store konnte nicht sicher geladen werden: {self.persist_path}"
            ) from exc
