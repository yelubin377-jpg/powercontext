"""HTTP adapter for deterministic, batched Memory reranking."""

from __future__ import annotations

import json
import math

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage


class HTTPMemoryReranker:
    policy_id = "http-cross-encoder-v1"

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.endpoint = endpoint
        self.client = client
        self.batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self, query: str, candidates: tuple[MemoryHit, ...], limit: int, /
    ) -> MemoryRerankDecision:
        if not candidates:
            raise ValueError("rerank requires at least one candidate")
        if type(limit) is not int or not 1 <= limit <= len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")
        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            payload = {
                "query": query,
                "candidates": [
                    {"rank": start + index + 1, "text": item.text} for index, item in enumerate(batch)
                ],
            }
            response = await self.client.post(self.endpoint, json=payload)
            response.raise_for_status()
            requests += 1
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise ValueError("reranker response must be JSON") from error
            if not isinstance(body, dict) or set(body) != {"scores"} or not isinstance(body["scores"], list):
                raise ValueError("invalid reranker response schema")
            expected = {start + index + 1 for index in range(len(batch))}
            seen: set[int] = set()
            for item in body["scores"]:
                if not isinstance(item, dict) or set(item) != {"rank", "score"}:
                    raise ValueError("invalid reranker score schema")
                rank, score = item["rank"], item["score"]
                if type(rank) is not int or rank not in expected or rank in seen:
                    raise ValueError("invalid reranker rank coverage")
                if not isinstance(score, (int, float)) or isinstance(score, bool):
                    raise ValueError("invalid reranker score")
                try:
                    normalized_score = float(score)
                except (OverflowError, ValueError) as error:
                    raise ValueError("invalid reranker score") from error
                if not math.isfinite(normalized_score):
                    raise ValueError("invalid reranker score")
                seen.add(rank)
                scores[rank] = normalized_score
            if seen != expected:
                raise ValueError("incomplete reranker rank coverage")
        selected = tuple(sorted(scores, key=lambda rank: (-scores[rank], rank))[:limit])
        return MemoryRerankDecision(selected_ranks=selected, usage=InferenceUsage(requests=requests))


__all__ = ["HTTPMemoryReranker"]
