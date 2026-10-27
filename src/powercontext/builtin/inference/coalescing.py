"""Concurrent embedding coalescing and bounded LRU vector cache."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from powercontext.builtin.inference.models import EmbeddingResult, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel


@dataclass
class _Pending:
    future: asyncio.Future[tuple[tuple[float, ...], ...]]
    request_ids: set[int]
    input_tokens: int | None = None
    output_tokens: int | None = None


class CoalescingEmbeddingModel:
    def __init__(self, delegate: EmbeddingModel, *, max_batch_size: int = 32, coalesce_ms: float = 5, cache_size: int = 256) -> None:
        if type(max_batch_size) is not int or max_batch_size < 1 or cache_size < 0 or coalesce_ms < 0:
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
        results: list[tuple[float, ...] | None] = [None] * len(texts)
        request_count = 0
        input_tokens = None
        output_tokens = None
        misses: list[str] = []
        async with self._lock:
            for index, text in enumerate(texts):
                cached = self._cache.get(text)
                if cached is not None:
                    self._cache.move_to_end(text)
                    results[index] = cached
                elif text not in misses:
                    misses.append(text)
        if misses:
            vectors, request_count, input_tokens, output_tokens = await self._resolve(misses)
            for text, vector in zip(misses, vectors, strict=True):
                for index, candidate in enumerate(texts):
                    if candidate == text:
                        results[index] = vector
        return EmbeddingResult(
            vectors=tuple(vector for vector in results if vector is not None),
            usage=InferenceUsage(
                requests=request_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self.embed(texts)

    async def _resolve(
        self, texts: list[str]
    ) -> tuple[tuple[tuple[float, ...], ...], int, int | None, int | None]:
        owner: list[str] = []
        owner_pending: dict[str, _Pending] = {}
        refs: dict[str, _Pending] = {}
        async with self._lock:
            loop = asyncio.get_running_loop()
            for text in texts:
                pending = self._pending.get(text)
                if pending is None:
                    future: asyncio.Future[tuple[float, ...]] = loop.create_future()
                    self._pending[text] = _Pending(future, set())
                    owner.append(text)
                    owner_pending[text] = self._pending[text]
                refs[text] = self._pending[text]
        if owner:
            task = asyncio.create_task(self._produce(owner), name="powercontext-embedding-batch")
            await asyncio.shield(task)
        values = []
        request_ids: set[int] = set()
        input_tokens = None
        output_tokens = None
        for text in texts:
            pending = refs.get(text)
            if pending is not None:
                values.append(await asyncio.shield(pending.future))
                request_ids.update(pending.request_ids)
                input_tokens = pending.input_tokens
                output_tokens = pending.output_tokens
            else:
                values.append(self._cache[text])
        return tuple(values), len(request_ids), input_tokens, output_tokens

    async def _produce(self, owner: list[str]) -> None:
        try:
            if self.coalesce_ms:
                await asyncio.sleep(self.coalesce_ms / 1000)
            async with self._lock:
                batch = [text for text in owner if text in self._pending]
            for start in range(0, len(batch), self.max_batch_size):
                part = batch[start : start + self.max_batch_size]
                async with self._lock:
                    batch_id = self._batch_serial
                    self._batch_serial += 1
                result = await self.delegate.embed(tuple(part))
                if len(result.vectors) != len(part):
                    raise ValueError("embedding provider returned an invalid vector count")
                async with self._lock:
                    for text, vector in zip(part, result.vectors, strict=True):
                        if self.cache_size:
                            self._cache[text] = vector
                            self._cache.move_to_end(text)
                            while len(self._cache) > self.cache_size:
                                self._cache.popitem(last=False)
                        pending = self._pending.pop(text)
                        pending.request_ids.add(batch_id)
                        pending.input_tokens = result.usage.input_tokens
                        pending.output_tokens = result.usage.output_tokens
                        pending.future.set_result(vector)
        except BaseException as error:
            async with self._lock:
                for text in owner:
                    pending = self._pending.pop(text, None)
                    if pending is not None and not pending.future.done():
                        pending.future.set_exception(error)
            raise


__all__ = ["CoalescingEmbeddingModel"]
