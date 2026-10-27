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

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    ConnectorBinding,
    SourceDefinitionRegistry,
    SourceRef,
    project_source_for_transport,
)
from powercontext.sources.connectors import ConnectorLifecycle
from powercontext.sources.pi_session import (
    PI_EVENT_SOURCE_DEFINITION,
    PI_EVENT_SOURCE_NAME,
    PiEventInput,
    PiSessionConnector,
    PiSessionInput,
    parse_pi_session,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

HEADER = {"type": "session", "version": 3, "id": "session-1"}


def parse(records: list[dict], *, leaf: str | None = None):
    raw = "\n".join(json.dumps(record) for record in [HEADER, *records]).encode()
    return parse_pi_session(PiSessionInput(content=raw, leaf_id=leaf))


def test_selected_branch_keeps_control_links_and_skips_control_entries() -> None:
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "ask"}},
        {"type": "model_change", "id": "control", "parentId": "root"},
        {
            "type": "message",
            "id": "leaf",
            "parentId": "control",
            "message": {"role": "assistant", "content": "answer"},
        },
    ]
    events = parse(records, leaf="leaf")
    assert [event.event_id for event in events] == ["root", "leaf"]
    assert events[1].parent_id == "control"
    assert events[1].event_type == "message"
    assert events[1].content == "assistant: answer"


def test_default_leaf_is_the_last_entry() -> None:
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "ask"}},
        {
            "type": "message",
            "id": "leaf",
            "parentId": "root",
            "message": {"role": "assistant", "content": "answer"},
        },
    ]
    assert parse(records) == parse(records, leaf="leaf")


def test_every_visible_event_kind_is_rendered() -> None:
    records = [
        {
            "type": "message",
            "id": "tool",
            "parentId": None,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "toolCall", "name": "read", "id": "call-1", "arguments": {"b": 1, "a": "值"}},
                ],
            },
        },
        {
            "type": "message",
            "id": "result",
            "parentId": "tool",
            "message": {
                "role": "toolResult",
                "toolName": "read",
                "toolCallId": "call-1",
                "content": [{"type": "text", "text": "file contents"}],
            },
        },
        {
            "type": "message",
            "id": "shell",
            "parentId": "result",
            "message": {"role": "bashExecution", "command": "printf result", "output": "result", "content": []},
        },
        {"type": "compaction", "id": "compacted", "parentId": "shell", "summary": "retained summary"},
        {"type": "branch_summary", "id": "branched", "parentId": "compacted", "summary": "branch summary"},
        {
            "type": "custom_message",
            "id": "custom",
            "parentId": "branched",
            "content": [{"type": "text", "text": "Custom evidence"}],
        },
    ]
    events = parse(records)
    assert [event.content for event in events] == [
        'assistant: [tool_call read call-1] {"a":"值","b":1}',
        "toolResult: [tool_result read call-1] file contents",
        "bashExecution: $ printf result\nresult",
        "compaction: retained summary",
        "branch_summary: branch summary",
        "custom: Custom evidence",
    ]
    assert [event.role for event in events] == [
        "assistant",
        "toolResult",
        "bashExecution",
        "compaction",
        "branch_summary",
        "custom",
    ]


def test_events_without_visible_body_are_ignored() -> None:
    records = [
        {
            "type": "message",
            "id": "image",
            "parentId": None,
            "message": {"role": "assistant", "content": [{"type": "image", "data": "ignored"}]},
        },
        {"type": "message", "id": "empty", "parentId": "image", "message": {"role": "assistant", "content": ""}},
    ]
    assert parse(records) == ()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}\n",
        b"[]\n",
        b"null\n",
        b"\xff",
        b'{"type":"session","version":2,"id":"x"}\n',
        b'{"type":"session","version":3,"id":""}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":"missing"}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":"a"}\n',
        b'{"type":"session","version":3,"id":"x"}\n{"type":"message","id":"a","parentId":null}\n'
        b'{"type":"message","id":"a","parentId":null}\n',
    ],
)
def test_invalid_archives_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError):
        parse_pi_session(PiSessionInput(content=raw))


def test_missing_leaf_is_rejected() -> None:
    records = [
        {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "ask"}},
    ]
    with pytest.raises(ValueError):
        parse(records, leaf="absent")


def test_identity_depends_only_on_session_and_event() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        records = [
            {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "first"}},
        ]
        event = parse(records)[0]
        source = await registry.resolve(event)
        assert len(source.name) <= MAX_SOURCE_ID_LENGTH
        assert (await registry.resolve(event)).name == source.name

        records[0]["message"]["content"] = "second"
        changed = parse(records)[0]
        assert changed.content != event.content
        assert (await registry.resolve(changed)).name == source.name

        other = await registry.resolve(PiEventInput(**{**event.model_dump(), "event_id": "other"}))
        assert other.name != source.name

    asyncio.run(scenario())


def test_projection_keeps_event_identity_and_metadata() -> None:
    async def scenario() -> None:
        registry = SourceDefinitionRegistry((PI_EVENT_SOURCE_DEFINITION,))
        records = [
            {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "ask"}},
        ]
        event = parse(records)[0]
        source = await registry.resolve(event)
        observation = project_source_for_transport(registry, source)
        evidence = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
        assert evidence["source_type"] == PI_EVENT_SOURCE_NAME
        assert evidence["source_id"] == source.name
        assert evidence["content"] == event.content
        assert evidence["metadata"] == {
            "session_id": "session-1",
            "event_id": "root",
            "parent_id": None,
            "event_type": "message",
            "role": "user",
        }
        assert await registry.read(source) == event

    asyncio.run(scenario())


class RecordingSink:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, object]] = []

    async def submit(self, binding, item_id, definition_name, value):
        self.items.append((item_id, definition_name, value))
        return SourceRef(source_type=definition_name, source_id=item_id)


class MemoryCheckpoints:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    async def load(self, binding):
        return self.values.get(binding.binding_id)

    async def save(self, binding, checkpoint, *, expected):
        self.values[binding.binding_id] = checkpoint


def write_session(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in [HEADER, *records]) + "\n",
        encoding="utf-8",
    )


def binding() -> ConnectorBinding:
    return ConnectorBinding(
        scope_id="scope", binding_id="pi", connector_name="pi-session-history", connector_version="1"
    )


def test_connector_imports_new_events_only_and_keeps_progress(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = [
            {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared"}},
            {
                "type": "message",
                "id": "left",
                "parentId": "root",
                "message": {"role": "assistant", "content": "left"},
            },
            {"type": "model_change", "id": "control", "parentId": "root"},
            {
                "type": "message",
                "id": "right",
                "parentId": "control",
                "message": {"role": "assistant", "content": "right"},
            },
        ]
        write_session(path, records)
        sink, checkpoints = RecordingSink(), MemoryCheckpoints()
        scope_binding = binding()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)

        first = await lifecycle.run(PiSessionConnector(path, "left"), scope_binding)
        assert [item[0] for item in sink.items] == ["root", "left"]
        assert first.committed_checkpoint is not None

        sink.items.clear()
        second = await lifecycle.run(PiSessionConnector(path, "right"), scope_binding)
        assert [item[0] for item in sink.items] == ["right"]
        assert second.previous_checkpoint == first.committed_checkpoint

        sink.items.clear()
        replay = await lifecycle.run(PiSessionConnector(path, "right"), scope_binding)
        assert sink.items == []
        assert replay.committed_checkpoint == second.committed_checkpoint

        records.append({
            "type": "message",
            "id": "tail",
            "parentId": "right",
            "message": {"role": "assistant", "content": "later"},
        })
        write_session(path, records)
        sink.items.clear()
        await lifecycle.run(PiSessionConnector(path, "tail"), scope_binding)
        assert [item[0] for item in sink.items] == ["tail"]

    asyncio.run(scenario())


def test_connector_keeps_progress_when_the_archive_is_damaged(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = [
            {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared"}},
        ]
        write_session(path, records)
        sink, checkpoints = RecordingSink(), MemoryCheckpoints()
        scope_binding = binding()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
        completed = await lifecycle.run(PiSessionConnector(path), scope_binding)

        path.write_text(path.read_text(encoding="utf-8") + "{bad json}\n", encoding="utf-8")
        with pytest.raises(ValueError):
            await lifecycle.run(PiSessionConnector(path), scope_binding)
        assert checkpoints.values[scope_binding.binding_id] == completed.committed_checkpoint

        write_session(path, records)
        repaired = await lifecycle.run(PiSessionConnector(path), scope_binding)
        assert repaired.previous_checkpoint == completed.committed_checkpoint
        assert repaired.items == ()

    asyncio.run(scenario())


def test_connector_rejects_a_rewritten_imported_event(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "session.jsonl"
        records = [
            {"type": "message", "id": "root", "parentId": None, "message": {"role": "user", "content": "shared"}},
            {
                "type": "message",
                "id": "left",
                "parentId": "root",
                "message": {"role": "assistant", "content": "left"},
            },
        ]
        write_session(path, records)
        sink, checkpoints = RecordingSink(), MemoryCheckpoints()
        scope_binding = binding()
        lifecycle = ConnectorLifecycle(sink=sink, checkpoints=checkpoints)
        await lifecycle.run(PiSessionConnector(path, "left"), scope_binding)

        records[0]["message"]["content"] = "rewritten"
        write_session(path, records)
        with pytest.raises(ValueError):
            await lifecycle.run(PiSessionConnector(path, "left"), scope_binding)

        records[0]["message"]["content"] = "shared"
        write_session(path, records)
        repaired = await lifecycle.run(PiSessionConnector(path, "left"), scope_binding)
        assert repaired.items == ()

    asyncio.run(scenario())
