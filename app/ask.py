"""Optional local LLM layer for natural-language Q&A over FTS5 retrieval.

Requires ``llama-cpp-python`` (see ``requirements-llm.txt``) and a GGUF instruct
model dropped into the ``models/`` folder beside the executable. When absent, all
entry points degrade gracefully — keyword search is unaffected.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass

from . import paths, search

try:
    from llama_cpp import Llama

    _HAS_LLAMA = True
except ImportError:
    Llama = None  # type: ignore[misc, assignment]
    _HAS_LLAMA = False

_IDLE_TIMEOUT_S = 300
_MAX_CONTEXT_PAGES = 8
_EXCERPT_CHARS = 800
_MAX_OUTPUT_EXPAND = 128
_MAX_OUTPUT_ANSWER = 384
_N_CTX = 4096
_N_THREADS = 4

NOT_FOUND = "I couldn't find this in the indexed documents."

_model_lock = threading.Lock()
_llm: Llama | None = None
_model_path: str | None = None
_last_used: float = 0.0
_gen_lock = threading.Lock()  # one generation at a time

_CITATION_RE = re.compile(
    r"\[([^\]]+?)\s+p\.(\d+)\]"
    r"|(?:\(|\[)([^\])\(]+?)[,\s]+p(?:age)?\.?\s*(\d+)(?:\)|\])"
    r"|([^\s\[\(]{3,80}?)\s+p\.(\d+)",
    re.IGNORECASE,
)

# Too generic to use as single-term FTS queries — they match almost any PDF.
_WEAK_TERMS = frozenset({
    "word", "words", "text", "page", "pages", "file", "files", "data",
    "list", "lists", "code", "value", "values", "column", "table", "figure",
    "number", "numbers", "count", "letter", "letters", "string", "strings",
    "line", "lines", "name", "names", "type", "types", "example", "examples",
    "section", "chapter", "book", "books", "read", "reading", "show", "shown",
    "using", "used", "use", "make", "made", "get", "set", "see", "also",
    "note", "notes", "item", "items", "field", "fields", "row", "rows",
    "cell", "cells", "plot", "chart", "graph", "index", "result", "results",
    "times", "time", "often", "frequency", "occurrences", "occurrence",
    "appear", "appears", "mentioned", "total", "overall", "across",
})

# Corpus-wide word-count questions — answered deterministically, not via LLM.
_WORD_COUNT_RE = re.compile(
    r"(?:"
    r"how\s+many\s+times\s+(?:is\s+|does\s+|did\s+)?(?:the\s+word\s+)?"
    r"['\"]?(\w+)['\"]?\s+"
    r"(?:appear(?:s|ed)?|used|mentioned|occur(?:s|red)?|found|show\s+up)"
    r"|how\s+many\s+times\s+(?:does\s+)?(?:the\s+word\s+)?"
    r"['\"]?(\w+)['\"]?\s+appear"
    r"|count\s+(?:the\s+)?(?:occurrences?\s+of\s+|times\s+)?(?:the\s+word\s+)?"
    r"['\"]?(\w+)['\"]?"
    r"|how\s+often\s+(?:is\s+)?(?:the\s+word\s+)?['\"]?(\w+)['\"]?\s+used"
    r")",
    re.IGNORECASE,
)

# Minimum fraction of search terms that must appear in an excerpt to keep it.
_MIN_RELEVANCE = 0.5

# Questions that aren't about document content — answer from general knowledge.
_OUT_OF_SCOPE_RE = re.compile(
    r"(?:"
    r"how\s+many\s+['\w]{0,3}s?\s+in\s+(?:the\s+)?(?:word|letter|string|name)\s+\w+"
    r"|how\s+do\s+you\s+spell"
    r"|what\s+is\s+\d+\s*[\+\-\*\/]\s*\d+"
    r")",
    re.IGNORECASE,
)

# Question words stripped from FTS fallback queries (implicit AND otherwise).
_STOP_WORDS = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how",
    "is", "are", "was", "were", "be", "been", "being",
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "into", "about", "our", "your", "my",
    "do", "does", "did", "can", "could", "would", "should", "will",
    "this", "that", "these", "those", "it", "its", "we", "they", "their",
    "me", "tell", "explain", "describe", "find", "show", "give", "any",
    "there", "have", "has", "had", "not", "all", "some", "many", "much",
})


@dataclass
class PageContext:
    document_id: int
    filename: str
    folder: str
    page_number: int
    excerpt: str
    rank: float
    snippet: str = ""  # FTS match region — often more relevant than page start


@dataclass
class AskResult:
    answer: str
    sources: list[dict]
    search_queries: list[str]
    took_ms: float


def llama_installed() -> bool:
    return _HAS_LLAMA


def model_path() -> str | None:
    p = paths.find_gguf_model()
    return p.name if p else None


def model_available() -> bool:
    return _HAS_LLAMA and paths.find_gguf_model() is not None


def model_loaded() -> bool:
    with _model_lock:
        return _llm is not None


def status() -> dict:
    path = paths.find_gguf_model()
    return {
        "available": _HAS_LLAMA and path is not None,
        "llama_installed": _HAS_LLAMA,
        "model_name": path.name if path else None,
        "loaded": model_loaded(),
        "models_dir": str(paths.models_dir()),
    }


def _unload_if_idle() -> None:
    global _llm, _model_path
    with _model_lock:
        if _llm is None:
            return
        if time.time() - _last_used < _IDLE_TIMEOUT_S:
            return
        _llm = None
        _model_path = None


def _get_llm() -> Llama:
    """Lazy-load the GGUF model; unload after idle timeout."""
    global _llm, _model_path, _last_used

    _unload_if_idle()

    path = paths.find_gguf_model()
    if path is None:
        raise RuntimeError(
            f"no GGUF model in {paths.models_dir()} — drop a *.gguf file there"
        )
    if not _HAS_LLAMA:
        raise RuntimeError(
            "llama-cpp-python not installed — pip install -r requirements-llm.txt"
        )

    key = str(path.resolve())
    with _model_lock:
        if _llm is None or _model_path != key:
            _llm = Llama(
                model_path=key,
                n_ctx=_N_CTX,
                n_threads=_N_THREADS,
                verbose=False,
            )
            _model_path = key
        _last_used = time.time()
        return _llm


def _chat(llm: Llama, system: str, user: str, max_tokens: int) -> str:
    out = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0.1,
    )
    text = (out["choices"][0]["message"]["content"] or "").strip()
    if text:
        return text
    # Some GGUF models (e.g. extract/VL variants) ignore chat roles — try completion.
    prompt = f"{system}\n\n{user}\n\n"
    out2 = llm(prompt, max_tokens=max_tokens, temperature=0.1, echo=False)
    return (out2["choices"][0]["text"] or "").strip()


def _significant_words(text: str) -> list[str]:
    seen: set[str] = set()
    words: list[str] = []
    for w in re.findall(r"\w{3,}", text.lower()):
        if w not in _STOP_WORDS and w not in seen:
            seen.add(w)
            words.append(w)
    return words


def _acronyms(question: str) -> list[str]:
    """Uppercase tokens like CGM, NDSS — kept even when short."""
    return [
        m.group().lower()
        for m in re.finditer(r"\b[A-Z]{2,6}\b", question)
    ]


def _search_terms(question: str) -> list[str]:
    """Keywords worth matching — drops generic terms like 'word' or 'count'."""
    terms = [w for w in _significant_words(question) if w not in _WEAK_TERMS]
    for a in _acronyms(question):
        if a not in terms:
            terms.append(a)
    return terms


def _quoted_phrases(question: str) -> list[str]:
    return [
        m.group(1).strip()
        for m in re.finditer(r'"([^"]+)"', question)
        if len(m.group(1).strip()) >= 3
    ]


def _is_out_of_scope(question: str) -> bool:
    """True for spelling puzzles, arithmetic, etc. — not document questions."""
    if _parse_word_count(question):
        return False  # handled by deterministic counter
    return bool(_OUT_OF_SCOPE_RE.search(question))


def _parse_word_count(question: str) -> str | None:
    """Extract a single word to count across the indexed corpus, if asked."""
    m = _WORD_COUNT_RE.search(question)
    if not m:
        return None
    word = next(g for g in m.groups() if g)
    if word.lower() in _STOP_WORDS or len(word) < 2:
        return None
    return word


def _count_word_in_corpus(
    conn,
    word: str,
    *,
    folder: str | None = None,
    method: str | None = None,
) -> AskResult:
    """Exact whole-word count across indexed pages — no LLM."""
    t0 = time.perf_counter()
    word_re = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
    sql = """
        SELECT p.document_id, p.page_number, p.text_content,
               d.filename, d.folder
        FROM pages p JOIN documents d ON d.id = p.document_id
        WHERE 1=1
    """
    params: list = []
    if folder:
        sql += " AND d.folder LIKE ?"
        params.append(f"%{folder}%")
    if method in ("native", "ocr"):
        sql += " AND p.extraction_method = ?"
        params.append(method)

    total = 0
    page_hits: list[tuple[int, dict]] = []
    doc_ids: set[int] = set()

    for row in conn.execute(sql, params):
        n = len(word_re.findall(row["text_content"]))
        if n:
            total += n
            doc_ids.add(row["document_id"])
            page_hits.append((n, dict(row)))

    page_hits.sort(key=lambda x: -x[0])
    sources = [
        {
            "document_id": r["document_id"],
            "filename": r["filename"],
            "folder": r["folder"],
            "page_number": r["page_number"],
            "count": n,
        }
        for n, r in page_hits[:_MAX_CONTEXT_PAGES]
    ]

    if total == 0:
        answer = (
            f'The word "{word}" does not appear in your indexed documents.'
        )
    else:
        answer = (
            f'The word "{word}" appears {total:,} times across '
            f"{len(page_hits)} page(s) in {len(doc_ids)} document(s)."
        )
        top = page_hits[:3]
        if top:
            parts = [
                f"{r['filename']} p.{r['page_number']} ({n:,})" for n, r in top
            ]
            answer += " Most occurrences: " + ", ".join(parts) + "."

    took_ms = round((time.perf_counter() - t0) * 1000, 1)
    return AskResult(
        answer=answer,
        sources=sources,
        search_queries=[f"count:{word}"],
        took_ms=took_ms,
    )


def _is_searchable_query(q: str) -> bool:
    """Reject FTS queries that are a single generic term."""
    q = q.strip()
    if not q:
        return False
    if q.startswith('"') and q.endswith('"') and len(q) > 4:
        return True
    if " OR " in q.upper():
        terms = [t.strip().lower() for t in re.split(r"\s+OR\s+", q, flags=re.I)]
        strong = [t for t in terms if t not in _WEAK_TERMS and t not in _STOP_WORDS]
        return len(strong) >= 2 or any(len(t) >= 3 for t in strong)
    words = [w.lower() for w in re.findall(r"\w{2,}", q)]
    strong = [w for w in words if w not in _WEAK_TERMS and w not in _STOP_WORDS]
    if len(strong) >= 2:
        return True
    return len(strong) == 1 and len(strong[0]) >= 3


def _relevance_score(
    ctx: PageContext, terms: list[str], phrases: list[str],
    query_terms: list[str] | None = None,
) -> float:
    combined = f"{ctx.snippet} {ctx.excerpt}".lower()
    if phrases:
        if any(p.lower() in combined for p in phrases):
            return 1.0
    check = list(terms)
    if query_terms:
        check = list(dict.fromkeys(check + query_terms))
    if not check:
        return 0.0
    hits = sum(1 for t in check if t in combined)
    return hits / len(terms) if terms else hits / len(check)


def _filter_relevant_contexts(
    contexts: list[PageContext],
    question: str,
    search_queries: list[str] | None = None,
) -> list[PageContext]:
    """Keep excerpts that actually mention the question's subject terms."""
    terms = _search_terms(question)
    phrases = _quoted_phrases(question)
    query_terms: list[str] = []
    for q in search_queries or []:
        query_terms.extend(_search_terms(q))
        query_terms.extend(w.lower() for w in _acronyms(q))
    if not terms and not phrases and not query_terms:
        return []
    scored: list[tuple[float, PageContext]] = []
    for ctx in contexts:
        score = _relevance_score(ctx, terms, phrases, query_terms)
        if score >= _MIN_RELEVANCE:
            scored.append((score, ctx))
    scored.sort(key=lambda x: -x[0])
    return [ctx for _, ctx in scored[:_MAX_CONTEXT_PAGES]]


def _fallback_queries(question: str) -> list[str]:
    """FTS-safe queries when the LLM returns unusable expansion output."""
    words = _search_terms(question)
    phrases = _quoted_phrases(question)
    queries: list[str] = []
    queries.extend(f'"{p}"' for p in phrases)
    if len(words) >= 2:
        queries.append(" OR ".join(words[:6]))
        queries.append(" ".join(words[:3]))
    elif len(words) == 1:
        queries.append(words[0])
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        if _is_searchable_query(q) and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:5]


def _extract_keywords_from_text(raw: str) -> list[str]:
    """Pull search terms from free-form / JSON-ish model output."""
    skip = {"question", "answer", "excerpts", "query", "queries", "response"}
    queries: list[str] = []
    for m in re.finditer(r'"([^"]{3,60})"', raw):
        phrase = m.group(1).strip()
        if phrase.lower() in skip or phrase.endswith("?"):
            continue
        if phrase and len(_significant_words(phrase)) >= 1:
            queries.append(phrase)
    return queries[:4]


def _looks_like_garbage(answer: str) -> bool:
    """True when the model echoed context headers instead of answering."""
    if "doc_id=" in answer or 'filename="' in answer:
        return True
    if answer.strip().startswith("[doc_id"):
        return True
    prose = re.sub(r"\[[^\]]*\]", "", answer).strip()
    if len(prose) < 25:
        return True
    return False


def _parse_query_list(raw: str, question: str) -> list[str]:
    """Extract FTS5 query strings from model output; merge with keyword fallbacks."""
    raw = raw.strip()
    queries: list[str] = []

    m = re.search(r"\[[\s\S]*?\]", raw)
    if m:
        try:
            arr = json.loads(m.group())
            if isinstance(arr, list):
                queries.extend(str(q).strip() for q in arr if str(q).strip())
        except json.JSONDecodeError:
            pass

    queries.extend(_extract_keywords_from_text(raw))
    queries.extend(_fallback_queries(question))

    # Drop queries that are just the full question (often 0-hit AND queries).
    q_lower = question.lower().strip()
    out: list[str] = []
    seen: set[str] = set()
    for q in queries:
        q = q.strip()
        if not q or q.lower() == q_lower:
            continue
        # Skip JSON key names the model sometimes echoes.
        if q.lower() in {"question", "answer", "excerpts", "query", "queries"}:
            continue
        if not _is_searchable_query(q):
            continue
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out[:6] if out else _fallback_queries(question)


def expand_query(question: str) -> list[str]:
    llm = _get_llm()
    system = (
        "You translate document search questions into keyword queries for a "
        "full-text search engine. Output ONLY a JSON array of 2-4 short search "
        "strings. Use plain keywords and quoted phrases for exact matches. "
        "No explanation, no markdown."
    )
    user = f"Question: {question}"
    raw = _chat(llm, system, user, _MAX_OUTPUT_EXPAND)
    with _model_lock:
        global _last_used
        _last_used = time.time()
    return _parse_query_list(raw, question)


def _search_one(
    conn,
    q: str,
    *,
    folder: str | None = None,
    method: str | None = None,
) -> list[search.Hit]:
    try:
        return search.search(
            conn, q, limit=50, folder=folder, method=method,
            hl_open="", hl_close="",
        )
    except Exception:
        return []


def _retrieve_pages(
    conn,
    queries: list[str],
    *,
    folder: str | None = None,
    method: str | None = None,
) -> list[search.Hit]:
    """Run each FTS query and merge by (document_id, page_number), best rank wins."""
    best: dict[tuple[int, int], search.Hit] = {}
    tried: set[str] = set()

    def _try(q: str) -> None:
        q = q.strip()
        if not q or q in tried or not _is_searchable_query(q):
            return
        tried.add(q)
        for h in _search_one(conn, q, folder=folder, method=method):
            key = (h.document_id, h.page_number)
            prev = best.get(key)
            if prev is None or h.rank < prev.rank:
                best[key] = h

    for q in queries:
        _try(q)
        if not best:
            words = _search_terms(q)
            if len(words) >= 2:
                _try(" OR ".join(words[:6]))
            elif len(words) == 1 and len(words[0]) >= 5:
                _try(words[0])

    ranked = sorted(best.values(), key=lambda h: h.rank)
    return ranked[:_MAX_CONTEXT_PAGES]


def _fetch_page_text(conn, document_id: int, page_number: int) -> str:
    row = conn.execute(
        """SELECT text_content FROM pages
           WHERE document_id=? AND page_number=?""",
        (document_id, page_number),
    ).fetchone()
    return row["text_content"] if row else ""


def _build_contexts(conn, hits: list[search.Hit]) -> list[PageContext]:
    contexts: list[PageContext] = []
    for h in hits:
        text = _fetch_page_text(conn, h.document_id, h.page_number)
        excerpt = " ".join(text.split())
        if len(excerpt) > _EXCERPT_CHARS:
            excerpt = excerpt[:_EXCERPT_CHARS] + "…"
        snippet = " ".join((h.snippet or "").split())
        # Prefer the FTS match region for display when the hit is not at page start.
        display = snippet if len(snippet) >= 40 else excerpt
        contexts.append(PageContext(
            document_id=h.document_id,
            filename=h.filename,
            folder=h.folder,
            page_number=h.page_number,
            excerpt=display,
            rank=h.rank,
            snippet=snippet,
        ))
    return contexts


def _format_context_block(ctx: PageContext) -> str:
    return (
        f'[doc_id={ctx.document_id} filename="{ctx.filename}" page={ctx.page_number}]\n'
        f"{ctx.excerpt}"
    )


def _citation_groups(match: re.Match) -> tuple[str, int] | None:
    """Normalize alternate citation patterns to (filename, page)."""
    if match.group(1) is not None:
        return match.group(1).strip(), int(match.group(2))
    if match.group(3) is not None:
        return match.group(3).strip(), int(match.group(4))
    if match.group(5) is not None:
        return match.group(5).strip(), int(match.group(6))
    return None


def _match_citation(
    fname: str, page: int, contexts: list[PageContext],
) -> PageContext | None:
    fl = fname.lower()
    for ctx in contexts:
        if ctx.page_number != page:
            continue
        kl = ctx.filename.lower()
        if kl == fl or kl in fl or fl in kl:
            return ctx
    return None


def _valid_citations(answer: str, contexts: list[PageContext]) -> set[tuple[str, int]]:
    """Return (filename_lower, page) pairs cited in the answer that match context."""
    found: set[tuple[str, int]] = set()
    for m in _CITATION_RE.finditer(answer):
        parsed = _citation_groups(m)
        if parsed is None:
            continue
        fname, page = parsed
        ctx = _match_citation(fname, page, contexts)
        if ctx:
            found.add((ctx.filename.lower(), ctx.page_number))
    return found


def _extractive_fallback(contexts: list[PageContext], question: str = "") -> str:
    """Short grounded summary when the LLM cannot produce valid citations."""
    contexts = _filter_relevant_contexts(contexts, question)
    if not contexts:
        return NOT_FOUND
    keywords = _search_terms(question)
    parts: list[str] = []
    for ctx in contexts[:3]:
        text = ctx.excerpt
        best: str | None = None
        if keywords:
            for sent in re.split(r"(?<=[.!?])\s+", text):
                sl = sent.lower()
                if sum(1 for k in keywords if k in sl) >= max(1, len(keywords) // 2):
                    best = sent.strip()
                    break
        if best:
            parts.append(f"{best} [{ctx.filename} p.{ctx.page_number}]")
    return " ".join(parts) if parts else NOT_FOUND


def _filter_answer(answer: str, contexts: list[PageContext], question: str = "") -> str:
    """Drop sentences with bad citations; fall back to extractive summary."""
    if NOT_FOUND in answer or _looks_like_garbage(answer):
        return _extractive_fallback(contexts, question) if contexts else NOT_FOUND

    valid = _valid_citations(answer, contexts)
    if not valid:
        cleaned = re.sub(r"\{[^{}]*\}", "", answer).strip()
        if len(cleaned) > 40 and not _looks_like_garbage(cleaned):
            return cleaned + " " + " ".join(
                f"[{c.filename} p.{c.page_number}]" for c in contexts[:2]
            )
        return _extractive_fallback(contexts, question)

    sentences = re.split(r"(?<=[.!?])\s+", answer)
    kept: list[str] = []
    for sent in sentences:
        cites = list(_CITATION_RE.finditer(sent))
        if not cites:
            kept.append(sent)
            continue
        sent_ok = True
        for m in cites:
            parsed = _citation_groups(m)
            if parsed is None:
                sent_ok = False
                break
            fname, page = parsed
            if _match_citation(fname, page, contexts) is None:
                sent_ok = False
                break
        if sent_ok:
            kept.append(sent)
    result = " ".join(kept).strip()
    return result if result else _extractive_fallback(contexts, question)


def answer_with_citations(question: str, contexts: list[PageContext]) -> str:
    if not contexts:
        return NOT_FOUND

    llm = _get_llm()
    blocks = "\n\n".join(_format_context_block(c) for c in contexts)
    # List exact citation targets so small / non-instruct models can copy them.
    cite_list = ", ".join(f"[{c.filename} p.{c.page_number}]" for c in contexts)
    system = (
        "Answer the question using ONLY the excerpts below. "
        "Put a citation after each fact using EXACTLY one of these forms: "
        f"{cite_list}. "
        "Copy the filename and page number exactly. "
        "If the excerpts lack the answer, say exactly: "
        f'"{NOT_FOUND}"'
    )
    user = f"Excerpts:\n\n{blocks}\n\nQuestion: {question}\n\nAnswer:"
    raw = _chat(llm, system, user, _MAX_OUTPUT_ANSWER)
    with _model_lock:
        global _last_used
        _last_used = time.time()
    return _filter_answer(raw, contexts, question)


def _contexts_to_sources(contexts: list[PageContext]) -> list[dict]:
    return [
        {
            "document_id": c.document_id,
            "filename": c.filename,
            "folder": c.folder,
            "page_number": c.page_number,
        }
        for c in contexts
    ]


def ask(
    conn,
    question: str,
    *,
    folder: str | None = None,
    method: str | None = None,
) -> AskResult:
    """Full pipeline: expand query → FTS retrieve → grounded answer."""
    question = question.strip()
    if not question:
        raise ValueError("empty question")

    if not model_available():
        raise RuntimeError(
            "Ask mode unavailable — install llama-cpp-python and add a GGUF model "
            f"to {paths.models_dir()}"
        )

    t0 = time.perf_counter()
    with _gen_lock:
        count_word = _parse_word_count(question)
        if count_word:
            return _count_word_in_corpus(
                conn, count_word, folder=folder, method=method,
            )

        if _is_out_of_scope(question):
            return AskResult(
                answer=NOT_FOUND,
                sources=[],
                search_queries=[],
                took_ms=round((time.perf_counter() - t0) * 1000, 1),
            )

        search_queries = expand_query(question)
        fallback = _fallback_queries(question)
        seen_q: set[str] = set()
        merged_queries: list[str] = []
        for q in search_queries + fallback:
            if q not in seen_q:
                seen_q.add(q)
                merged_queries.append(q)
        search_queries = merged_queries
        if not search_queries:
            return AskResult(
                answer=NOT_FOUND,
                sources=[],
                search_queries=[],
                took_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
        hits = _retrieve_pages(conn, search_queries, folder=folder, method=method)
        contexts = _filter_relevant_contexts(
            _build_contexts(conn, hits), question, search_queries,
        )
        if not contexts:
            return AskResult(
                answer=NOT_FOUND,
                sources=[],
                search_queries=search_queries,
                took_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
        answer = answer_with_citations(question, contexts)

    took_ms = (time.perf_counter() - t0) * 1000
    return AskResult(
        answer=answer,
        sources=_contexts_to_sources(contexts),
        search_queries=search_queries,
        took_ms=round(took_ms, 1),
    )
