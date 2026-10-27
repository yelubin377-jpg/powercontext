# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""HTTP scoring-service reranker with cross-batch global selection."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

DEFAULT_HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"
_BATCH_SCORES_KEY = "scores"
_BATCH_RANK_KEY = "rank"
_BATCH_SCORE_KEY = "score"


class HTTPMemoryReranker:
    """Score Memory candidates through one HTTP rerank service.

    Candidates are split into batches of at most ``batch_size`` global ranks. Each
    global rank is sent exactly once with its original rank and text; service scores
    are mapped back to the original candidates and the final selection is made over
    the complete pool in one global ordering (score descending, original rank
    ascending on ties).
    """

    policy_id: str

    def __init__(
        self,
        endpoint: str,
        /,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = DEFAULT_HTTP_RERANK_POLICY_ID,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be a positive integer")  # noqa: TRY003
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Batch-score the coarse pool and return one global stable selection."""

        _validate_bounds(len(candidates), limit)
        score_by_rank: dict[int, float] = {}
        request_count = 0
        for batch_start in range(0, len(candidates), self._batch_size):
            batch = candidates[batch_start : batch_start + self._batch_size]
            batch_ranks = tuple(range(batch_start + 1, batch_start + len(batch) + 1))
            batch_scores = await self._score_batch(query, batch, batch_ranks)
            request_count += 1
            score_by_rank.update(batch_scores)
        selected_ranks = tuple(
            rank
            for rank, _score in sorted(
                score_by_rank.items(),
                key=lambda item: (-item[1], item[0]),
            )[:limit]
        )
        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=InferenceUsage(requests=request_count),
        )

    async def _score_batch(
        self,
        query: str,
        batch: tuple[MemoryHit, ...],
        batch_ranks: tuple[int, ...],
    ) -> dict[int, float]:
        """Send one batch and validate the response covers exactly its ranks."""

        payload: dict[str, Any] = {
            "query": query,
            "candidates": [
                {_BATCH_RANK_KEY: rank, "text": candidate.text}
                for rank, candidate in zip(batch_ranks, batch, strict=True)
            ],
        }
        response = await self._client.post(self._endpoint, json=payload)
        response.raise_for_status()
        return _parse_scores(response.content, expected_ranks=frozenset(batch_ranks))


def _parse_scores(content: bytes, *, expected_ranks: frozenset[int]) -> dict[int, float]:
    """Parse one batch response, rejecting any malformed or non-covering schema."""

    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("rerank response is not valid JSON") from error  # noqa: TRY003
    if not isinstance(payload, dict) or set(payload) != {_BATCH_SCORES_KEY}:
        raise ValueError("rerank response must contain exactly the scores field")  # noqa: TRY003
    raw_scores = payload[_BATCH_SCORES_KEY]
    if not isinstance(raw_scores, list):
        raise ValueError("rerank scores must be a list")  # noqa: TRY003
    score_by_rank: dict[int, float] = {}
    for raw_score in raw_scores:
        rank, score = _parse_score_record(raw_score)
        if rank not in expected_ranks or rank in score_by_rank:
            raise ValueError(  # noqa: TRY003
                "rerank scores must cover every batch rank exactly once"
            )
        score_by_rank[rank] = score
    if set(score_by_rank) != expected_ranks:
        raise ValueError("rerank scores must cover every batch rank exactly once")  # noqa: TRY003
    return score_by_rank


def _parse_score_record(raw_score: Any) -> tuple[int, float]:
    if not isinstance(raw_score, dict) or set(raw_score) != {
        _BATCH_RANK_KEY,
        _BATCH_SCORE_KEY,
    }:
        raise ValueError("each score record must contain exactly rank and score")  # noqa: TRY003
    rank = raw_score[_BATCH_RANK_KEY]
    if type(rank) is not int:
        raise ValueError("rerank score rank must be an integer")  # noqa: TRY003
    score = raw_score[_BATCH_SCORE_KEY]
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("rerank score must be a finite number")  # noqa: TRY003
    numeric_score = float(score)
    if not math.isfinite(numeric_score):
        raise ValueError("rerank score must be a finite number")  # noqa: TRY003
    return rank, numeric_score


def _validate_bounds(candidate_count: int, limit: int) -> None:
    if candidate_count < 1:
        raise ValueError("rerank requires at least one candidate")  # noqa: TRY003
    if limit < 1 or limit > candidate_count:
        raise ValueError("rerank limit must be between one and the candidate count")  # noqa: TRY003


__all__ = ["DEFAULT_HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
