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

"""Capture one branch of a Pi session as immutable, exactly referenceable evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME: Final = "pi-event"
PI_SESSION_CONNECTOR_NAME: Final = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION: Final = "1"

_CHECKPOINT_VERSION: Final = "1"
_IDENTITY_DIGEST_LENGTH: Final = 32
_IDENTITY_VISIBLE_LENGTH: Final = (MAX_SOURCE_ID_LENGTH - _IDENTITY_DIGEST_LENGTH - 2) // 2
_SESSION_VERSION: Final = 3
_SUMMARY_ENTRY_TYPES: Final = frozenset({"compaction", "branch_summary"})


class PiSessionInput(BaseModel):
    """One raw Pi session archive together with the branch leaf to capture."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible event taken from the selected branch of a Pi session."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _SessionEntry:
    """One Pi session entry and its place in the session tree."""

    identifier: str
    parent_id: str | None
    record: dict[str, JsonValue]


def parse_pi_session(value: PiSessionInput, /) -> tuple[PiEventInput, ...]:
    """Return the visible events of the selected branch, ordered from root to leaf.

    The returned tuple keeps the complete input record of every event, so that a
    later reader can quote the original evidence instead of a compacted view.
    """

    session_id, _, chain = _read_session(value.content, value.leaf_id)
    return _visible_events(session_id, chain)


class PiEventSource(Source):
    """A captured Pi event retained as durable, exactly referenceable evidence."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventAdapter:
    """Resolve and read the captured Pi event Source."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        return PiEventSource(
            name=_source_identity(value.session_id, value.event_id),
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one captured Pi event as standard textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PiEventAdapter(),
    version="1",
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import the visible events of one Pi session branch into a Scope.

    The connector parses the whole archive before it submits anything, so a
    damaged or rewritten history never advances the persisted progress.
    """

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        session_id, entries, chain = _read_session(self._path.read_bytes(), self._leaf_id)
        digests = {entry.identifier: _record_digest(entry.record) for entry in entries}
        checkpoint_session, imported = _decode_progress(session.checkpoint)
        if checkpoint_session is not None and checkpoint_session != session_id:
            imported = {}
        _reject_rewritten_history(imported, digests)
        for event in _visible_events(session_id, chain):
            if event.event_id in imported:
                continue
            outcome = await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                imported[event.event_id] = _record_digest(event.record)
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=_encode_progress(session_id, imported),
        )


def _read_session(
    content: bytes,
    leaf_id: str | None,
) -> tuple[str, tuple[_SessionEntry, ...], tuple[_SessionEntry, ...]]:
    """Return the session id, every entry, and the chain of the selected branch."""

    lines = _session_lines(content)
    session_id = _read_header(lines[0])
    entries = _read_entries(lines[1:])
    if not entries:
        raise ValueError("Pi session content must contain at least one entry")  # noqa: TRY003
    selected = leaf_id if leaf_id is not None else entries[-1].identifier
    return session_id, entries, _branch_chain(entries, selected)


def _session_lines(content: bytes) -> list[str]:
    """Return the non-empty lines of one archive, rejecting an invalid encoding."""

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be UTF-8 encoded") from error  # noqa: TRY003
    # JSONL uses LF (optionally preceded by CR), not Unicode text separators.
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("Pi session content must start with a session header")  # noqa: TRY003
    return lines


def _read_header(line: str) -> str:
    """Return the session id declared by the archive header."""

    header = _decode_record(line)
    if header.get("type") != "session" or header.get("version") != _SESSION_VERSION:
        raise ValueError("Pi session header must declare type=session and version=3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Pi session header must declare a non-empty id")  # noqa: TRY003
    return session_id


def _read_entries(lines: list[str]) -> tuple[_SessionEntry, ...]:
    """Return every archive entry, rejecting an inconsistent session tree."""

    entries: list[_SessionEntry] = []
    seen: set[str] = set()
    for line in lines:
        record = _decode_record(line)
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Pi session entries must declare a non-empty id")  # noqa: TRY003
        if identifier in seen:
            raise ValueError(f"Pi session entry {identifier!r} is declared more than once")  # noqa: TRY003
        if "parentId" not in record:
            raise ValueError(f"Pi session entry {identifier!r} must declare parentId")  # noqa: TRY003
        parent_id = record["parentId"]
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id.strip()):
            raise ValueError(f"Pi session entry {identifier!r} must declare a parent id or null")  # noqa: TRY003
        if parent_id is not None and parent_id not in seen:
            raise ValueError(f"Pi session entry {identifier!r} references an unknown parent")  # noqa: TRY003
        seen.add(identifier)
        entries.append(_SessionEntry(identifier=identifier, parent_id=parent_id, record=record))
    return tuple(entries)


def _decode_record(line: str) -> dict[str, JsonValue]:
    try:
        decoded = json.loads(line, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as error:
        raise ValueError("Pi session content must contain valid JSON records") from error  # noqa: TRY003
    if not isinstance(decoded, dict):
        raise ValueError("Pi session records must be JSON objects")  # noqa: TRY003, TRY004
    return decoded


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Pi session contains a non-JSON number: {value}")  # noqa: TRY003


def _branch_chain(entries: Sequence[_SessionEntry], leaf_id: str) -> tuple[_SessionEntry, ...]:
    """Return the ancestor chain of one leaf, ordered from the root to the leaf."""

    by_id = {entry.identifier: entry for entry in entries}
    if leaf_id not in by_id:
        raise ValueError(f"Pi session does not contain the leaf {leaf_id!r}")  # noqa: TRY003
    chain: list[_SessionEntry] = []
    visited: set[str] = set()
    current: _SessionEntry | None = by_id[leaf_id]
    while current is not None:
        if current.identifier in visited:
            raise ValueError(f"Pi session entry {current.identifier!r} takes part in a cycle")  # noqa: TRY003
        visited.add(current.identifier)
        chain.append(current)
        current = by_id.get(current.parent_id) if current.parent_id is not None else None
    return tuple(reversed(chain))


def _visible_events(session_id: str, chain: Sequence[_SessionEntry]) -> tuple[PiEventInput, ...]:
    events: list[PiEventInput] = []
    for entry in chain:
        event = _visible_event(session_id, entry)
        if event is not None:
            events.append(event)
    return tuple(events)


def _visible_event(session_id: str, entry: _SessionEntry) -> PiEventInput | None:
    """Project one session entry into visible evidence, or ignore it when it carries no body."""

    record = entry.record
    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None
    if event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if not isinstance(role, str) or not role:
            return None
        body = _message_body(message)
    elif event_type in _SUMMARY_ENTRY_TYPES:
        role = event_type
        body = _text_field(record.get("summary"))
    elif event_type == "custom_message":
        role = "custom"
        body = _visible_text(record.get("content"))
    else:
        return None
    if not body.strip():
        return None
    return PiEventInput(
        session_id=session_id,
        event_id=entry.identifier,
        parent_id=entry.parent_id,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _message_body(message: dict[str, JsonValue]) -> str:
    """Render the visible body of one Pi message."""

    role = message.get("role")
    content = message.get("content")
    if role == "toolResult":
        text = _visible_text(content)
        name = _text_field(message.get("toolName"))
        call_id = _text_field(message.get("toolCallId"))
        return f"[tool_result {name} {call_id}] {text}"
    if role == "bashExecution":
        command = _text_field(message.get("command"))
        output = _text_field(message.get("output"))
        return f"$ {command}\n{output}"
    return _visible_blocks(content)


def _visible_blocks(content: object) -> str:
    """Join the visible blocks of one message with line breaks."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif kind == "toolCall":
            parts.append(_tool_call_body(block))
    return "\n".join(parts)


def _visible_text(content: object) -> str:
    """Return the visible text of one string or text-block payload."""

    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _tool_call_body(block: dict[str, JsonValue]) -> str:
    """Render one tool call with its compact, key-sorted arguments."""

    name = _text_field(block.get("name"))
    call_id = _text_field(block.get("id"))
    arguments = block.get("arguments")
    encoded = json.dumps(
        arguments if arguments is not None else {},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"[tool_call {name} {call_id}] {encoded}"


def _text_field(value: object) -> str:
    return value if isinstance(value, str) else ""


def _record_digest(record: dict[str, JsonValue]) -> str:
    """Return a stable digest of one complete session record."""

    encoded = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_identity(session_id: str, event_id: str) -> str:
    """Return one stable identity that only depends on the session and the event."""

    # Preserve existing identities while making embedded delimiters unambiguous.
    identity = f"{session_id}\x00{event_id}"
    if "\x00" in session_id or "\x00" in event_id:
        identity = json.dumps([session_id, event_id], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode()).hexdigest()[:_IDENTITY_DIGEST_LENGTH]
    return f"{session_id[:_IDENTITY_VISIBLE_LENGTH]}:{event_id[:_IDENTITY_VISIBLE_LENGTH]}:{digest}"


def _decode_progress(checkpoint: JsonValue | None) -> tuple[str | None, dict[str, str]]:
    """Read the persisted collection progress of one Pi session binding."""

    if checkpoint is None:
        return None, {}
    if not isinstance(checkpoint, dict):
        raise TypeError("Pi session checkpoint must be a JSON object")  # noqa: TRY003
    session_id = checkpoint.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        raise TypeError("Pi session checkpoint must record a string session id")  # noqa: TRY003
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        raise ValueError("Pi session checkpoint must record the imported events")  # noqa: TRY003, TRY004
    imported: dict[str, str] = {}
    for event_id, entry in events.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("digest"), str):
            raise ValueError("Pi session checkpoint must record one digest per imported event")  # noqa: TRY003, TRY004
        imported[event_id] = entry["digest"]
    return session_id, imported


def _encode_progress(session_id: str, imported: dict[str, str]) -> dict[str, JsonValue]:
    """Encode the collection progress of one Pi session binding."""

    return {
        "version": _CHECKPOINT_VERSION,
        "session_id": session_id,
        "events": {event_id: {"digest": digest} for event_id, digest in sorted(imported.items())},
    }


def _reject_rewritten_history(imported: dict[str, str], digests: dict[str, str]) -> None:
    """Reject an archive whose already imported events were rewritten."""

    for event_id, digest in imported.items():
        current = digests.get(event_id)
        if current is not None and current != digest:
            raise ValueError(f"Pi session event {event_id!r} was rewritten after it was imported")  # noqa: TRY003


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventAdapter",
    "PiEventInput",
    "PiEventSource",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
]
