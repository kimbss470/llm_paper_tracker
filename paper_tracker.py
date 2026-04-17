#!/usr/bin/env python3
"""Fetch recent LLM-efficiency papers and build a static HTML tracker site."""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
SITE_DIR = BASE_DIR / "site"
PAPER_DIR = SITE_DIR / "papers"

USER_AGENT = "llm-paper-tracker/1.0 (contact: maintainer@example.com)"
MIN_PUBLICATION_DATE = "2025-01-01"
KEYWORDS = [
    "LLM",
    "KV Cache",
    "MoE",
    "Efficient",
    "Quantization",
    "Long Context",
    "Inference Optimization",
]
MAX_RESULTS = 80
REQUEST_TIMEOUT = 25
MAX_HTTP_RETRIES = 4

ALLOWED_VENUES = ["Preprint (arXiv)", "ICML", "NeurIPS", "ICLR"]
OPENALEX_TARGET_VENUES = ["ICML", "NeurIPS", "ICLR"]

LLM_PATTERNS = [
    r"\bllm\b",
    r"large\s+language\s+model",
]

FOCUS_PATTERNS = [
    r"kv\s*cache",
    r"cache\s*compression",
    r"cache\s*eviction",
    r"\bmoe\b",
    r"mixture[-\s]of[-\s]experts",
    r"quantiz",
    r"low[-\s]?bit",
    r"int4|int8|fp8|gptq|awq",
    r"efficient",
    r"long\s+context",
    r"context\s+window",
    r"inference",
    r"latency",
    r"throughput",
    r"serving",
]

CATEGORY_RULES: dict[str, list[str]] = {
    "MoE": [r"\bmoe\b", r"mixture[-\s]of[-\s]experts"],
    "KV Cache Compression": [r"kv\s*cache", r"cache\s*compression", r"cache\s*eviction"],
    "Quantization": [r"quantiz", r"low[-\s]?bit", r"int4|int8|fp8|gptq|awq"],
    "Efficient Attention": [r"flashattention", r"linear\s+attention", r"sparse\s+attention"],
    "Long Context": [r"long\s+context", r"context\s+window", r"rope", r"position\s+interpolation"],
    "Distillation": [r"distill"],
    "Inference Optimization": [r"inference", r"latency", r"throughput", r"serving"],
    "Training Efficiency": [r"efficient\s+training", r"gradient\s+checkpoint", r"memory\s+efficient"],
}


@dataclass
class Paper:
    paper_id: str
    source: str
    title: str
    authors: list[str]
    affiliations: list[str]
    venue: str
    year: int
    published_date: str
    category: list[str]
    abstract: str
    url: str
    doi: str | None
    summary: dict[str, list[str]]


def request_text(url: str, params: dict[str, Any] | None = None) -> str:
    headers = {"User-Agent": USER_AGENT}
    last_error: Exception | None = None

    for attempt in range(MAX_HTTP_RETRIES):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)

            # Retry transient upstream/rate-limit failures.
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < MAX_HTTP_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue

            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            last_error = exc
            if attempt < MAX_HTTP_RETRIES - 1:
                time.sleep(2**attempt)
                continue
            raise

    if last_error:
        raise last_error
    raise RuntimeError("request_text failed unexpectedly without an exception")


def request_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    text = request_text(url, params=params)
    return json.loads(text)


def normalize_title(title: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", title.lower())
    return compact[:200]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:90] if slug else "paper"


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def clean_affiliations(values: list[str]) -> list[str]:
    blocked = [
        r"\barxiv\b",
        r"\bproceedings\b",
        r"\bjournal\b",
        r"\bconference\b",
        r"\bworkshop\b",
        r"\bvolume\b",
        r"\bvol\.?\b",
        r"\bissue\b",
        r"\bissn\b",
        r"\bdoi\b",
    ]
    cleaned: list[str] = []
    for raw in values:
        name = normalize_spaces(raw)
        if not name:
            continue
        # Drop trailing conference/journal metadata fragments from affiliation strings.
        name = re.sub(
            r"\s*[,;\-–—:]\s*(in\s+)?(proceedings|journal|conference|workshop|symposium|transactions?)\b.*$",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip(" ,;:-")
        if not name:
            continue
        lowered = name.lower()
        if any(re.search(pattern, lowered) for pattern in blocked):
            continue
        cleaned.append(name)
    return sorted(set(cleaned))


def infer_arxiv_venue(journal_ref: str | None) -> str:
    normalized = normalize_spaces(journal_ref or "")
    return normalized if normalized else "Preprint (arXiv)"


def canonicalize_allowed_venue(venue: str | None) -> str | None:
    normalized = normalize_spaces(venue or "")
    if not normalized:
        return None

    lowered = normalized.lower()
    if "arxiv" in lowered:
        return "Preprint (arXiv)"
    if "neurips" in lowered or "neural information processing systems" in lowered:
        return "NeurIPS"
    if "iclr" in lowered or "learning representations" in lowered:
        return "ICLR"
    if "icml" in lowered or "machine learning" in lowered:
        return "ICML"
    return None


def fetch_openalex_source_ids() -> list[str]:
    source_ids: set[str] = set()

    for venue in OPENALEX_TARGET_VENUES:
        payload = request_json(
            "https://api.openalex.org/sources",
            params={
                "search": venue,
                "per-page": "25",
                "mailto": "maintainer@example.com",
            },
        )

        for item in payload.get("results", []):
            display_name = item.get("display_name")
            if canonicalize_allowed_venue(display_name) != venue:
                continue

            source_id = item.get("id")
            if source_id:
                source_ids.add(source_id)

    return sorted(source_ids)


def infer_arxiv_venue_from_comment(comment: str | None) -> str | None:
    normalized = normalize_spaces(comment or "")
    if not normalized:
        return None

    patterns = [
        r"accepted\s+to\s+([^.;]+)",
        r"to\s+appear\s+in\s+([^.;]+)",
        r"published\s+in\s+([^.;]+)",
        r"in\s+proceedings\s+of\s+([^.;]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            venue = normalize_spaces(match.group(1))
            if venue:
                return venue
    return None


def choose_openalex_venue(item: dict[str, Any]) -> str:
    def source_name(location: dict[str, Any]) -> str:
        source = location.get("source") or {}
        return normalize_spaces(source.get("display_name") or "")

    def is_preprint(name: str) -> bool:
        lowered = name.lower()
        return "arxiv" in lowered or "preprint" in lowered

    candidates: list[str] = []

    primary_location = item.get("primary_location") or {}
    primary_name = source_name(primary_location)
    if primary_name:
        candidates.append(primary_name)

    for location in item.get("locations", []) or []:
        name = source_name(location)
        if name:
            candidates.append(name)

    host_venue = item.get("host_venue") or {}
    host_name = normalize_spaces(host_venue.get("display_name") or "")
    if host_name:
        candidates.append(host_name)

    for venue in candidates:
        if not is_preprint(venue):
            return venue
    for venue in candidates:
        if venue:
            return venue
    return "OpenAlex Indexed Venue"


def infer_categories(text: str) -> list[str]:
    lowered = text.lower()
    matched = [
        name
        for name, patterns in CATEGORY_RULES.items()
        if any(re.search(pattern, lowered) for pattern in patterns)
    ]
    return matched or ["General LLM Efficiency"]


def is_target_llm_paper(text: str) -> bool:
    lowered = text.lower()
    has_llm = any(re.search(pattern, lowered) for pattern in LLM_PATTERNS)
    has_focus = any(re.search(pattern, lowered) for pattern in FOCUS_PATTERNS)
    return has_llm and has_focus


def split_sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    pieces = re.split(r"(?<=[.!?])\s+", normalized)
    return [p.strip() for p in pieces if p.strip()]


def pick_sentence(sentences: list[str], patterns: list[str], fallback_idx: int = 0) -> str:
    for sentence in sentences:
        lowered = sentence.lower()
        if any(re.search(pattern, lowered) for pattern in patterns):
            return sentence
    if sentences:
        return sentences[min(fallback_idx, len(sentences) - 1)]
    return "Not enough metadata available to infer this section yet."


def build_summary(paper: dict[str, Any]) -> dict[str, list[str]]:
    sentences = split_sentences(paper.get("abstract", ""))
    first_two = sentences[:2] if sentences else ["Abstract is unavailable."]

    motivation = pick_sentence(sentences, [r"motivat", r"challenge", r"problem"], fallback_idx=0)
    key_idea = pick_sentence(sentences, [r"we propose", r"we present", r"we introduce", r"method"], fallback_idx=1)
    exp = pick_sentence(
        sentences,
        [r"experiment", r"benchmark", r"result", r"outperform", r"improv", r"sota"],
        fallback_idx=2,
    )
    analysis = pick_sentence(sentences, [r"ablation", r"analysis", r"dataset", r"data"], fallback_idx=3)
    discussion = pick_sentence(sentences, [r"limitation", r"future", r"conclusion", r"discuss"], fallback_idx=4)

    significance = (
        f"This work contributes to {', '.join(paper['category'])} in {paper['year']} and is a candidate "
        "for practical LLM system optimization follow-up."
    )

    references = [
        f"Source metadata page: {paper['url']}",
    ]
    if paper.get("doi"):
        references.append(f"DOI: https://doi.org/{paper['doi']}")

    return {
        "Summary": [*first_two],
        "Motivation": [motivation],
        "Key Idea": [key_idea],
        "Experimental Results": [exp],
        "Data analysis": [analysis],
        "Discussion (e.g., future work)": [discussion],
        "Significance of this study": [significance],
        "Useful references to consider": references,
    }


def decode_openalex_abstract(inverted_index: dict[str, list[int]] | None) -> str:
    if not inverted_index:
        return ""
    max_pos = -1
    for positions in inverted_index.values():
        if positions:
            max_pos = max(max_pos, max(positions))
    if max_pos < 0:
        return ""

    tokens = [""] * (max_pos + 1)
    for token, positions in inverted_index.items():
        for pos in positions:
            if 0 <= pos < len(tokens):
                tokens[pos] = token
    return " ".join(token for token in tokens if token)


def fetch_openalex() -> list[dict[str, Any]]:
    source_ids = fetch_openalex_source_ids()
    if not source_ids:
        print("Warning: OpenAlex source IDs for ICML/NeurIPS/ICLR were not resolved. Skipping OpenAlex fetch.")
        return []

    search_query = (
        '("large language model" OR LLM) '
        'AND ("kv cache" OR MoE OR quantization OR efficient OR "long context" OR inference)'
    )
    params = {
        "search": search_query,
        "per-page": str(MAX_RESULTS),
        "sort": "publication_date:desc",
        "filter": (
            f"from_publication_date:{MIN_PUBLICATION_DATE},"
            f"primary_location.source.id:{'|'.join(source_ids)}"
        ),
        "mailto": "maintainer@example.com",
    }

    payload = request_json("https://api.openalex.org/works", params=params)
    papers: list[dict[str, Any]] = []

    for item in payload.get("results", []):
        title = (item.get("display_name") or "").strip()
        if not title:
            continue

        authors = []
        affiliations = set()
        for authorship in item.get("authorships", []):
            author_name = (authorship.get("author") or {}).get("display_name")
            if author_name:
                authors.append(author_name)
            for institution in authorship.get("institutions", []):
                name = institution.get("display_name")
                if name:
                    affiliations.add(name)

        location = item.get("primary_location") or {}
        venue = canonicalize_allowed_venue(choose_openalex_venue(item))
        if not venue:
            continue
        publication_year = int(item.get("publication_year") or 0)
        publication_date = item.get("publication_date") or ""

        abstract = decode_openalex_abstract(item.get("abstract_inverted_index"))
        joined_text = f"{title} {abstract}"
        if not is_target_llm_paper(joined_text):
            continue
        categories = infer_categories(joined_text)

        record = {
            "paper_id": f"openalex-{item.get('id', '').split('/')[-1]}",
            "source": "OpenAlex",
            "title": title,
            "authors": authors,
            "affiliations": clean_affiliations(sorted(affiliations)),
            "venue": venue,
            "year": publication_year,
            "published_date": publication_date,
            "category": categories,
            "abstract": abstract,
            "url": item.get("id") or location.get("landing_page_url") or "",
            "doi": item.get("doi"),
        }
        record["summary"] = build_summary(record)
        papers.append(record)

    return papers


def fetch_arxiv() -> list[dict[str, Any]]:
    joined_query = (
        '(all:"large language model" OR all:LLM) '
        'AND (all:"KV Cache" OR all:MoE OR all:Efficient OR all:Quantization OR all:"Long Context" OR all:Inference)'
    )
    encoded_query = quote_plus(joined_query)
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query={encoded_query}&start=0&max_results={MAX_RESULTS}&sortBy=submittedDate&sortOrder=descending"
    )

    feed = feedparser.parse(request_text(url))
    papers: list[dict[str, Any]] = []
    min_pub_date = datetime.strptime(MIN_PUBLICATION_DATE, "%Y-%m-%d").date()

    for entry in feed.entries:
        title = re.sub(r"\s+", " ", entry.title).strip()
        abstract = re.sub(r"\s+", " ", entry.summary).strip()
        published = getattr(entry, "published", "")

        year = 0
        published_date = ""
        if published:
            try:
                published_date = published[:10]
                parsed_date = datetime.strptime(published_date, "%Y-%m-%d").date()
                if parsed_date < min_pub_date:
                    continue
                year = parsed_date.year
            except ValueError:
                year = 0

        authors = [author.name for author in getattr(entry, "authors", []) if getattr(author, "name", None)]
        joined_text = f"{title} {abstract}"
        if not is_target_llm_paper(joined_text):
            continue
        categories = infer_categories(joined_text)
        arxiv_id = entry.id.rsplit("/", 1)[-1]
        journal_ref = getattr(entry, "arxiv_journal_ref", None) or getattr(entry, "journal_ref", None)
        comment = getattr(entry, "arxiv_comment", None) or getattr(entry, "comment", None)
        venue = infer_arxiv_venue_from_comment(comment) or infer_arxiv_venue(journal_ref)
        venue = canonicalize_allowed_venue(venue)
        if not venue:
            continue
        doi = getattr(entry, "arxiv_doi", None)

        record = {
            "paper_id": f"arxiv-{arxiv_id}",
            "source": "arXiv",
            "title": title,
            "authors": authors,
            "affiliations": [],
            "venue": venue,
            "year": year,
            "published_date": published_date,
            "category": categories,
            "abstract": abstract,
            "url": entry.link,
            "doi": doi,
        }
        record["summary"] = build_summary(record)
        papers.append(record)

    return papers


def merge_papers(openalex_papers: list[dict[str, Any]], arxiv_papers: list[dict[str, Any]]) -> list[Paper]:
    title_to_affiliations: dict[str, list[str]] = {}
    for paper in openalex_papers:
        norm = normalize_title(paper["title"])
        if paper["affiliations"]:
            title_to_affiliations[norm] = paper["affiliations"]

    merged: dict[str, dict[str, Any]] = {}

    for source_papers in (openalex_papers, arxiv_papers):
        for paper in source_papers:
            # Final safety gate: never include papers matched only by non-LLM keywords.
            if not is_target_llm_paper(f"{paper.get('title', '')} {paper.get('abstract', '')}"):
                continue

            norm = normalize_title(paper["title"])
            if not norm:
                continue

            if norm in merged:
                existing = merged[norm]
                existing["authors"] = sorted(set(existing["authors"]) | set(paper["authors"]))
                merged_affiliations = sorted(set(existing["affiliations"]) | set(paper["affiliations"]))
                existing["affiliations"] = clean_affiliations(merged_affiliations)
                existing["category"] = sorted(set(existing["category"]) | set(paper["category"]))
                if existing["venue"] in {"Preprint (arXiv)", "arXiv"} and paper["venue"] not in {"Preprint (arXiv)", "arXiv"}:
                    existing["venue"] = paper["venue"]
                if not existing.get("doi") and paper.get("doi"):
                    existing["doi"] = paper["doi"]
                continue

            copied = dict(paper)
            if not copied["affiliations"] and norm in title_to_affiliations:
                copied["affiliations"] = title_to_affiliations[norm]
            copied["affiliations"] = clean_affiliations(copied["affiliations"])
            copied["summary"] = build_summary(copied)
            merged[norm] = copied

    papers = [Paper(**record) for record in merged.values()]
    papers.sort(key=lambda p: (p.published_date, p.year, p.title), reverse=True)
    return papers


def render_site(papers: list[Paper]) -> None:
    if SITE_DIR.exists():
        shutil.rmtree(SITE_DIR)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(STATIC_DIR / "style.css", SITE_DIR / "style.css")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    index_tpl = env.get_template("index.html.j2")
    paper_tpl = env.get_template("paper.html.j2")

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    indexed_rows: list[dict[str, Any]] = []
    for paper in papers:
        filename = f"{slugify(paper.title)}-{paper.paper_id}.html"
        indexed_rows.append({"paper": paper, "page_file": f"papers/{filename}"})

    index_html = index_tpl.render(
        rows=indexed_rows,
        keywords=KEYWORDS,
        generated_at=generated_at,
    )
    (SITE_DIR / "index.html").write_text(index_html, encoding="utf-8")

    for row in indexed_rows:
        paper = row["paper"]
        filename = Path(row["page_file"]).name
        paper_html = paper_tpl.render(paper=paper, generated_at=generated_at)
        (PAPER_DIR / filename).write_text(paper_html, encoding="utf-8")

    with (SITE_DIR / "papers.json").open("w", encoding="utf-8") as fp:
        json.dump([asdict(p) for p in papers], fp, ensure_ascii=False, indent=2)


def main() -> None:
    print("Fetching OpenAlex papers...")
    try:
        openalex_papers = fetch_openalex()
    except Exception as exc:
        print(f"Warning: OpenAlex fetch failed: {exc}")
        openalex_papers = []

    print("Fetching arXiv papers...")
    try:
        arxiv_papers = fetch_arxiv()
    except Exception as exc:
        print(f"Warning: arXiv fetch failed: {exc}")
        arxiv_papers = []

    papers = merge_papers(openalex_papers, arxiv_papers)
    print(f"Merged paper count: {len(papers)}")

    render_site(papers)
    print(f"Static site built at: {SITE_DIR}")


if __name__ == "__main__":
    main()
