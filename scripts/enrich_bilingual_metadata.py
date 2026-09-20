"""Enrich accepted literature records with corresponding-author affiliations and Chinese text.

This script runs after ``fetch_papers.py``. It adds four card-level metadata
fields without changing literature admission/filtering decisions:

- ``corresponding_authors``: names explicitly marked as corresponding by OpenAlex.
- ``corresponding_affiliations``: English institution/affiliation strings for
  those corresponding authors.
- ``title_zh``: Simplified-Chinese translation of the English paper title.
- ``corresponding_affiliations_zh``: Chinese translations of the affiliation
  strings above.

Accuracy policy
---------------
The script never assumes that the last author is the corresponding author. If
OpenAlex does not expose an explicit ``is_corresponding`` flag, the affiliation
fields are left empty and the frontend reports that reliable metadata is not
available.

Translation policy
------------------
Previously translated strings are recovered from the committed ``papers.json``
and reused first. Only new strings are sent to the public MyMemory translation
endpoint. Translation failure is non-fatal: English metadata remains available
and the Chinese field is left empty rather than inventing a translation.

Only Python's standard library is required.
"""

import html
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPERS_FILE = ROOT / "papers.json"
OPENALEX_BASE = "https://api.openalex.org/works/"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
USER_AGENT = (
    "Mozilla/5.0 (compatible; LiteratureTracker/1.0; "
    "+https://github.com/a-white-paper/literature-tracker)"
)


def normalized_doi(paper):
    """Return a bare DOI string from the website paper schema."""
    return str(paper.get("doi") or "").replace("https://doi.org/", "").strip()


def fetch_openalex_work(doi):
    """Fetch one complete OpenAlex work record by DOI."""
    if not doi:
        return {}

    identifier = urllib.parse.quote(f"https://doi.org/{doi}", safe=":/")
    request = urllib.request.Request(
        OPENALEX_BASE + identifier,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception as exc:
        print(f"OpenAlex affiliation lookup failed for {doi}: {exc}")
        return {}


def dedupe_preserve_order(values):
    """Remove blank/duplicate strings while preserving source order."""
    seen = set()
    result = []
    for value in values:
        value = " ".join(str(value or "").split()).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def corresponding_metadata(work):
    """Extract only explicitly marked corresponding authors and affiliations."""
    names = []
    affiliations = []

    for authorship in work.get("authorships") or []:
        if not authorship.get("is_corresponding"):
            continue

        author_name = ((authorship.get("author") or {}).get("display_name") or "").strip()
        if author_name:
            names.append(author_name)

        institutions = authorship.get("institutions") or []
        institution_names = [
            (institution or {}).get("display_name") or ""
            for institution in institutions
        ]
        institution_names = dedupe_preserve_order(institution_names)

        if institution_names:
            affiliations.extend(institution_names)
        else:
            # Some OpenAlex records contain raw affiliation text even when the
            # normalized institution object is unavailable. This is still tied
            # to the explicitly corresponding authorship and is therefore safer
            # than guessing from author order.
            affiliations.extend(authorship.get("raw_affiliation_strings") or [])

    return dedupe_preserve_order(names), dedupe_preserve_order(affiliations)


def load_translation_cache():
    """Recover translations from the previously committed dataset."""
    try:
        raw = subprocess.check_output(
            ["git", "show", "HEAD:papers.json"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        previous = json.loads(raw)
    except Exception:
        return {}

    cache = {}
    for paper in previous.get("papers") or []:
        title = str(paper.get("title") or "").strip()
        title_zh = str(paper.get("title_zh") or "").strip()
        if title and title_zh:
            cache[title] = title_zh

        source_affiliations = paper.get("corresponding_affiliations") or []
        translated_affiliations = paper.get("corresponding_affiliations_zh") or []
        for source, translated in zip(source_affiliations, translated_affiliations):
            source = str(source or "").strip()
            translated = str(translated or "").strip()
            if source and translated:
                cache[source] = translated

    return cache


def translate_to_chinese(text, cache):
    """Translate one English string to Simplified Chinese with cache reuse."""
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return ""
    if text in cache:
        return cache[text]

    # MyMemory's anonymous endpoint accepts short scientific titles and
    # institution names without credentials. Each request is deliberately
    # throttled to remain polite to the public service.
    params = urllib.parse.urlencode({
        "q": text[:490],
        "langpair": "en|zh-CN",
    })
    request = urllib.request.Request(
        f"{MYMEMORY_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
        translated = html.unescape(
            str(((payload.get("responseData") or {}).get("translatedText") or "")).strip()
        )
        if translated and translated.casefold() != text.casefold():
            cache[text] = translated
            time.sleep(0.15)
            return translated
    except Exception as exc:
        print(f"Chinese translation failed for {text[:80]!r}: {exc}")

    return ""


def enrich_paper(paper, translation_cache):
    """Return one paper with corresponding-author and Chinese metadata added."""
    enriched = dict(paper)
    doi = normalized_doi(paper)
    work = fetch_openalex_work(doi)
    names, affiliations = corresponding_metadata(work)

    enriched["corresponding_authors"] = names
    enriched["corresponding_affiliations"] = affiliations
    enriched["title_zh"] = translate_to_chinese(paper.get("title") or "", translation_cache)
    enriched["corresponding_affiliations_zh"] = [
        translate_to_chinese(affiliation, translation_cache)
        for affiliation in affiliations
    ]

    if names:
        print(
            f"Corresponding author metadata: {paper.get('title', 'Untitled')} — "
            f"{', '.join(names)}"
        )
    else:
        print(
            f"Corresponding author metadata unavailable: "
            f"{paper.get('title', 'Untitled')}"
        )

    return enriched


def main():
    """Enrich every accepted paper and write the dataset back to disk."""
    payload = json.loads(PAPERS_FILE.read_text(encoding="utf-8"))
    cache = load_translation_cache()
    papers = payload.get("papers") or []

    payload["papers"] = [enrich_paper(paper, cache) for paper in papers]
    PAPERS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with_affiliations = sum(
        bool(paper.get("corresponding_affiliations"))
        for paper in payload["papers"]
    )
    with_chinese_titles = sum(
        bool(paper.get("title_zh"))
        for paper in payload["papers"]
    )
    print(
        f"Bilingual metadata enrichment complete: {with_affiliations}/{len(papers)} "
        f"papers have explicit corresponding-author affiliations; "
        f"{with_chinese_titles}/{len(papers)} have Chinese titles."
    )


if __name__ == "__main__":
    main()
