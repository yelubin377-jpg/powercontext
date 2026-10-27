"""Deterministic, self-validating context packs and prefix deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any


_PACK_SCHEMA = "powercontext.context-pack.v1"
_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PREFIX = "sha256:"
_PACK_FIELDS = frozenset(("schema", "items", "digest", "byte_count"))
_DELTA_FIELDS = frozenset(("schema", "base_digest", "target_digest", "prefix_items", "appended"))


class ContextPackError(ValueError):
    """Raised when a context pack or delta violates the wire contract."""


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": _PACK_SCHEMA, "items": [_encode(item) for item in items]}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _encode(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise ContextPackError("encoded chunk must be a string")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ContextPackError("invalid base64 chunk") from error


def _load_json(data: bytes) -> object:
    try:
        return json.loads(data.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ContextPackError("wire payload must be valid UTF-8 JSON") from error


def _check_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != len(_DIGEST_PREFIX) + 64 or not value.startswith(_DIGEST_PREFIX):
        raise ContextPackError(f"{field} must be sha256:<hex>")
    try:
        int(value[len(_DIGEST_PREFIX) :], 16)
    except ValueError as error:
        raise ContextPackError(f"{field} must be sha256:<hex>") from error
    if value != value.lower():
        raise ContextPackError(f"{field} must use lowercase hexadecimal")
    return value


def _check_items(value: object, field: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{field} must be an array")
    try:
        return tuple(_decode(item) for item in value)
    except ContextPackError:
        raise


def _wire_byte_count(unsigned: dict[str, Any], field: str, value: int | None = None) -> int:
    """Return the fixed-point JSON length when ``byte_count`` is part of the wire."""

    count = 0 if value is None else value
    for _ in range(10):
        payload = {**unsigned, field: count}
        new_count = len(_canonical(payload))
        if new_count == count:
            return count
        count = new_count
    raise ContextPackError("could not determine wire byte_count")


@dataclass(frozen=True, slots=True)
class ContextPack:
    """A complete sequence of byte chunks and its wire-level integrity data."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...] | list[bytes]) -> ContextPack:
        try:
            normalized = tuple(bytes(item) for item in items)
        except (TypeError, ValueError) as error:
            raise ContextPackError("items must contain bytes") from error
        digest = _digest(normalized)
        unsigned = {"schema": _PACK_SCHEMA, "items": [_encode(item) for item in normalized], "digest": digest}
        return cls(normalized, digest, _wire_byte_count(unsigned, "byte_count"))

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContextPackError("content must be valid UTF-8") from error
        elif isinstance(content, str):
            text = content
        else:
            raise ContextPackError("content must be str or bytes")
        chunks: list[bytes] = []
        current = bytearray()
        for character in text:
            encoded = character.encode("utf-8")
            if len(encoded) > chunk_bytes:
                raise ContextPackError("chunk_bytes is too small for a UTF-8 character")
            if current and len(current) + len(encoded) > chunk_bytes:
                chunks.append(bytes(current))
                current.clear()
            current.extend(encoded)
        if current:
            chunks.append(bytes(current))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        payload = {
            "schema": _PACK_SCHEMA,
            "items": [_encode(item) for item in self.items],
            "digest": self.digest,
            "byte_count": self.byte_count,
        }
        encoded = _canonical(payload)
        if len(encoded) != self.byte_count:
            raise ContextPackError("pack byte_count does not match wire length")
        return encoded

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _load_json(data)
        if not isinstance(payload, dict) or set(payload) != _PACK_FIELDS:
            raise ContextPackError("pack fields do not match the protocol")
        if payload["schema"] != _PACK_SCHEMA:
            raise ContextPackError("unsupported pack schema")
        items = _check_items(payload["items"], "items")
        digest = _check_digest(payload["digest"], "digest")
        if digest != _digest(items):
            raise ContextPackError("pack digest does not match items")
        count = payload["byte_count"]
        if type(count) is not int or count < 0 or count != len(data):
            raise ContextPackError("pack byte_count does not match wire length")
        return cls(items, digest, count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A prefix-preserving patch from one complete pack to another."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        _check_digest(self.base_digest, "base_digest")
        _check_digest(self.target_digest, "target_digest")
        payload = {
            "schema": _DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [_encode(item) for item in self.appended],
        }
        return _canonical(payload)

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _load_json(data)
        if not isinstance(payload, dict) or set(payload) != _DELTA_FIELDS:
            raise ContextPackError("delta fields do not match the protocol")
        if payload["schema"] != _DELTA_SCHEMA:
            raise ContextPackError("unsupported delta schema")
        base_digest = _check_digest(payload["base_digest"], "base_digest")
        target_digest = _check_digest(payload["target_digest"], "target_digest")
        prefix = payload["prefix_items"]
        if type(prefix) is not int or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        appended = _check_items(payload["appended"], "appended")
        return cls(base_digest, target_digest, prefix, appended)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate a delta against a base pack and reconstruct its target."""

    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match base pack")
    if type(delta.prefix_items) is not int or delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix_items is outside the base pack")
    target_items = base.items[: delta.prefix_items] + tuple(delta.appended)
    target = ContextPack.build(target_items)
    if target.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match reconstructed items")
    return target


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
