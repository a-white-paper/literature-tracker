"""Fetch and normalize recent chemistry literature from OpenAlex.

This script is the data-ingestion layer for the Literature Tracker website.
It performs four main tasks:

1. Run several topic-focused searches against the OpenAlex Works API.
2. Reconstruct abstracts from OpenAlex's inverted-index representation.
3. Assign topic tags and a simple relevance score to each paper.
4. Deduplicate and save the final records to papers.json for the frontend.

The script is designed to run automatically from GitHub Actions, but it can
also be executed locally with:

    python scripts/fetch_papers.py

No third-party Python packages are required; only the standard library is used.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Repository root and output location.
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "papers.json"

# Search phrases sent to OpenAlex.
#
# These phrases are intentionally broader than the frontend tags. Broad API
# searches improve recall, while KEYWORDS below provide a second-stage local
# classification step.
SEARCHES = {
    "Electrochemistry": "electrochemical organic synthesis",
    "Photocatalysis": "photocatalysis organic synthesis",
    "Fe Catalysis": "iron catalysis radical organic synthesis",
    "Cu Catalysis": "copper catalysis radical organic synthesis",
    "Skeletal Editing": "skeletal editing organic chemistry",
    "Boron Chemistry": "boron chemistry organic synthesis",
    "Difluoromethylation": "difluoromethylation",
    "Decarboxylative Coupling": "decarboxylative coupling radical",
}

# Keywords used for local topic tagging and relevance scoring.
# Matching is case-insensitive because the searchable text is normalized to
# lowercase before these terms are checked.
KEYWORDS = {
    "Electrochemistry": ["electrochem", "electrosynth", "electrolysis", "alternating current"],
    "Photocatalysis": ["photocatal", "photochemical", "visible light", "lmct"],
    "Fe Catalysis": ["iron catal", "fe catal", "iron-mediated"],
    "Cu Catalysis": ["copper catal", "cu catal", "copper-mediated"],
    "Skeletal Editing": ["skeletal editing", "atom insertion", "atom deletion", "scaffold editing"],
    "Boron Chemistry": ["boron", "boronic", "boryl", "borylation"],
    "Difluoromethylation": ["difluoromethyl", "cf2h"],
    "Decarboxylative Coupling": ["decarboxyl", "carboxylic acid", "redox-active ester"],
}

# A small journal bonus is used only as a secondary relevance signal.
# Topic matching remains the dominant part of the score.
PRIORITY_JOURNALS = {
    "Nature",
    "Science",
    "Nature Catalysis",
    "Nature Chemistry",
    "Journal of the American Chemical Society",
    "Angewandte Chemie International Edition",
}


def reconstruct_abstract(index):
    """Rebuild normal abstract text from an OpenAlex inverted index.

    OpenAlex may return abstracts as a mapping from each word to the positions
    where that word occurs. This function reverses that representation by
    collecting all ``(position, word)`` pairs, sorting by position, and joining
    the words in their original order.

    Args:
        index: OpenAlex ``abstract_inverted_index`` dictionary, or a falsey
            value when no abstract is available.

    Returns:
        The reconstructed abstract as a plain string. Returns an empty string
        when the source record has no abstract.
    """
    if not index:
        return ""

    words = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))

    words.sort()
    return " ".join(word for _, word in words)


def tag_and_score(work):
    """Assign topic tags and a simple 0-100 relevance score to one work.

    The current scoring system is intentionally transparent rather than
    machine-learned. Every paper starts with a baseline score of 20. Matching
    topic keywords add points, and selected high-impact chemistry journals add
    a small bonus. The result is capped at 100.

    Args:
        work: Raw OpenAlex work dictionary.

    Returns:
        A tuple ``(tags, score)`` where ``tags`` is a list of matched topic
        names and ``score`` is an integer from 0 to 100.
    """
    text = " ".join([
        work.get("title") or "",
        reconstruct_abstract(work.get("abstract_inverted_index")),
    ]).lower()

    tags = []
    score = 20

    for tag, keywords in KEYWORDS.items():
        # Count distinct configured keywords that occur at least once.
        hits = sum(1 for keyword in keywords if keyword in text)

        if hits:
            tags.append(tag)

            # Limit each topic's contribution so one dense keyword family does
            # not overwhelm the rest of the relevance score.
            score += min(18, hits * 8)

    journal = (
        ((work.get("primary_location") or {}).get("source") or {})
        .get("display_name")
        or ""
    )

    if journal in PRIORITY_JOURNALS:
        score += 8

    return tags, min(score, 100)


def fetch(query, from_date):
    """Fetch recent OpenAlex works for one search query.

    Args:
        query: Free-text query sent to OpenAlex.
        from_date: Earliest publication date in ISO ``YYYY-MM-DD`` format.

    Returns:
        A list of raw OpenAlex work dictionaries.

    Raises:
        urllib.error.URLError: If the network request cannot be completed.
        TimeoutError: If the request exceeds the configured timeout.
    """
    params = urllib.parse.urlencode({
        "search": query,
        "filter": f"from_publication_date:{from_date}",
        "sort": "publication_date:desc",
        "per-page": 25,
        "mailto": "literature-tracker@example.com",
    })

    url = f"https://api.openalex.org/works?{params}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "literature-tracker/1.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response).get("results", [])


def normalize(work):
    """Convert a raw OpenAlex work into the website's paper schema.

    Keeping normalization in one function makes papers.json stable even if the
    upstream OpenAlex response contains many fields that the website does not
    use.

    Args:
        work: Raw OpenAlex work dictionary.

    Returns:
        Dictionary containing the fields expected by app.js.
    """
    tags, relevance = tag_and_score(work)

    # Limit the visible author list to eight names so cards stay readable.
    authors = [
        item.get("author", {}).get("display_name", "")
        for item in work.get("authorships", [])[:8]
        if item.get("author", {}).get("display_name")
    ]

    location = work.get("primary_location") or {}
    source = location.get("source") or {}

    # Store a bare DOI in papers.json; app.js reconstructs the doi.org URL.
    doi = (work.get("doi") or "").replace("https://doi.org/", "")

    return {
        "title": work.get("title") or "Untitled",
        "authors": authors,
        "journal": source.get("display_name") or "Unknown journal",
        "date": work.get("publication_date") or "",
        "doi": doi,
        "url": (
            work.get("doi")
            or location.get("landing_page_url")
            or work.get("id")
            or ""
        ),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "tags": tags,
        "relevance": relevance,
    }


def main():
    """Run all configured searches, deduplicate papers, and write papers.json."""
    # The first version tracks papers published within the previous 30 days.
    from_date = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).date().isoformat()

    # Dictionary-based deduplication makes DOI/title lookup efficient and also
    # lets us keep the higher relevance score if the same work appears in more
    # than one topic search.
    by_key = {}

    for query in SEARCHES.values():
        try:
            for work in fetch(query, from_date):
                paper = normalize(work)

                # DOI is the preferred stable identifier. If no DOI exists,
                # normalized title text provides a reasonable fallback key.
                key = (
                    paper["doi"].lower()
                    if paper["doi"]
                    else paper["title"].strip().lower()
                )

                if (
                    key not in by_key
                    or paper["relevance"] > by_key[key]["relevance"]
                ):
                    by_key[key] = paper

        except Exception as exc:
            # One failed topic query should not prevent the remaining searches
            # from completing or stop the website dataset from being updated.
            print(f"Search failed for {query!r}: {exc}")

    # Newer papers appear first; relevance breaks ties on identical dates.
    papers = sorted(
        by_key.values(),
        key=lambda paper: (paper["date"], paper["relevance"]),
        reverse=True,
    )

    payload = {
        "updated": datetime.now(timezone.utc).date().isoformat(),
        "papers": papers,
    }

    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved {len(papers)} papers to {OUTPUT}")


if __name__ == "__main__":
    main()
