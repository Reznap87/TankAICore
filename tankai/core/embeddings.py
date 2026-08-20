"""
Embedding-Schicht für TankAI.

Austauschbare Interface:
  - HashingEmbedder   (aktueller Default, keine Extra-Deps)
  - TorchBOWEmbedder  (etwas stärker, nutzt PyTorch)
  - SentenceTransformerEmbedder  (sobald sentence-transformers installiert)
  - OpenAIEmbedder    (wenn API-Key vorhanden)

Alle implementieren BaseEmbedder.embed(text) -> np.ndarray
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Optional, Sequence

import numpy as np


def _tokenize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[^\w\säöüß]", " ", text, flags=re.UNICODE)
    return [t for t in text.split() if len(t) > 1]


class BaseEmbedder(ABC):
    """Einheitliche Schnittstelle für alle Embedder."""

    dim: int

    @abstractmethod
    def embed(self, text: str) -> np.ndarray:
        """Gibt einen L2-normalisierten Vektor der Form (dim,) zurück."""
        ...

    def embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self.embed(t) for t in texts])


# ────────────────────────── Hashing (Default) ──────────────────────────

class HashingEmbedder(BaseEmbedder):
    """
    Deterministischer Hashing-Trick + TF.
    Keine externen Modelle, schnell, gut genug für Prototypen.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str) -> np.ndarray:
        tokens = _tokenize(text)
        vec = np.zeros(self.dim, dtype=np.float32)
        if not tokens:
            return vec

        counts = Counter(tokens)
        for token, count in counts.items():
            h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if ((h // self.dim) % 2 == 0) else -1.0
            vec[idx] += sign * math.log1p(count)

        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec


# ────────────────────────── Torch Bag-of-Words ──────────────────────────

class TorchBOWEmbedder(BaseEmbedder):
    """
    Etwas reichhaltigerer lokaler Embedder.
    Baut ein kleines Vokabular on-the-fly und nutzt eine
    feste Zufallsprojektion (ähnlich einem frozen linear layer).
    """

    def __init__(self, dim: int = 384, seed: int = 42) -> None:
        import torch

        self.dim = dim
        self._torch = torch
        gen = torch.Generator().manual_seed(seed)
        # Feste Projektionsmatrix (wird nie trainiert)
        self.projection = torch.randn(4096, dim, generator=gen)
        self.projection = self.projection / self.projection.norm(dim=0, keepdim=True)
        self._vocab: dict[str, int] = {}
        self._max_vocab = 4096

    def _token_id(self, token: str) -> int:
        if token not in self._vocab:
            if len(self._vocab) >= self._max_vocab:
                # Hash-Fallback wenn Vokabular voll
                h = int(hashlib.md5(token.encode()).hexdigest(), 16)
                return h % self._max_vocab
            self._vocab[token] = len(self._vocab)
        return self._vocab[token]

    def embed(self, text: str) -> np.ndarray:
        torch = self._torch
        tokens = _tokenize(text)
        bow = torch.zeros(self._max_vocab)
        for t in tokens:
            bow[self._token_id(t)] += 1.0
        # log-TF
        bow = torch.log1p(bow)
        vec = bow @ self.projection
        vec = vec / (vec.norm() + 1e-8)
        return vec.detach().numpy().astype(np.float32)


# ────────────────────────── Adapter für echte Modelle ──────────────────────────

class SentenceTransformerEmbedder(BaseEmbedder):
    """
    Wrapper für sentence-transformers.
    Nur nutzbar, wenn das Paket installiert ist:

        pip install sentence-transformers

    Beispiel:
        embedder = SentenceTransformerEmbedder("all-MiniLM-L6-v2")
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "sentence-transformers ist nicht installiert. "
                "Installiere es mit: pip install sentence-transformers"
            ) from e

        self.model = SentenceTransformer(model_name)
        self.dim = self.model.get_sentence_embedding_dimension()

    def embed(self, text: str) -> np.ndarray:
        vec = self.model.encode(text, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32)

    def embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        vecs = self.model.encode(list(texts), normalize_embeddings=True)
        return np.asarray(vecs, dtype=np.float32)


class OpenAIEmbedder(BaseEmbedder):
    """
    Wrapper für OpenAI Embeddings.
    Benötigt: pip install openai  +  OPENAI_API_KEY
    """

    def __init__(self, model: str = "text-embedding-3-small", dim: int = 1536) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ImportError("openai Paket fehlt: pip install openai") from e

        self.client = OpenAI()
        self.model = model
        self.dim = dim

    def embed(self, text: str) -> np.ndarray:
        resp = self.client.embeddings.create(input=text, model=self.model)
        vec = np.asarray(resp.data[0].embedding, dtype=np.float32)
        # L2-Norm
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec


def get_embedder(name: str = "hashing", **kwargs) -> BaseEmbedder:
    """
    Factory.

    name:
      - "hashing" (default)
      - "torch"
      - "minilm" / "sentence-transformers"
      - "openai"
    """
    name = name.lower()
    if name in ("hashing", "hash", "default"):
        return HashingEmbedder(**kwargs)
    if name in ("torch", "bow", "torchbow"):
        return TorchBOWEmbedder(**kwargs)
    if name in ("minilm", "sentence-transformers", "st"):
        return SentenceTransformerEmbedder(**kwargs)
    if name in ("openai", "ada"):
        return OpenAIEmbedder(**kwargs)
    raise ValueError(f"Unbekannter Embedder: {name}")
