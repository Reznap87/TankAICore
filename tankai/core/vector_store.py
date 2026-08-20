"""
Lokaler Vector-Store mit austauschbarem Embedder.
"""

from __future__ import annotations

import os
from typing import Optional

import numpy as np

from .embeddings import BaseEmbedder, HashingEmbedder


class VectorStore:
    """
    In-Memory Vector-Store mit optionaler Persistenz als .npz.
    """

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
        vec = self.embedder.embed(text)
        if id in self.ids:
            idx = self.ids.index(id)
            self.vectors[idx] = vec
            self.metadatas[idx] = metadata or {}
        else:
            self.ids.append(id)
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

        q = self.embedder.embed(query)
        scores = self.vectors @ q
        top_idx = np.argsort(scores)[::-1][:k]

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
        self.vectors = np.delete(self.vectors, idx, axis=0)
        if len(self.ids) == 0:
            self.vectors = None
        if self.persist_path:
            self._save()
        return True

    def __len__(self) -> int:
        return len(self.ids)

    def _save(self) -> None:
        if self.vectors is None or not self.persist_path:
            return
        try:
            np.savez_compressed(
                self.persist_path,
                ids=np.array(self.ids, dtype=object),
                vectors=self.vectors,
                metadatas=np.array(self.metadatas, dtype=object),
            )
        except Exception as e:
            print(f"[VectorStore] Warnung beim Speichern: {e}")

    def _load(self) -> None:
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            data = np.load(self.persist_path, allow_pickle=True)
            self.ids = list(data["ids"])
            self.vectors = data["vectors"]
            self.metadatas = list(data["metadatas"])
        except Exception as e:
            print(f"[VectorStore] Warnung beim Laden: {e}")
