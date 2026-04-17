#!/usr/bin/env python3
"""Fetch recent LLM-efficiency papers and build a static HTML tracker site."""

from __future__ import annotations

import json
import re
import shutil
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
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.text


def request_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    text = request_text(url, params=params)
    return json.loads(text)


def normalize_title(title: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", title.lower())
    return compact[:200]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:90] if slug else "paper"


def infer_categories(text: str) -> list[str]:
    lowered = text.lower()
    matched = [
        name
        for name, patterns in CATEGORY_RULES.items()
        if any(re.search(pattern, lowered) for pattern in patterns)
    ]
    return matched or ["General LLM Efficiency"]


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
    params = {
        "search": " ".join(KEYWORDS),
        "per-page": str(MAX_RESULTS),
        "sort": "publication_date:desc",
        "filter": f"from_publication_date:{MIN_PUBLICATION_DATE}",
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
        source = location.get("source") or {}
        venue = source.get("display_name") or "OpenAlex Indexed Venue"
        publication_year = int(item.get("publication_year") or 0)
        publication_date = item.get("publication_date") or ""

        abstract = decode_openalex_abstract(item.get("abstract_inverted_index"))
        joined_text = f"{title} {abstract}"
        categories = infer_categories(joined_text)

        record = {
            "paper_id": f"openalex-{item.get('id', '').split('/')[-1]}",
            "source": "OpenAlex",
            "title": title,
            "authors": authors,
            "affiliations": sorted(affiliations),
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
    joined_query = " OR ".join([f'all:"{kw}"' if " " in kw else f"all:{kw}" for kw in KEYWORDS])
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
        categories = infer_categories(f"{title} {abstract}")
        arxiv_id = entry.id.rsplit("/", 1)[-1]

        record = {
            "paper_id": f"arxiv-{arxiv_id}",
            "source": "arXiv",
            "title": title,
            "authors": authors,
            "affiliations": [],
            "venue": "arXiv",
            "year": year,
            "published_date": published_date,
            "category": categories,
            "abstract": abstract,
            "url": entry.link,
            "doi": None,
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
            norm = normalize_title(paper["title"])
            if not norm:
                continue

            if norm in merged:
                existing = merged[norm]
                existing["authors"] = sorted(set(existing["authors"]) | set(paper["authors"]))
                existing["affiliations"] = sorted(set(existing["affiliations"]) | set(paper["affiliations"]))
                existing["category"] = sorted(set(existing["category"]) | set(paper["category"]))
                if existing["venue"] == "arXiv" and paper["venue"] != "arXiv":
                    existing["venue"] = paper["venue"]
                if not existing.get("doi") and paper.get("doi"):
                    existing["doi"] = paper["doi"]
                continue

            copied = dict(paper)
            if not copied["affiliations"] and norm in title_to_affiliations:
                copied["affiliations"] = title_to_affiliations[norm]
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
    openalex_papers = fetch_openalex()

    print("Fetching arXiv papers...")
    arxiv_papers = fetch_arxiv()

    papers = merge_papers(openalex_papers, arxiv_papers)
    print(f"Merged paper count: {len(papers)}")

    render_site(papers)
    print(f"Static site built at: {SITE_DIR}")


if __name__ == "__main__":
    main()
