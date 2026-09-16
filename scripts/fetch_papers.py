import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "papers.json"

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


def reconstruct_abstract(index):
    if not index:
        return ""
    words = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    words.sort()
    return " ".join(word for _, word in words)


def tag_and_score(work):
    text = " ".join([
        work.get("title") or "",
        reconstruct_abstract(work.get("abstract_inverted_index")),
    ]).lower()
    tags = []
    score = 20
    for tag, keywords in KEYWORDS.items():
        hits = sum(1 for keyword in keywords if keyword in text)
        if hits:
            tags.append(tag)
            score += min(18, hits * 8)
    journal = ((work.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
    if journal in {"Nature", "Science", "Nature Catalysis", "Nature Chemistry", "Journal of the American Chemical Society", "Angewandte Chemie International Edition"}:
        score += 8
    return tags, min(score, 100)


def fetch(query, from_date):
    params = urllib.parse.urlencode({
        "search": query,
        "filter": f"from_publication_date:{from_date}",
        "sort": "publication_date:desc",
        "per-page": 25,
        "mailto": "literature-tracker@example.com",
    })
    url = f"https://api.openalex.org/works?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "literature-tracker/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response).get("results", [])


def normalize(work):
    tags, relevance = tag_and_score(work)
    authors = [
        item.get("author", {}).get("display_name", "")
        for item in work.get("authorships", [])[:8]
        if item.get("author", {}).get("display_name")
    ]
    location = work.get("primary_location") or {}
    source = location.get("source") or {}
    doi = (work.get("doi") or "").replace("https://doi.org/", "")
    return {
        "title": work.get("title") or "Untitled",
        "authors": authors,
        "journal": source.get("display_name") or "Unknown journal",
        "date": work.get("publication_date") or "",
        "doi": doi,
        "url": work.get("doi") or location.get("landing_page_url") or work.get("id") or "",
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "tags": tags,
        "relevance": relevance,
    }


def main():
    from_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    by_key = {}
    for query in SEARCHES.values():
        try:
            for work in fetch(query, from_date):
                paper = normalize(work)
                key = paper["doi"].lower() if paper["doi"] else paper["title"].strip().lower()
                if key not in by_key or paper["relevance"] > by_key[key]["relevance"]:
                    by_key[key] = paper
        except Exception as exc:
            print(f"Search failed for {query!r}: {exc}")

    papers = sorted(by_key.values(), key=lambda p: (p["date"], p["relevance"]), reverse=True)
    OUTPUT.write_text(
        json.dumps({"updated": datetime.now(timezone.utc).date().isoformat(), "papers": papers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Saved {len(papers)} papers to {OUTPUT}")


if __name__ == "__main__":
    main()
