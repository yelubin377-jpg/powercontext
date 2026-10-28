"""Persistent Source dependency lifecycle registry."""
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, insert, select
from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.sources import SourceRef

class SourceLifecycleError(ValueError):
    pass

class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str = Field(min_length=1)
    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip(): raise ValueError("value must not be blank")
        return value

@dataclass
class _Node:
    reference: SourceRef | MemoryCitation | ArtifactRef
    parents: tuple[str, ...] = ()
    policy: str = "all"
    family: str = "memory"
    revision: int = 1
    available: bool = True

def _dump(value: object) -> bytes:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()

class ScopedSourceLifecycle:
    def __init__(self, runtime: Any, scope_id: str) -> None:
        self.runtime, self.scope_id = runtime, scope_id
        self.nodes: dict[str, _Node] = {}
        self.events: dict[str, SourceLifecycleEvent] = {}
        self._loaded = False

    @property
    def _database(self):
        return self.runtime._provider.database

    async def _ensure_loaded(self) -> None:
        if self._loaded: return
        async with self._database.transaction() as connection:
            rows = (await connection.execute(select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id))).mappings().all()
        for row in rows:
            payload = json.loads(bytes(row["reference"]).decode())
            family = str(row["family"])
            ref = SourceRef.model_validate(payload) if family == "source" else MemoryCitation.model_validate(payload) if family == "memory" else ArtifactRef.model_validate(payload)
            node_id = str(row["node_id"])
            self.nodes[node_id] = _Node(ref, tuple(json.loads(bytes(row["parents"]).decode())), str(row["policy"]), family, int(row["revision"]), bool(row["available"]))
            if row["sequence"] is not None:
                self.events[node_id] = SourceLifecycleEvent(node_id=node_id, revision=int(row["event_revision"]), sequence=int(row["sequence"]), available=bool(row["event_available"]), reason=str(row["event_reason"]))
        self._loaded = True

    async def _persist(self, node_ids: tuple[str, ...] | None = None) -> None:
        ids = tuple(self.nodes) if node_ids is None else node_ids
        async with self._database.transaction() as connection:
            for node_id in ids:
                node, event = self.nodes[node_id], self.events.get(node_id)
                await connection.execute(delete(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == self.scope_id, SOURCE_LIFECYCLE_TABLE.c.node_id == node_id))
                await connection.execute(insert(SOURCE_LIFECYCLE_TABLE).values(scope_id=self.scope_id, node_id=node_id, family=node.family, reference=_dump(node.reference), parents=json.dumps(node.parents).encode(), policy=node.policy, available=node.available, revision=node.revision, sequence=None if event is None else event.sequence, event_available=None if event is None else event.available, event_revision=None if event is None else event.revision, event_reason=None if event is None else event.reason))

    async def register_source(self, node_id: str, source_ref: SourceRef, *, revision: int) -> SourceRef:
        await self._ensure_loaded()
        if revision <= 0: raise SourceLifecycleError("revision must be positive")
        try:
            await self.runtime.records.for_scope(self.scope_id).get_source(source_ref.source_type, source_ref.source_id)
        except Exception as error: raise SourceLifecycleError("unknown source reference") from error
        result = self._register(node_id, source_ref, (), "all", "source", revision)
        await self._persist((node_id,)); return result

    async def register_memory(self, node_id: str, citation: MemoryCitation, *, parents: tuple[str, ...], policy: str = "all") -> MemoryCitation:
        await self._ensure_loaded()
        try: await self.runtime.memory.for_scope(self.scope_id).get(type("Request", (), {"citation": citation})())
        except Exception as error: raise SourceLifecycleError("unknown memory citation") from error
        result = self._register(node_id, citation, parents, policy, "memory", 1)
        await self._sync_memory_states({node_id: not self.nodes[node_id].available})
        await self._persist((node_id,))
        return result

    async def register_topic(self, node_id: str, artifact_ref: ArtifactRef, *, parents: tuple[str, ...], policy: str = "all") -> ArtifactRef:
        await self._ensure_loaded()
        try: await self.runtime.records.for_scope(self.scope_id).get_artifact_revision(artifact_ref.family, artifact_ref.artifact_id, artifact_ref.revision)
        except Exception as error: raise SourceLifecycleError("unknown topic artifact") from error
        result = self._register(node_id, artifact_ref, parents, policy, "topic", 1)
        await self._persist((node_id,)); return result

    def _register(self, node_id, reference, parents, policy, family, revision):
        if not node_id or node_id != node_id.strip(): raise SourceLifecycleError("invalid node id")
        parents = tuple(parents)
        if family != "source":
            if not parents or len(set(parents)) != len(parents) or any(parent not in self.nodes for parent in parents): raise SourceLifecycleError("unknown or duplicate parent")
            if node_id in parents or self._would_cycle(node_id, parents): raise SourceLifecycleError("cyclic dependency")
            if policy not in {"all", "any"}: raise SourceLifecycleError("invalid policy")
        if node_id in self.nodes:
            old = self.nodes[node_id]
            if old.reference != reference or old.parents != parents or old.policy != policy or old.revision != revision: raise SourceLifecycleError("conflicting registration")
            return reference
        node = _Node(reference, parents, policy, family, revision, self._support(parents, policy) if parents else True)
        self.nodes[node_id] = node
        return reference

    def _would_cycle(self, node_id, parents):
        seen, stack = set(), list(parents)
        while stack:
            current = stack.pop()
            if current == node_id: return True
            if current in seen: continue
            seen.add(current); stack.extend(self.nodes[current].parents)
        return False

    def _support(self, parents, policy):
        values = [self.nodes[parent].available for parent in parents]
        return all(values) if policy == "all" else any(values)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        await self._ensure_loaded()
        node = self.nodes.get(event.node_id)
        if node is None or node.family != "source": raise SourceLifecycleError("unknown source")
        if event.revision != node.revision: raise SourceLifecycleError("source revision mismatch")
        old = self.events.get(event.node_id)
        if old == event: return sorted(node_id for node_id, item in self.nodes.items() if item.available)
        if old is not None and event.sequence <= old.sequence: raise SourceLifecycleError("stale event")
        before = {node_id: item.available for node_id, item in self.nodes.items()}
        previous_event = old
        node.available = event.available
        changed = True
        while changed:
            changed = False
            for item in self.nodes.values():
                if item.parents:
                    value = self._support(item.parents, item.policy)
                    if item.available != value: item.available, changed = value, True
        try:
            await self._sync_memory_states(before)
            self.events[event.node_id] = event
            await self._persist(tuple(self.nodes))
        except Exception:
            for node_id, value in before.items(): self.nodes[node_id].available = value
            if previous_event is None:
                self.events.pop(event.node_id, None)
            else:
                self.events[event.node_id] = previous_event
            raise
        return sorted(node_id for node_id, item in self.nodes.items() if item.available)

    async def _sync_memory_states(self, before):
        changed = [node_id for node_id, node in self.nodes.items() if node.family == "memory" and before.get(node_id, not node.available) != node.available]
        if not changed: return
        async with self.runtime._context(self.scope_id) as context, self.runtime._locked(self.scope_id):
            service = context.artifacts.memory
            current = await service.head(context.artifacts.memory_artifact_id)
            if current is None: return
            for node_id in changed:
                citation = self.nodes[node_id].reference
                if not isinstance(citation, MemoryCitation): continue
                entry = next((item for item in await service.entries(current) if item.entry_id == citation.entry_id), None)
                if entry is None: continue
                if self.nodes[node_id].available and entry.state == "inactive": current = await service.reactivate(current, entries=(entry,), reason="source restored")
                elif not self.nodes[node_id].available and entry.state == "active": current = await service.forget(current, entries=(entry,), reason="source withdrawn")

    async def ensure_loaded(self): await self._ensure_loaded()

    async def resolve(self, node_id, *, purpose="generation"):
        await self._ensure_loaded()
        node = self.nodes.get(node_id)
        if node is None: raise SourceLifecycleError("unknown node")
        if purpose not in {"generation", "audit"}: raise SourceLifecycleError("unknown purpose")
        if purpose == "generation" and not node.available: raise SourceLifecycleError("inactive evidence")
        return node.reference

class SourceLifecycleApplication:
    def __init__(self, runtime): self._runtime, self._scopes = runtime, {}
    def for_scope(self, scope_id): return self._scopes.setdefault(scope_id, ScopedSourceLifecycle(self._runtime, scope_id))
    async def allows_memory(self, scope_id, memory_ref, entry_id):
        service = self.for_scope(scope_id); await service.ensure_loaded()
        matches = [node.available for node in service.nodes.values() if node.family == "memory" and isinstance(node.reference, MemoryCitation) and node.reference.memory_ref == memory_ref and node.reference.entry_id == entry_id]
        return all(matches) if matches else True
    async def allows_topic(self, scope_id, reference):
        service = self.for_scope(scope_id); await service.ensure_loaded()
        matches = [node.available for node in service.nodes.values() if node.family == "topic" and node.reference == reference]
        return all(matches) if matches else True
__all__ = ["SourceLifecycleError", "SourceLifecycleEvent", "SourceLifecycleApplication", "ScopedSourceLifecycle"]
