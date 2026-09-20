"""Fetch, filter, score, and normalize recent synthetic-organic literature.

This script is the data-ingestion layer for the Literature Tracker website.
It performs six main tasks:

1. Run several topic-focused searches against the OpenAlex Works API.
2. Reconstruct abstracts from OpenAlex's inverted-index representation.
3. Reject papers that are clearly dominated by materials, energy, sensing,
   degradation, or device-oriented research.
4. Require a positive synthetic-organic chemistry signal before admission.
5. Classify admitted papers into the tracked topics and assign a transparent
   relevance score.
6. Deduplicate records and save the final dataset to papers.json.

The filtering philosophy is intentionally conservative: for this tracker,
showing fewer papers is preferable to flooding the website with irrelevant
materials-science results.

The script runs automatically from GitHub Actions, but it can also be executed
locally with:

    python scripts/fetch_papers.py

No third-party Python packages are required; only the standard library is used.
"""

import json
import re
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
    "Difluoromethylation": "difluoromethylation organic synthesis",
    "Decarboxylative Coupling": "decarboxylative coupling organic synthesis",
}

TOPIC_KEYWORDS = {
    "Electrochemistry": [
        "electrochem", "electrosynth", "electrolysis", "electrooxid",
        "electroreduct", "alternating current",
    ],
    "Photocatalysis": [
        "photocatal", "photochemical", "visible light", "visible-light",
        "photoinduced", "photoredox", "lmct",
    ],
    "Fe Catalysis": [
        "iron catal", "iron-catal", "fe catal", "fe-catal", "iron-mediated",
    ],
    "Cu Catalysis": [
        "copper catal", "copper-catal", "cu catal", "cu-catal",
        "copper-mediated",
    ],
    "Skeletal Editing": [
        "skeletal editing", "skeletal edit", "atom insertion", "atom deletion",
        "scaffold editing", "scaffold edit", "ring expansion", "ring contraction",
    ],
    "Boron Chemistry": [
        "boronic", "boronate", "organoboron", "borylation", "boryl", "borane",
    ],
    "Difluoromethylation": ["difluoromethyl", "cf2h"],
    "Decarboxylative Coupling": [
        "decarboxyl", "redox-active ester", "carboxylic acid coupling",
    ],
}

SYNTHETIC_ORGANIC_SIGNALS = [
    "organic synthesis", "synthetic method", "synthetic methodology",
    "chemical synthesis", "cross-coupling", "cross coupling",
    "coupling reaction", "functionalization", "c-h functionalization",
    "c–h functionalization", "c−h functionalization", "alkylation",
    "arylation", "acylation", "amination", "amidation", "olefination",
    "difunctionalization", "cyclization", "annulation", "dearomatization",
    "rearrangement", "ring expansion", "ring contraction", "skeletal editing",
    "scaffold editing", "decarboxylative", "decarbonylative", "defluorinative",
    "borylation", "hydrofunctionalization", "carbofunctionalization",
    "radical addition", "radical coupling", "radical relay",
    "radical-polar crossover", "enantioselective", "enantioselectivity",
    "asymmetric catalysis", "stereoselective", "late-stage functionalization",
    "late stage functionalization", "substrate scope", "reaction scope",
    "synthetic utility", "total synthesis", "phosphonylation",
    "fluoroalkylation", "monofluoromethyl", "difluoromethyl",
    "trifluoromethylation", "c(sp3)", "c(sp³)", "c-c bond", "c–c bond",
    "c-n bond", "c–n bond", "c-o bond", "c–o bond",
]

STRONG_TOPIC_SIGNALS = [
    "difluoromethyl", "cf2h", "skeletal editing", "scaffold editing",
    "redox-active ester", "decarboxylative coupling",
    "decarboxylative cross-coupling", "electrosynthesis",
    "organic electrosynthesis", "photoredox", "organoboron",
]

HARD_EXCLUDE_SIGNALS = [
    "lithium-ion battery", "lithium ion battery", "sodium-ion battery",
    "sodium ion battery", "potassium-ion battery", "zinc-ion battery",
    "solid-state battery", "supercapacitor", "energy storage", "fuel cell",
    "photovoltaic", "solar cell", "perovskite solar",
    "hydrogen evolution reaction", "oxygen evolution reaction",
    "oxygen reduction reaction", "water splitting",
    "electrochemical water splitting", "photocatalytic degradation",
    "photodegradation", "wastewater treatment", "pollutant degradation",
    "dye degradation", "electrochemical sensor", "electrochemical sensing",
    "biosensor", "gas sensor", "corrosion protection", "corrosion inhibition",
    "coelectrolysis", "co-electrolysis", "electrocatalytic urea synthesis",
    "co2 electroreduction", "co2 reduction", "carbon dioxide reduction",
    "plastic upcycling", "upcycling of plastics", "mixed plastics",
    "plastic waste", "polymeric carbon nitride", "photoluminescence quantum yield",
    "circularly-polarized luminescence", "circularly polarized luminescence",
    "chiroptical properties", "oled", "organic light-emitting diode",
]

SOFT_MATERIALS_SIGNALS = [
    "electrode material", "electrocatalyst", "heterogeneous photocatalyst",
    "nanocomposite", "nanoparticle", "nanostructure", "graphene",
    "carbon nanotube", "metal-organic framework", "metal organic framework",
    "mof", "covalent organic framework", "cof", "porous material",
    "semiconductor", "thin film", "device performance", "electrode performance",
    "specific capacitance", "charge storage", "energy density", "power density",
    "photocurrent density", "band gap", "photoluminescence", "luminescence",
]

PRIORITY_JOURNALS = {
    "Nature", "Science", "Nature Catalysis", "Nature Chemistry",
    "Nature Synthesis", "Journal of the American Chemical Society",
    "Angewandte Chemie International Edition", "ACS Catalysis",
    "Chemical Science", "Organic Letters", "The Journal of Organic Chemistry",
    "Chem Catalysis", "CCS Chemistry", "Science Advances",
}

MIN_RELEVANCE_SCORE = 45


def reconstruct_abstract(index):
    """Rebuild normal abstract text from an OpenAlex inverted index."""
    if not index:
        return ""
    words = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    words.sort()
    return " ".join(word for _, word in words)


def searchable_text(work):
    """Return normalized title + abstract text used by all local filters."""
    return " ".join([
        work.get("title") or "",
        reconstruct_abstract(work.get("abstract_inverted_index")),
    ]).lower()


def matched_terms(text, terms):
    """Return configured terms that occur in normalized text."""
    return [term for term in terms if term in text]


def classify_topics(text):
    """Return tracked topic tags supported by the paper's title/abstract."""
    tags = []
    for tag, keywords in TOPIC_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            tags.append(tag)
    return tags


def evaluate_admission(work):
    """Decide whether a raw OpenAlex work belongs in this tracker."""
    text = searchable_text(work)
    title = (work.get("title") or "Untitled").strip()

    synthetic_hits = matched_terms(text, SYNTHETIC_ORGANIC_SIGNALS)
    strong_topic_hits = matched_terms(text, STRONG_TOPIC_SIGNALS)
    hard_exclude_hits = matched_terms(text, HARD_EXCLUDE_SIGNALS)
    soft_material_hits = matched_terms(text, SOFT_MATERIALS_SIGNALS)
    tags = classify_topics(text)

    metadata = {
        "text": text,
        "synthetic_hits": synthetic_hits,
        "strong_topic_hits": strong_topic_hits,
        "hard_exclude_hits": hard_exclude_hits,
        "soft_material_hits": soft_material_hits,
        "tags": tags,
    }

    if hard_exclude_hits and len(synthetic_hits) < 3:
        reason = "hard materials/energy signal: " + ", ".join(hard_exclude_hits[:4])
        return False, reason, metadata

    if len(soft_material_hits) >= 2 and len(synthetic_hits) < 2:
        reason = "materials-dominated signal: " + ", ".join(soft_material_hits[:4])
        return False, reason, metadata

    if not synthetic_hits and not strong_topic_hits:
        return False, "no synthetic-organic admission signal", metadata

    if not tags:
        return False, "no tracked-topic match", metadata

    return True, f"accepted: {title}", metadata


def score_admitted_work(work, metadata):
    """Assign a transparent 0-100 score to an already admitted paper."""
    synthetic_hits = metadata["synthetic_hits"]
    strong_topic_hits = metadata["strong_topic_hits"]
    soft_material_hits = metadata["soft_material_hits"]
    tags = metadata["tags"]

    score = 40
    score += min(24, max(0, len(synthetic_hits) - 1) * 6)
    score += min(16, len(strong_topic_hits) * 8)
    score += min(24, len(tags) * 8)
    score -= min(20, len(soft_material_hits) * 5)

    journal = (
        ((work.get("primary_location") or {}).get("source") or {})
        .get("display_name") or ""
    )
    if journal in PRIORITY_JOURNALS:
        score += 10

    return max(0, min(score, 100))


def fetch(query, from_date):
    """Fetch recent OpenAlex works for one search query."""
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


def normalize(work, metadata, relevance):
    """Convert an admitted OpenAlex work into the website's paper schema."""
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
        "tags": metadata["tags"],
        "relevance": relevance,
    }


def normalize_title(title):
    """Create a stable title key for cross-source/version deduplication."""
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def main():
    """Run searches, filter candidates, deduplicate papers, and write JSON."""
    from_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()

    accepted_by_id = {}
    id_by_title = {}
    id_by_doi = {}
    rejected_count = 0
    candidate_count = 0

    for topic, query in SEARCHES.items():
        try:
            works = fetch(query, from_date)
            print(f"Fetched {len(works)} candidates for {topic}: {query!r}")

            for work in works:
                candidate_count += 1
                accepted, reason, metadata = evaluate_admission(work)
                title = work.get("title") or "Untitled"

                if not accepted:
                    rejected_count += 1
                    print(f"Rejected: {title} — {reason}")
                    continue

                relevance = score_admitted_work(work, metadata)
                if relevance < MIN_RELEVANCE_SCORE:
                    rejected_count += 1
                    print(
                        f"Rejected: {title} — relevance {relevance} "
                        f"below threshold {MIN_RELEVANCE_SCORE}"
                    )
                    continue

                paper = normalize(work, metadata, relevance)
                doi_key = paper["doi"].strip().lower()
                title_key = normalize_title(paper["title"])

                existing_id = None
                if doi_key and doi_key in id_by_doi:
                    existing_id = id_by_doi[doi_key]
                elif title_key and title_key in id_by_title:
                    existing_id = id_by_title[title_key]

                if existing_id is not None:
                    existing = accepted_by_id[existing_id]
                    if paper["relevance"] > existing["relevance"]:
                        accepted_by_id[existing_id] = paper
                    continue

                record_id = len(accepted_by_id)
                accepted_by_id[record_id] = paper
                if doi_key:
                    id_by_doi[doi_key] = record_id
                if title_key:
                    id_by_title[title_key] = record_id

        except Exception as exc:
            print(f"Search failed for {query!r}: {exc}")

    papers = sorted(
        accepted_by_id.values(),
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

    print(
        f"Processed {candidate_count} candidates; rejected {rejected_count}; "
        f"saved {len(papers)} unique synthetic-organic papers to {OUTPUT}"
    )


if __name__ == "__main__":
    main()
