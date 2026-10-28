"""Concurrent embedding coalescing and bounded LRU vector cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass
class _Batch:
    """Metadata shared by all texts sent in one provider request."""

    batch_id: int
    usage: InferenceUsage | None = None


@dataclass
class _Pending:
    future: asyncio.Future[tuple[float, ...]]
    batch: _Batch | None = None


class CoalescingEmbeddingModel:
    """Coalesce concurrent text requests and cache successful vectors."""

    def __init__(
        self,
        delegate: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1:
            raise ValueError("invalid embedding coalescing configuration")
        if type(cache_size) is not int or cache_size < 0:
            raise ValueError("invalid embedding coalescing configuration")
        if coalesce_ms < 0:
            raise ValueError("invalid embedding coalescing configuration")
        self.delegate = delegate
        self.profile = delegate.profile
        self.max_batch_size = max_batch_size
        self.coalesce_ms = coalesce_ms
        self.cache_size = cache_size
        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._pending: dict[str, _Pending] = {}
        self._batch_serial = 0
        self._lock = asyncio.Lock()

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(vectors=())

        cached: dict[str, tuple[float, ...]] = {}
        pending: dict[str, _Pending] = {}
        owners: list[str] = []
        async with self._lock:
            for text in dict.fromkeys(texts):
                vector = self._cache.get(text)
                if vector is not None:
                    self._cache.move_to_end(text)
                    cached[text] = vector
                    continue
                existing = self._pending.get(text)
                if existing is None:
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future[tuple[float, ...]] = loop.create_future()
                    future.add_done_callback(_consume_future_exception)
                    existing = _Pending(future)
                    self._pending[text] = existing
                    owners.append(text)
                pending[text] = existing

        if owners:
            producer = asyncio.create_task(self._produce(owners), name="powercontext-embedding-batch")
            await asyncio.shield(producer)

        vectors_by_text = dict(cached)
        batches: dict[int, _Batch] = {}
        for text, item in pending.items():
            vectors_by_text[text] = await asyncio.shield(item.future)
            if item.batch is not None:
                batches[item.batch.batch_id] = item.batch

        input_tokens = _sum_usage_field(batch.usage.input_tokens for batch in batches.values())
        output_tokens = _sum_usage_field(batch.usage.output_tokens for batch in batches.values())
        requests = sum(batch.usage.requests for batch in batches.values() if batch.usage is not None)
        return EmbeddingResult(
            vectors=tuple(vectors_by_text[text] for text in texts),
            usage=InferenceUsage(
                requests=requests,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self.embed(texts)

    async def _produce(self, owners: list[str]) -> None:
        if self.coalesce_ms:
            await asyncio.sleep(self.coalesce_ms / 1000)

        batches: list[tuple[tuple[str, ...], _Batch]] = []
        async with self._lock:
            # Include texts that arrived during the coalescing window, while
            # leaving batches already handed to the provider untouched.
            active = [text for text, pending in self._pending.items() if pending.batch is None]
            for start in range(0, len(active), self.max_batch_size):
                part = tuple(active[start : start + self.max_batch_size])
                shared = _Batch(self._batch_serial)
                self._batch_serial += 1
                for text in part:
                    pending = self._pending.get(text)
                    if pending is not None:
                        pending.batch = shared
                batches.append((part, shared))

        # A failed request must not stop unrelated batches from completing.
        await asyncio.gather(
            *(self._run_batch(part, shared) for part, shared in batches),
            return_exceptions=True,
        )

    async def _run_batch(self, texts: tuple[str, ...], batch: _Batch) -> None:
        try:
            result = await self.delegate.embed(texts)
            if len(result.vectors) != len(texts):
                raise ValueError("embedding provider returned an invalid vector count")
        except BaseException as error:
            async with self._lock:
                for text in texts:
                    pending = self._pending.pop(text, None)
                    if pending is not None and not pending.future.done():
                        pending.future.set_exception(error)
            return

        async with self._lock:
            batch.usage = result.usage
            for text, vector in zip(texts, result.vectors, strict=True):
                pending = self._pending.pop(text, None)
                if pending is None:
                    continue
                if self.cache_size:
                    self._cache[text] = vector
                    self._cache.move_to_end(text)
                    while len(self._cache) > self.cache_size:
                        self._cache.popitem(last=False)
                pending.future.set_result(vector)


def _consume_future_exception(future: asyncio.Future[tuple[float, ...]]) -> None:
    if not future.cancelled():
        future.exception()


def _sum_usage_field(values: Iterable[int | None]) -> int | None:
    values_list = list(values)
    if any(value is None for value in values_list):
        return None
    return sum(value for value in values_list if value is not None) if values_list else None


__all__ = ["CoalescingEmbeddingModel"]
