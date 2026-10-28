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

"""Complete PDF page parsing with page-level sources that stay re-checkable."""

from __future__ import annotations

import hashlib
import io
import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from pydantic import BaseModel, model_validator
from pypdf import PdfReader

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.definitions import AdapterSourceDefinition, SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

if TYPE_CHECKING:
    from powercontext.artifacts.models import MemoryCitation
    from powercontext.builtin.artifacts.memory.models import MemoryHit
    from powercontext.builtin.runtime import ScopedMemoryApplication

PDF_PAGE_SOURCE_TYPE = "pdf-page"

# Retrieval is a three stage affair: find the document the question is about,
# pull that document's pages back through the Memory index, then rerank them.
_DISCOVERY_LIMIT = 60
_ENTITY_LIMIT = 20
_DOCUMENT_LIMIT = 512
_DOCUMENT_LIMIT_MAXIMUM = 4096
_HEADING_CHARACTERS = 120
_HEADING_WEIGHT = 2.0
_RELEVANCE_SEEDS = 10
_RELEVANCE_EXPANSION = 40
_RELEVANCE_EXPANSION_WEIGHT = 0.5
_STATEMENT_PRIOR = 0.5
_FAMILY_PRIOR = 2.0

# A financial report states its balance sheet, its statement of operations and
# its statement of cash flows once each, and then discusses them. These are the
# headings that mark those pages, and the question words that point at each of
# them.
_STATEMENT_PATTERNS = (
    ("balance", re.compile(r"balance\s+sheets?", re.IGNORECASE)),
    ("cash", re.compile(r"statements?\s+of\s+cash\s+flows|cash\s+flows?\s+statement", re.IGNORECASE)),
    ("operations", re.compile(r"statements?\s+of\s+(operations|income)|income\s+statements?", re.IGNORECASE)),
    ("notes", re.compile(r"notes?\s+to\s+(the\s+)?(consolidated\s+)?(financial\s+statements|condensed)", re.IGNORECASE)),
    ("statement", re.compile(r"consolidated\s+statements?\s+of", re.IGNORECASE)),
)
_STATEMENT_FAMILIES = ("balance", "cash", "operations", "notes", "statement")
_FAMILY_VOCABULARY = {
    "balance": frozenset(
        """asset assets liability liabilities equity receivable receivables payable payables inventory inventories
        current liquidity quick ratio working capital debt leverage solvency book""".split()
    ),
    "operations": frozenset(
        """revenue sales margin gross operating income ebitda ebit cost cogs expense expenses tax taxes profit
        earnings segment pricing discount""".split()
    ),
    "cash": frozenset(
        """cash flow capex expenditure expenditures depreciation amortization dividend buyback investing financing
        activities""".split()
    ),
}

# Capitalised words that open a question instead of naming the filer.
_QUESTION_WORDS = frozenset(
    ["a", "an", "the", "and", "or", "but", "for", "from", "with", "without", "within", "between", "among", "what", "which", "who", "whom", "whose", "how", "why", "when", "where", "does", "did", "do", "is", "are", "was", "were", "has", "have", "had", "can", "could", "should", "would", "if", "then", "than", "as", "by", "in", "on", "at", "to", "of", "please", "give", "respond", "state", "explain", "calculate", "define", "using", "use", "used", "shown", "round", "answer", "based", "assume", "given", "provide", "consider", "only", "there", "their", "its", "our", "your", "we", "you", "they", "them", "he", "she", "his", "her", "not", "no", "any", "all", "each", "every", "other", "please", "assume", "definition", "view", "perspective", "means", "meaning", "include", "including", "per", "unit", "units"]
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9&.\-]*")


class PDFDocumentInput(BaseModel):
    """One complete original PDF file and the identifier callers use for it."""

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page with its document identity and extractable body text."""

    document_id: str
    document_sha256: str
    page_number: int
    text: str


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Encode one page identity from the document, its digest, and the physical page."""

    identity = f"{PDF_PAGE_SOURCE_TYPE}:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) <= MAX_SOURCE_ID_LENGTH:
        return identity
    digest = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:32]
    return f"{PDF_PAGE_SOURCE_TYPE}:{digest}:{document_sha256}:{page_number}"


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Parse every physical page of one original PDF, failing atomically.

    Physical page numbers start at 1 and cover blank pages as well. A file that is
    empty, not a PDF, damaged, or protected by a real password raises ``ValueError``
    instead of returning the pages that happened to parse.
    """

    document_id = value.document_id
    if not document_id or not document_id.strip():
        raise ValueError("document_id must not be empty or whitespace")
    content = value.content
    if not content:
        raise ValueError("PDF content must not be empty")
    digest = hashlib.sha256(content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            # An empty user password is a permission marker, not a secret: such a
            # file is readable without caller-supplied credentials.
            if reader.decrypt("") == 0:
                raise ValueError("PDF requires a password")
        pages = tuple(
            PDFPageInput(
                document_id=document_id,
                document_sha256=digest,
                page_number=number,
                text=page.extract_text() or "",
            )
            for number, page in enumerate(reader.pages, start=1)
        )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Invalid PDF document: {error}") from error
    return pages



class PDFPageSource(Source):
    """A captured PDF page that can be read back without the original file."""

    materialization: Literal[SourceMaterialization.CAPTURED] = SourceMaterialization.CAPTURED
    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str

    @model_validator(mode="after")
    def validate_identity(self) -> PDFPageSource:
        identity = pdf_page_source_id(self.document_id, self.document_sha256, self.page_number)
        if self.name != identity or self.source_id != identity:
            raise ValueError("PDF page name and source_id must match the document/page identity")
        return self


class _PDFPageAdapter:
    name = PDF_PAGE_SOURCE_TYPE
    input_class = PDFPageInput
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        identity = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(name=identity, source_id=identity, **value.model_dump())

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class _PDFPageTextEvidence:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_TYPE,
            source_id=source.source_id,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION: SourceDefinition[PDFPageInput, PDFPageSource, PDFPageInput] = AdapterSourceDefinition(
    adapter=_PDFPageAdapter(),
    projections=(_PDFPageTextEvidence(),),
)


async def search_pdf_pages(
    memory: ScopedMemoryApplication,
    question: str,
    limit: int = 10,
) -> tuple[MemoryCitation, ...]:
    """Return the exact page citations that best answer one question.

    A financial report asks about one filer, so the search first settles which
    document in the scope the question is about and then works inside that
    document only. Pages are ranked by how completely they cover the question,
    with the page heading counted separately: the heading of a financial report
    page names its section, and a question about legal proceedings or about a
    segment matches that heading rather than the prose around it.

    Every returned citation points at one stored page, so a caller can read back
    the exact text, document digest, and physical page number.
    """

    from powercontext.artifacts.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if limit < 1 or not question.strip():
        return ()
    document_id = await _resolve_document(memory, question, SearchMemoryRequest)
    if document_id is None:
        return ()
    pages = await _document_pages(memory, document_id, SearchMemoryRequest)
    if not pages:
        return ()
    ranked = _rank_pages(pages, question)
    citations: list[MemoryCitation] = []
    seen: set[str] = set()
    for hit in ranked:
        citation = MemoryCitation(
            memory_ref=hit.memory_ref,
            entry_id=hit.entry_id,
            entry_version_id=hit.entry_version_id,
        )
        entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        if len(entry.sources) != 1 or entry.sources[0].source_type != PDF_PAGE_SOURCE_TYPE:
            continue
        source_id = entry.sources[0].source_id
        if source_id in seen:
            continue
        seen.add(source_id)
        citations.append(citation)
        if len(citations) >= limit:
            break
    return tuple(citations)


def _document_id_of(text: str) -> str:
    """Read the document identifier back from a stored page projection."""

    return text.split("\n", 1)[0].strip()


def _document_query(document_id: str) -> str:
    """Build the lexical query that addresses one document's pages only.

    Every stored page starts with its document identifier, so the identifier is a
    term that matches each page of that document and no page of any other one.
    Separators become underscores so the shared analyzer keeps the identifier as
    a single term.
    """

    return re.sub(r"[^0-9A-Za-z]+", "_", document_id)


def _entity_terms(question: str) -> tuple[str, ...]:
    """Return the capitalised words in a question that may name the filer."""

    terms: list[str] = []
    for match in _WORD.finditer(question):
        word = match.group(0)
        if not word[:1].isupper() or word.lower() in _QUESTION_WORDS:
            continue
        if len(word) < 2 or not any(character.isalpha() for character in word):
            continue
        terms.append(word.lower())
    return tuple(dict.fromkeys(terms))


async def _resolve_document(memory, question: str, request_model) -> str | None:
    """Settle which document in the scope the question is about."""

    candidates = await _candidate_documents(memory, question, request_model)
    if not candidates:
        return None
    entities = _entity_terms(question)
    best = _named_document(candidates, entities)
    if best is not None:
        return best
    # Without a named filer, trust the document the question matched most closely.
    return max(candidates, key=lambda document_id: candidates[document_id])


async def _candidate_documents(memory, question: str, request_model) -> dict[str, float]:
    """Collect the documents the scope holds, weighted by how well they matched."""

    page = await memory.search(request_model(query=question, mode="fts", limit=_DISCOVERY_LIMIT))
    candidates: dict[str, float] = {}
    for rank, hit in enumerate(page.hits, start=1):
        document_id = _document_id_of(hit.text)
        if document_id:
            candidates[document_id] = candidates.get(document_id, 0.0) + 1.0 / rank
    for entity in _entity_terms(question)[:4]:
        entity_page = await memory.search(request_model(query=entity, mode="fts", limit=_ENTITY_LIMIT))
        for hit in entity_page.hits:
            document_id = _document_id_of(hit.text)
            if document_id and document_id not in candidates:
                candidates[document_id] = 0.0
    return candidates


def _named_document(candidates: dict[str, float], entities: tuple[str, ...]) -> str | None:
    """Return the document whose identifier names one of the question's entities."""

    best = None
    best_score = 0
    for document_id in candidates:
        lowered = document_id.lower()
        score = max((len(entity) for entity in entities if entity in lowered), default=0)
        if score > best_score:
            best, best_score = document_id, score
    return best


async def _document_pages(memory, document_id: str, request_model) -> tuple[MemoryHit, ...]:
    """Read every stored page of one document back through the Memory index."""

    limit = _DOCUMENT_LIMIT
    hits: tuple[MemoryHit, ...] = ()
    while True:
        page = await memory.search(request_model(query=_document_query(document_id), mode="fts", limit=limit))
        hits = tuple(hit for hit in page.hits if _document_id_of(hit.text) == document_id)
        if len(page.hits) < limit or limit >= _DOCUMENT_LIMIT_MAXIMUM:
            return hits
        limit *= 2


def _query_terms(question: str) -> tuple[str, ...]:
    """Return the lexical terms the shared index sees for one question.

    Repeated words stay repeated: a term the question insists on carries more of
    its weight, exactly as a repeated word carries more of the query.
    """

    from powercontext.builtin.artifacts.search import analyze_fts_query

    terms = analyze_fts_query(question).split()
    expanded: list[str] = []
    for term in terms:
        expanded.append(term)
        expanded.extend(sorted(_numeric_variants(term)))
    return tuple(expanded)


def _numeric_variants(term: str) -> set[str]:
    """Return the plain numbers a term carries, so 'FY2022' also matches '2022'."""

    if not any(character.isdigit() for character in term):
        return set()
    variants: set[str] = set()
    for run in re.findall(r"\d+", term):
        if len(run) < 2:
            continue
        variants.add(run)
        if len(run) == 2:
            variants.add(f"20{run}")
        elif len(run) == 4 and run.startswith("19"):
            variants.add(f"20{run[2:]}")
    return {variant for variant in variants if variant != term}


def _rank_pages(hits: tuple[MemoryHit, ...], question: str) -> list[MemoryHit]:
    """Rank the pages of one document against one question."""

    from powercontext.builtin.artifacts.search import analyze_text

    terms = _query_terms(question)
    if not terms:
        return list(hits)
    documents = _page_documents(hits, analyze_text)
    frequencies: Counter[str] = Counter()
    for _hit, counts, _length, _heading, _family in documents:
        frequencies.update(counts.keys())
    total = len(documents)
    idf = _inverse_document_frequency(frequencies, total)
    scores = _coverage_scores(documents, terms, idf)
    _apply_relevance_feedback(documents, scores, terms, idf)
    _apply_page_priors(documents, scores, terms, idf)
    order = sorted(range(total), key=lambda index: (-scores[index], index))
    return [documents[index][0] for index in order]


def _page_documents(hits, analyze_text) -> list[tuple[MemoryHit, Counter[str], int, set[str], str | None]]:
    """Describe every retrieved page: its terms, its heading, and its statement family."""

    documents = []
    for hit in hits:
        body = hit.text.split("\n", 1)[1] if "\n" in hit.text else hit.text
        tokens = analyze_text(hit.text).split()
        heading = set(analyze_text(" ".join(body.split())[:_HEADING_CHARACTERS]).split())
        documents.append((hit, Counter(tokens), len(tokens), heading, _statement_family(body)))
    return documents


def _inverse_document_frequency(frequencies: Counter[str], total: int):
    """Return the inverse document frequency of every term inside one document."""

    def idf(term: str) -> float:
        document_frequency = frequencies.get(term, 0)
        if not document_frequency:
            return 0.0
        return math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))

    return idf


def _coverage_scores(documents, terms, idf) -> dict[int, float]:
    """Score every page by the weight of the question terms it carries."""

    scores: dict[int, float] = dict.fromkeys(range(len(documents)), 0.0)
    for term in terms:
        weight = idf(term)
        if weight <= 0:
            continue
        for index, (_hit, counts, _length, _heading, _family) in enumerate(documents):
            if term in counts:
                scores[index] += weight
    return scores


def _apply_relevance_feedback(documents, scores, terms, idf) -> None:
    """Widen the query with the vocabulary of the pages that answer it best.

    A financial report answers a question in the report's own words, so the pages
    that match the question best name the terms the report uses for the same
    thing. Those terms reach the page the question paraphrases instead of the
    page that merely repeats the wording.
    """

    seeds = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:_RELEVANCE_SEEDS]
    if not seeds:
        return
    weights: Counter[str] = Counter()
    seed_total = sum(score for _index, score in seeds) or 1.0
    for index, score in seeds:
        share = score / seed_total
        _hit, counts, length, _heading, _family = documents[index]
        for term, occurrences in counts.items():
            if term in terms or len(term) < 3:
                continue
            weights[term] += share * (occurrences / max(1, length)) * idf(term)
    for term, _weight in weights.most_common(_RELEVANCE_EXPANSION):
        weight = _RELEVANCE_EXPANSION_WEIGHT * idf(term)
        if weight <= 0:
            continue
        for index, (_hit, counts, _length, _heading, _family) in enumerate(documents):
            if term in counts:
                scores[index] += weight


def _apply_page_priors(documents, scores, terms, idf) -> None:
    """Reward the page that answers the kind of question that was asked.

    A question about liquidity belongs to the balance sheet, a question about
    margin belongs to the statement of operations, and a question about cash
    generation belongs to the statement of cash flows. A financial report states
    each of those once, so the statement the question points at outranks the
    discussion that happens to repeat the wording. The heading of a page carries
    the same signal and is weighted with it.
    """

    wanted = _wanted_family(terms)
    for index, (_hit, _counts, _length, heading, family) in enumerate(documents):
        value = scores[index]
        if family in _STATEMENT_FAMILIES:
            value *= 1 + _STATEMENT_PRIOR
        if family and family == wanted:
            value *= 1 + _FAMILY_PRIOR
        for term in terms:
            if term in heading:
                value += _HEADING_WEIGHT * idf(term)
        scores[index] = value


def _statement_family(text: str) -> str | None:
    """Name the financial statement a page belongs to, if it belongs to one."""

    for family, pattern in _STATEMENT_PATTERNS:
        if pattern.search(text):
            return family
    return None


def _wanted_family(terms: tuple[str, ...]) -> str | None:
    """Name the statement family the question is about, if it is about one."""

    best = None
    best_score = 0
    for family, vocabulary in _FAMILY_VOCABULARY.items():
        score = sum(1 for term in terms if term in vocabulary)
        if score > best_score:
            best, best_score = family, score
    return best


__all__ = [
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_TYPE",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
