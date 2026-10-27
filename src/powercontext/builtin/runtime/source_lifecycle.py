"""Source dependency lifecycle registry."""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

class SourceLifecycleError(ValueError): pass

class SourceLifecycleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    sequence: int = Field(gt=0)
    available: bool
    reason: str = Field(min_length=1)
    @field_validator("node_id", "reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError("value must not be blank")
        return value

class _Node:
    def __init__(self, reference, parents=(), policy="all", family="memory"):
        self.reference, self.parents, self.policy, self.family = reference, tuple(parents), policy, family
        self.available = True

class ScopedSourceLifecycle:
    def __init__(self, runtime, scope_id): self.runtime, self.scope_id, self.nodes, self.events = runtime, scope_id, {}, {}
    async def register_source(self, node_id, source_ref, *, revision): return self._register(node_id, source_ref, (), "all", "source")
    async def register_memory(self, node_id, citation, *, parents, policy="all"): return self._register(node_id, citation, parents, policy, "memory")
    async def register_topic(self, node_id, artifact_ref, *, parents, policy="all"): return self._register(node_id, artifact_ref, parents, policy, "topic")
    def _register(self, node_id, ref, parents, policy, family):
        parents=tuple(parents)
        if node_id in self.nodes:
            old=self.nodes[node_id]
            if old.reference != ref or old.parents != parents or old.policy != policy: raise SourceLifecycleError("conflicting registration")
            return ref
        if family != "source" and (not parents or any(parent not in self.nodes for parent in parents)): raise SourceLifecycleError("unknown parent")
        if policy not in {"all", "any"}: raise SourceLifecycleError("invalid policy")
        node=_Node(ref, parents, policy, family); node.available=self._support(node.parents, policy); self.nodes[node_id]=node; return ref
    def _support(self, parents, policy):
        values=[self.nodes[parent].available for parent in parents]
        return all(values) if policy == "all" else any(values)
    async def apply(self, event):
        if event.node_id not in self.nodes: raise SourceLifecycleError("unknown node")
        old=self.events.get(event.node_id)
        if old and (event.sequence <= old.sequence or event.revision < old.revision): raise SourceLifecycleError("stale event")
        self.events[event.node_id]=event; self.nodes[event.node_id].available=event.available
        changed=True
        while changed:
            changed=False
            for node in self.nodes.values():
                if node.parents:
                    value=self._support(node.parents,node.policy)
                    if node.available != value: node.available=value; changed=True
        return sorted(node_id for node,node in self.nodes.items() if node.available)
    async def resolve(self, node_id, *, purpose="generation"):
        node=self.nodes.get(node_id)
        if node is None: raise SourceLifecycleError("unknown node")
        if purpose == "generation" and not node.available: raise SourceLifecycleError("inactive evidence")
        return node.reference

class SourceLifecycleApplication:
    def __init__(self, runtime): self._scopes={}
    def for_scope(self, scope_id): return self._scopes.setdefault(scope_id, ScopedSourceLifecycle(self, scope_id))

__all__=["SourceLifecycleError","SourceLifecycleEvent","SourceLifecycleApplication","ScopedSourceLifecycle"]
