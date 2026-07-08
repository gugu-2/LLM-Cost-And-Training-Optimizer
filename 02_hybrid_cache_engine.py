"""
Cerberus Hybrid Caching Engine — Core Implementation
======================================================
Foundational backend module for the Hybrid Caching Engine described in
01_ARCHITECTURE_BLUEPRINT.md.

Responsibilities:
  1. Prefix/Radix caching for shared system prompts / large contexts.
  2. Semantic caching via vector similarity (Qdrant) with dynamic thresholds.
  3. Cache-poisoning / prompt-injection resistant write-gating.
  4. Async fire-and-forget logging into the Data Flywheel (queue-backed).

This is production-shaped scaffold code: real async I/O, real interfaces,
swap-in adapters for Qdrant/Redis/NATS. In a real Rust gateway, this Python
service would sit behind gRPC and be called by the hot-path gateway for the
embedding + vector-search steps only; the prefix trie check would ideally be
inlined in Rust for lowest latency. It's presented here as a single coherent
Python module for clarity and because the semantic/security logic is most
naturally expressed and iterated on in Python.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Any

import numpy as np

logger = logging.getLogger("cerberus.cache_engine")

# ---------------------------------------------------------------------------
# 1. PREFIX / RADIX TRIE CACHE
# ---------------------------------------------------------------------------
# Mirrors the idea behind vLLM/SGLang's automatic prefix caching, but at the
# *network* cache layer (before we even decide to hit an LLM). Useful when
# many tenants share a large system prompt / RAG context and only the tail
# of the prompt (the user's actual question) differs.

class RadixNode:
    __slots__ = ("children", "is_terminal", "token_id", "kv_ref", "last_used")

    def __init__(self):
        self.children: dict[str, "RadixNode"] = {}
        self.is_terminal: bool = False
        self.token_id: Optional[str] = None   # opaque handle to cached KV/result
        self.kv_ref: Optional[str] = None      # pointer into Redis/GPU KV store
        self.last_used: float = time.time()


class PrefixCache:
    """
    Radix trie keyed on whitespace-tokenized prefix chunks (in production,
    key on actual model tokenizer token IDs for exact prefill reuse).
    """

    def __init__(self, chunk_size_chars: int = 64, max_nodes: int = 500_000):
        self.root = RadixNode()
        self.chunk_size_chars = chunk_size_chars
        self.max_nodes = max_nodes
        self._node_count = 0
        self._lock = asyncio.Lock()

    def _chunks(self, text: str) -> list[str]:
        # Chunk by fixed char windows as a stand-in for token boundaries.
        return [text[i:i + self.chunk_size_chars]
                for i in range(0, len(text), self.chunk_size_chars)]

    async def lookup(self, prompt: str) -> tuple[int, Optional[str], str]:
        """
        Walk the trie as far as possible.
        Returns: (matched_char_depth, kv_ref_of_deepest_match, remaining_suffix)
        """
        node = self.root
        chunks = self._chunks(prompt)
        matched_depth = 0
        deepest_kv_ref = None
        consumed_chunks = 0

        async with self._lock:
            for chunk in chunks:
                child = node.children.get(chunk)
                if child is None:
                    break
                node = child
                node.last_used = time.time()
                matched_depth += len(chunk)
                consumed_chunks += 1
                if node.kv_ref:
                    deepest_kv_ref = node.kv_ref

        remaining_suffix = "".join(chunks[consumed_chunks:])
        return matched_depth, deepest_kv_ref, remaining_suffix

    async def insert(self, prompt: str, kv_ref: str) -> None:
        node = self.root
        async with self._lock:
            for chunk in self._chunks(prompt):
                if chunk not in node.children:
                    if self._node_count >= self.max_nodes:
                        self._evict_lru()
                    node.children[chunk] = RadixNode()
                    self._node_count += 1
                node = node.children[chunk]
            node.is_terminal = True
            node.kv_ref = kv_ref

    def _evict_lru(self) -> None:
        # Simplified: real impl needs a global LRU heap over leaf nodes.
        # Left as a hook — production version should track a min-heap keyed
        # on last_used across all terminal nodes for O(log n) eviction.
        logger.warning("PrefixCache at capacity — eviction triggered (stub).")


# ---------------------------------------------------------------------------
# 2. EMBEDDING CLIENT (adapter over a TEI / ONNX embedding microservice)
# ---------------------------------------------------------------------------

class EmbeddingClient:
    """
    Thin async adapter over a HF Text-Embeddings-Inference (or ONNX) server.
    Kept separate so the gateway can call this over gRPC/HTTP in production.
    """

    def __init__(self, endpoint: str = "http://tei-service:8080/embed", dim: int = 384):
        self.endpoint = endpoint
        self.dim = dim

    async def embed(self, text: str) -> np.ndarray:
        # Placeholder deterministic pseudo-embedding for local testing without
        # a live TEI server. Replace with an actual HTTP call, e.g.:
        #
        #   async with httpx.AsyncClient() as client:
        #       resp = await client.post(self.endpoint, json={"inputs": text})
        #       vec = np.array(resp.json()["embedding"], dtype=np.float32)
        #
        h = hashlib.sha256(text.encode()).digest()
        rng = np.random.default_rng(int.from_bytes(h[:8], "little"))
        vec = rng.normal(size=self.dim).astype(np.float32)
        return vec / np.linalg.norm(vec)


# ---------------------------------------------------------------------------
# 3. VECTOR STORE ADAPTER (Qdrant-shaped interface)
# ---------------------------------------------------------------------------

@dataclass
class CacheHit:
    response: str
    similarity: float
    vector_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorCacheStore:
    """
    Adapter over Qdrant. Swap the internals for a real qdrant-client:

        from qdrant_client import AsyncQdrantClient
        self.client = AsyncQdrantClient(url=..., api_key=...)

    Kept as an in-memory stub here so this module runs standalone for review
    and unit testing.
    """

    def __init__(self, collection: str = "semantic_cache"):
        self.collection = collection
        self._store: dict[str, dict[str, Any]] = {}   # id -> {vector, payload}

    async def search(
        self,
        tenant_id: str,
        query_vec: np.ndarray,
        top_k: int = 3,
    ) -> list[CacheHit]:
        results: list[CacheHit] = []
        for vid, entry in self._store.items():
            if entry["payload"].get("tenant_id") != tenant_id:
                continue  # mandatory tenant isolation filter
            sim = float(np.dot(query_vec, entry["vector"]))  # cosine (unit vectors)
            results.append(CacheHit(
                response=entry["payload"]["response"],
                similarity=sim,
                vector_id=vid,
                metadata=entry["payload"],
            ))
        results.sort(key=lambda h: h.similarity, reverse=True)
        return results[:top_k]

    async def upsert(self, tenant_id: str, vector: np.ndarray, payload: dict[str, Any]) -> str:
        vid = str(uuid.uuid4())
        payload = {**payload, "tenant_id": tenant_id, "created_at": time.time()}
        self._store[vid] = {"vector": vector, "payload": payload}
        return vid


# ---------------------------------------------------------------------------
# 4. CACHE SECURITY LAYER — poisoning / prompt-injection resistant writes
# ---------------------------------------------------------------------------

class CacheSecurityGate:
    """
    Gate applied BEFORE a response is written into the semantic cache, and
    a lighter check applied on READ to reject clearly anomalous matches.

    Two concerns handled:
      (a) Prompt-injection hijack: don't let an adversarial prompt cause the
          model to emit content that then gets cached and served to OTHER
          users under similar-looking future queries.
      (b) Cache poisoning: reject writes whose embedding falls in an outlier
          region of the tenant's existing embedding distribution (crude
          drift signal), or that carry known injection markers.
    """

    INJECTION_MARKERS = (
        "ignore previous instructions",
        "disregard the system prompt",
        "reveal your system prompt",
        "you are now in developer mode",
    )

    def __init__(self, min_response_len: int = 1, drift_z_threshold: float = 4.0):
        self.min_response_len = min_response_len
        self.drift_z_threshold = drift_z_threshold

    def _looks_like_injection_artifact(self, prompt: str, response: str) -> bool:
        low_prompt = prompt.lower()
        return any(marker in low_prompt for marker in self.INJECTION_MARKERS)

    def _embedding_is_outlier(self, vector: np.ndarray, tenant_vec_stats: Optional[dict]) -> bool:
        if not tenant_vec_stats:
            return False
        mean = tenant_vec_stats["mean"]
        std = tenant_vec_stats["std"] + 1e-6
        z = float(np.linalg.norm((vector - mean) / std))
        return z > self.drift_z_threshold

    def approve_write(
        self,
        prompt: str,
        response: str,
        vector: np.ndarray,
        tenant_vec_stats: Optional[dict] = None,
    ) -> tuple[bool, str]:
        if len(response.strip()) < self.min_response_len:
            return False, "empty_response"
        if self._looks_like_injection_artifact(prompt, response):
            return False, "injection_marker_detected"
        if self._embedding_is_outlier(vector, tenant_vec_stats):
            return False, "embedding_drift_outlier"
        return True, "ok"

    def approve_read(self, hit: CacheHit, dynamic_threshold: float) -> bool:
        return hit.similarity >= dynamic_threshold


# ---------------------------------------------------------------------------
# 5. DYNAMIC THRESHOLD CONTROLLER
# ---------------------------------------------------------------------------

class DynamicThresholdController:
    """
    Per-tenant, per-domain adaptive cosine-similarity threshold.
    Starts conservative; tightens on negative feedback signals
    (thumbs-down / low judge score attributed to a cache hit),
    loosens slowly on sustained positive signal. This is a simplified
    exponential controller — production version should be a proper
    contextual bandit (e.g. LinUCB) keyed on tenant+domain.
    """

    def __init__(self, base_threshold: float = 0.90, floor: float = 0.80, ceiling: float = 0.985):
        self.base_threshold = base_threshold
        self.floor = floor
        self.ceiling = ceiling
        self._tenant_thresholds: dict[str, float] = {}

    def get(self, tenant_id: str) -> float:
        return self._tenant_thresholds.get(tenant_id, self.base_threshold)

    def report_feedback(self, tenant_id: str, was_good_hit: bool) -> None:
        current = self.get(tenant_id)
        if was_good_hit:
            new_val = max(self.floor, current - 0.001)   # loosen slowly
        else:
            new_val = min(self.ceiling, current + 0.02)  # tighten fast
        self._tenant_thresholds[tenant_id] = new_val


# ---------------------------------------------------------------------------
# 6. ASYNC FLYWHEEL LOGGER (fire-and-forget queue publish)
# ---------------------------------------------------------------------------

class FlywheelLogger:
    """
    Publishes prompt/response events to the async pipeline (NATS/Kafka).
    Never blocks the hot path — the gateway calls `.log_nowait()` and moves on.
    """

    def __init__(self, publish_fn=None):
        # publish_fn: async callable(topic: str, payload: dict) -> None
        # In production, bind this to a NATS/Kafka producer client.
        self._publish_fn = publish_fn or self._default_stdout_publish
        self._task_set: set[asyncio.Task] = set()

    async def _default_stdout_publish(self, topic: str, payload: dict) -> None:
        logger.info("FLYWHEEL[%s]: %s", topic, json.dumps(payload)[:300])

    def log_nowait(self, event_type: str, payload: dict) -> None:
        task = asyncio.create_task(self._publish_fn(f"flywheel.{event_type}", payload))
        self._task_set.add(task)
        task.add_done_callback(self._task_set.discard)


# ---------------------------------------------------------------------------
# 7. THE ORCHESTRATOR — ties everything together for one request
# ---------------------------------------------------------------------------

@dataclass
class CacheLookupResult:
    hit: bool
    response: Optional[str] = None
    source: str = "miss"          # "prefix" | "semantic" | "miss"
    similarity: Optional[float] = None
    prefix_depth: int = 0
    trace: dict[str, Any] = field(default_factory=dict)


class HybridCacheOrchestrator:
    def __init__(
        self,
        prefix_cache: PrefixCache,
        embed_client: EmbeddingClient,
        vector_store: VectorCacheStore,
        security_gate: CacheSecurityGate,
        threshold_ctrl: DynamicThresholdController,
        flywheel: FlywheelLogger,
    ):
        self.prefix_cache = prefix_cache
        self.embed_client = embed_client
        self.vector_store = vector_store
        self.security_gate = security_gate
        self.threshold_ctrl = threshold_ctrl
        self.flywheel = flywheel

    async def handle_request(
        self,
        tenant_id: str,
        prompt: str,
        llm_call_fn,  # async callable(remaining_prompt: str) -> str  — used ONLY on miss
    ) -> CacheLookupResult:
        request_id = str(uuid.uuid4())
        t0 = time.perf_counter()

        # --- Step 1: prefix/radix check (system prompt / shared context reuse) ---
        prefix_depth, kv_ref, remaining_suffix = await self.prefix_cache.lookup(prompt)
        if kv_ref and prefix_depth == len(prompt):
            # Exact full-prompt prefix match -> treat as a direct hit upstream
            # (in a real system this still might route to the LLM using the
            # reused KV cache for a much cheaper generation-only pass).
            pass  # fallthrough to semantic check for the response itself

        # --- Step 2: embed remaining (or full) prompt for semantic lookup ---
        text_to_embed = remaining_suffix if remaining_suffix else prompt
        query_vec = await self.embed_client.embed(text_to_embed)

        # --- Step 3: semantic vector search, tenant-isolated ---
        candidates = await self.vector_store.search(tenant_id, query_vec, top_k=3)
        threshold = self.threshold_ctrl.get(tenant_id)

        for hit in candidates:
            if self.security_gate.approve_read(hit, threshold):
                elapsed_ms = (time.perf_counter() - t0) * 1000
                self.flywheel.log_nowait("cache_hit", {
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "similarity": hit.similarity,
                    "vector_id": hit.vector_id,
                    "prefix_depth": prefix_depth,
                    "latency_ms": elapsed_ms,
                })
                return CacheLookupResult(
                    hit=True,
                    response=hit.response,
                    source="semantic",
                    similarity=hit.similarity,
                    prefix_depth=prefix_depth,
                    trace={"vector_id": hit.vector_id, "latency_ms": elapsed_ms, "threshold_used": threshold},
                )

        # --- Step 4: cache miss -> call the LLM router (external) ---
        response = await llm_call_fn(prompt)

        # --- Step 5: security-gated async write-back (never blocks the response) ---
        asyncio.create_task(self._write_back(tenant_id, prompt, response, query_vec, request_id))

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return CacheLookupResult(
            hit=False,
            response=response,
            source="miss",
            prefix_depth=prefix_depth,
            trace={"latency_ms": elapsed_ms},
        )

    async def _write_back(
        self,
        tenant_id: str,
        prompt: str,
        response: str,
        vector: np.ndarray,
        request_id: str,
    ) -> None:
        approved, reason = self.security_gate.approve_write(prompt, response, vector)
        if not approved:
            logger.warning("Cache write REJECTED (%s) for request %s", reason, request_id)
            self.flywheel.log_nowait("cache_write_rejected", {
                "request_id": request_id, "tenant_id": tenant_id, "reason": reason,
            })
            return

        vector_id = await self.vector_store.upsert(
            tenant_id, vector, {"prompt": prompt, "response": response}
        )
        await self.prefix_cache.insert(prompt, kv_ref=vector_id)

        self.flywheel.log_nowait("cache_write", {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "vector_id": vector_id,
            "prompt": prompt,
            "response": response,
        })


# ---------------------------------------------------------------------------
# Example wiring (would live in the gateway's startup / DI container)
# ---------------------------------------------------------------------------

async def _demo():
    logging.basicConfig(level=logging.INFO)

    orchestrator = HybridCacheOrchestrator(
        prefix_cache=PrefixCache(),
        embed_client=EmbeddingClient(),
        vector_store=VectorCacheStore(),
        security_gate=CacheSecurityGate(),
        threshold_ctrl=DynamicThresholdController(),
        flywheel=FlywheelLogger(),
    )

    async def fake_llm(prompt: str) -> str:
        await asyncio.sleep(0.05)  # simulate provider latency
        return f"[LLM ANSWER for]: {prompt[:60]}..."

    # First call -> miss, gets cached
    r1 = await orchestrator.handle_request("tenant_acme", "What is the capital of France?", fake_llm)
    print("Call 1:", r1.source, r1.response)

    await asyncio.sleep(0.1)  # allow async write-back to finish

    # Second, near-identical call -> should hit semantic cache
    r2 = await orchestrator.handle_request("tenant_acme", "What is the capital of France?", fake_llm)
    print("Call 2:", r2.source, r2.response, r2.similarity)


if __name__ == "__main__":
    asyncio.run(_demo())
