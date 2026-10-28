"""PDF identity, parsing, projection, and retrieval edge cases beyond the public sample."""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from pypdf import PdfWriter

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    pdf_page_source_id,
    search_pdf_pages,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

DIGEST = "a" * 64


def _pdf(pages: int = 1, encrypt: bool = False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    if encrypt:
        writer.encrypt(user_password="", owner_password="owner")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not a pdf at all",
        b"%PDF-1.7\nbroken",
    ],
)
def test_unusable_files_raise_value_error(content):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="doc", content=content))


@pytest.mark.parametrize("document_id", ["", " ", "\t", "\n", "   ", " \t\n "])
def test_blank_document_identity_is_rejected(document_id):
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id=document_id, content=_pdf()))


def test_physical_pages_are_numbered_from_one_and_include_blank_pages():
    content = _pdf(pages=3)
    pages = parse_pdf_document(PDFDocumentInput(document_id="doc", content=content))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert all(page.text == "" for page in pages)
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(content).hexdigest()}


def test_empty_password_permission_marker_is_accepted():
    pages = parse_pdf_document(PDFDocumentInput(document_id="doc", content=_pdf(pages=2, encrypt=True)))
    assert [page.page_number for page in pages] == [1, 2]


def test_identity_is_stable_bounded_and_revision_sensitive():
    first = pdf_page_source_id("doc", DIGEST, 3)
    assert first == pdf_page_source_id("doc", DIGEST, 3)
    assert first != pdf_page_source_id("doc", DIGEST, 4)
    assert first != pdf_page_source_id("other", DIGEST, 3)
    assert first != pdf_page_source_id("doc", "b" * 64, 3)
    assert len(first) <= 256
    assert len(pdf_page_source_id("中" * 120, DIGEST, 1)) <= 256


def test_projection_carries_exactly_the_contract_fields():
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    page = PDFPageInput(document_id="doc", document_sha256=DIGEST, page_number=7, text="body text")
    source = asyncio.run(registry.resolve(page))
    observation = project_source_for_transport(registry, source)
    projection = observation.projection(TEXT_EVIDENCE_PROJECTION_KEY)
    assert projection["source_type"] == "pdf-page"
    assert projection["source_id"] == source.name
    assert projection["content"] == "doc\nbody text"
    assert projection["metadata"] == {
        "document_id": "doc",
        "document_sha256": DIGEST,
        "page_number": 7,
    }


def test_read_returns_the_page_input_and_definition_is_stable():
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    page = PDFPageInput(document_id="doc", document_sha256=DIGEST, page_number=1, text="hello")
    source = asyncio.run(registry.resolve(page))
    assert asyncio.run(registry.read(source)) == page
    assert PDF_PAGE_SOURCE_DEFINITION.name == "pdf-page"
    assert PDF_PAGE_SOURCE_DEFINITION.version == "1"
    assert PDF_PAGE_SOURCE_DEFINITION.input_class is PDFPageInput


class _EvidencePipeline:
    async def extract(self, request, /):
        return tuple(
            MemoryEntryInput(
                kind="source-evidence",
                text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                sources=(source,),
            )
            for source in request.sources
            if source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"].strip()
        )


def test_search_returns_distinct_page_citations(tmp_path):
    asyncio.run(_search_returns_distinct_page_citations(tmp_path))


async def _search_returns_distinct_page_citations(tmp_path):
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    observations = []
    for number in range(1, 6):
        page = PDFPageInput(
            document_id="doc",
            document_sha256=DIGEST,
            page_number=number,
            text=f"page {number} revenue growth",
        )
        source = await registry.resolve(page)
        observations.append(project_source_for_transport(registry, source))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pages.db'}"))
    async with open_builtin_runtime(config) as runtime:
        scope = await runtime.scopes.create(ScopeDraft(title="Pages", summary="Evidence", idempotency_key="pages"))
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        for observation in observations:
            await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope.scope_id, observation=observation))
    async with open_builtin_runtime(config, candidate_pipeline=_EvidencePipeline()) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        for _ in range(len(observations) + 1):
            if not (await memory.flush()).processed:
                break
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(scope.scope_id)
        citations = await search_pdf_pages(memory, "revenue growth", limit=3)
        assert isinstance(citations, tuple)
        assert 0 < len(citations) <= 3
        sources = []
        for citation in citations:
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
            assert len(entry.sources) == 1
            assert entry.sources[0].source_type == "pdf-page"
            sources.append(entry.sources[0].source_id)
        assert len(set(sources)) == len(sources)
        assert await search_pdf_pages(memory, "revenue growth", limit=0) == ()
