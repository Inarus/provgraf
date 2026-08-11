"""Embeddings (RAG): the mmlw model via sentence-transformers, run locally.

The torch/sentence-transformers import is LAZY (inside the functions) — the provgraf core
(init/ingest/check/structure) never loads torch. It is needed only for `embed`/`search`/`similar`.
mmlw convention: queries carry the "zapytanie: " prefix, passages carry none, embeddings are normalized.
"""
import json
import time
from pathlib import Path

from provgraf.config import Settings

_MODEL: dict = {}
_GLOSS: dict = {}
_RERANK: dict = {}
# marker of the models' last use — the MCP daemon releases them when idle (idle unload)
last_used: float = 0.0


def unload() -> bool:
    """Release the RAG models from memory (idle unload in the daemon). True = something was freed."""
    had = bool(_MODEL) or bool(_RERANK)
    _MODEL.clear()
    _RERANK.clear()
    if had:
        import gc
        gc.collect()
        try:
            import torch
            if torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:  # noqa: BLE001
            pass
    return had


def _model():
    if "m" not in _MODEL:
        from sentence_transformers import SentenceTransformer
        s = Settings()
        try:
            _MODEL["m"] = SentenceTransformer(s.embedding_model, device=s.embedding_device)
        except Exception:  # noqa: BLE001 — fallback when MPS/CUDA is unavailable
            _MODEL["m"] = SentenceTransformer(s.embedding_model, device="cpu")
    return _MODEL["m"]


def embed_passage(text: str):
    """Passage vector (fact/description) — no prefix."""
    global last_used
    last_used = time.time()
    return _model().encode(text, normalize_embeddings=True)


def embed_query(text: str):
    """Query vector — mmlw requires the 'zapytanie: ' prefix (a model protocol constant)."""
    global last_used
    last_used = time.time()
    return _model().encode("zapytanie: " + text, normalize_embeddings=True)


def _reranker():
    if "r" not in _RERANK:
        from sentence_transformers import CrossEncoder
        s = Settings()
        try:
            _RERANK["r"] = CrossEncoder(s.reranker_model, device=s.embedding_device)
        except Exception:  # noqa: BLE001
            _RERANK["r"] = CrossEncoder(s.reranker_model, device="cpu")
    return _RERANK["r"]


def rerank(query: str, passages: list[str]) -> list[float]:
    """Cross-encoder (stage 2): relevance score per (query, passage). Higher = more relevant."""
    if not passages:
        return []
    global last_used
    last_used = time.time()
    scores = _reranker().predict([(query, p) for p in passages])
    return [float(x) for x in scores]


def _load_gloss() -> dict:
    if "g" not in _GLOSS:
        path = Path(__file__).resolve().parents[2] / "config" / "gloss.json"
        try:
            _GLOSS["g"] = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _GLOSS["g"] = {"_default": {}, "fields": {}}
    return _GLOSS["g"]


def _gloss_for(field: str, group: str) -> dict:
    """Dictionary entry: matched by field (equality/prefix+'_', longest wins),
    falling back to the group, then _default."""
    g = _load_gloss()
    fields = g.get("fields", {})
    best = None
    for k in fields:
        if (field == k or field.startswith(k + "_")) and (best is None or len(k) > len(best)):
            best = k
    if best:
        return fields[best]
    if group in fields:
        return fields[group]
    return g.get("_default", {})


def compose_text(row) -> str:
    """Auto-gloss of a fact (contextual retrieval). A curated `gloss` wins; otherwise it is composed
    from the field dictionary (config/gloss.json) + location (collection) + value + synonyms + basis + source."""
    if row.get("gloss"):
        return row["gloss"]
    if row.get("kind") == "document":
        parts = [f"Source document: {row.get('label') or row['qname']}."]
        if row.get("issuer"):
            parts.append(f"Issuer: {row['issuer']}.")
        return " ".join(parts)
    if row.get("kind") == "question":
        # decision precedent: the question + (if resolved) the answer with its basis
        try:
            qv = json.loads(row.get("val") or "{}")
        except (json.JSONDecodeError, TypeError):
            qv = {}
        parts = [f"Structural question: {qv.get('pytanie') or row.get('label') or row['qname']}."]
        if qv.get("rozstrzygniecie"):
            parts.append(f"Resolution: {qv['rozstrzygniecie']}.")
        if qv.get("podstawa"):
            parts.append(f"Basis: {qv['podstawa']}.")
        return " ".join(parts)
    qn = row["qname"]
    local = qn.split(":", 1)[1] if ":" in qn else qn
    group, _, field = local.partition(".")
    g = _gloss_for(field, group)
    parts: list[str] = []
    if g.get("desc"):
        parts.append(g["desc"] + ".")
    if row.get("collections"):
        parts.append(f"Applies to: {row['collections']}.")
    val = row.get("val")
    if val is not None and val != "":
        unit = f" {row['unit']}" if row.get("unit") else ""
        parts.append(f"{row.get('label') or qn}: {val}{unit}.")
    elif row.get("label"):
        parts.append(row["label"] + ".")
    if g.get("syn"):
        parts.append(f"({g['syn']})")
    # NOTE: note/source deliberately kept OUT of the embedded text — that is provenance (a separate
    # signal), and a long note blurs the vector (a regression on "deposit in Skopanie"). Only the
    # semantic signal remains.
    return " ".join(parts)
