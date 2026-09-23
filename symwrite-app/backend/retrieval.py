"""Local, profile-specific retrieval with a content-addressed SQLite cache."""

import hashlib
import json
import math
import re
import sqlite3
import threading
from collections import Counter
from pathlib import Path

from .configuration import Profile

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
STOPWORDS = set(
    "a an and are as at be by for from has have i in is it its of on or that the this to was we were with".split()
)


def tokens(text):
    return [
        word for word in re.findall(r"[\w'-]+", text.lower()) if word not in STOPWORDS
    ]


class Retriever:
    def __init__(
        self, profile: Profile, directory: Path, mode="semantic", encoder=None
    ):
        if mode not in {"semantic", "lexical"}:
            raise ValueError("SYMWRITE_RETRIEVAL must be semantic or lexical")
        self.mode = mode
        self.profile = profile
        self.encoder = encoder
        self.lock = threading.Lock()
        # Chunk long samples; every excerpt preserves its human-readable source.
        self.chunks = []
        for source in profile.sources:
            for i in range(0, len(source.text), 750):
                self.chunks.append(
                    {
                        "id": f"{source.id}:{i // 750 + 1}",
                        "source_id": source.id,
                        "title": source.title,
                        "kind": source.kind,
                        "text": source.text[i : i + 750],
                    }
                )
        signature = json.dumps(
            {"version": 1, "model": EMBEDDING_MODEL, "chunks": self.chunks},
            sort_keys=True,
        )
        self.fingerprint = hashlib.sha256(signature.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        self.db_path = directory / f"{self.fingerprint}.sqlite3"
        self.cache_dir = directory / "models"
        self.vectors = None

    def _load(self):
        if self.encoder is None:
            from fastembed import TextEmbedding

            self.encoder = TextEmbedding(
                model_name=EMBEDDING_MODEL, cache_dir=str(self.cache_dir), threads=2
            )
        if self.vectors is not None:
            return
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS vectors (fingerprint TEXT PRIMARY KEY, payload TEXT)"
            )
            row = db.execute(
                "SELECT payload FROM vectors WHERE fingerprint=?", (self.fingerprint,)
            ).fetchone()
            if row:
                vectors = json.loads(row[0])
            else:
                vectors = [
                    list(map(float, vector))
                    for vector in self.encoder.passage_embed(
                        [chunk["text"] for chunk in self.chunks]
                    )
                ]
                db.execute(
                    "INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                    (self.fingerprint, json.dumps(vectors)),
                )
            if len(vectors) != len(self.chunks) or any(len(v) != 384 for v in vectors):
                raise ValueError(
                    "Embedding cache does not match its profile or model; remove the cache and restart"
                )
            self.vectors = vectors

    def warmup(self):
        if self.mode == "semantic":
            with self.lock:
                self._load()

    def search(self, query: str, top_k=4, budget=2800):
        if not query.strip():
            return []
        if self.mode == "semantic":
            with self.lock:
                self._load()
                vector = list(next(iter(self.encoder.query_embed(query[-3000:]))))
                if len(vector) != 384:
                    raise ValueError(
                        "Query embedding dimension does not match the index"
                    )
                scores = [cosine(vector, record) for record in self.vectors]
            threshold = 0.35
        else:
            query_words = Counter(tokens(query[-3000:]))
            scores = [
                sparse_cosine(query_words, Counter(tokens(chunk["text"])))
                for chunk in self.chunks
            ]
            threshold = 0.05
        ranked = sorted(
            zip(scores, self.chunks), key=lambda pair: pair[0], reverse=True
        )
        result = []
        for score, chunk in ranked:
            if score < threshold or len(result) >= top_k or budget <= 0:
                break
            excerpt = chunk["text"][:budget]
            result.append({**chunk, "text": excerpt, "score": round(float(score), 3)})
            budget -= len(excerpt)
        return result


def cosine(a, b):
    denominator = math.sqrt(
        sum(float(x) ** 2 for x in a) * sum(float(x) ** 2 for x in b)
    )
    return (
        sum(float(x) * float(y) for x, y in zip(a, b)) / denominator
        if denominator
        else 0.0
    )


def sparse_cosine(a, b):
    denominator = math.sqrt(
        sum(x * x for x in a.values()) * sum(x * x for x in b.values())
    )
    return (
        sum(count * b.get(word, 0) for word, count in a.items()) / denominator
        if denominator
        else 0.0
    )
