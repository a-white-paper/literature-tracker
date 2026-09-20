"""Enrich accepted literature records with corresponding-author affiliations and Chinese text.

This script runs after ``fetch_papers.py``. It adds card-level metadata without
changing literature admission/filtering decisions:

- ``corresponding_authors``: corresponding-author names.
- ``corresponding_affiliations``: English institution/affiliation strings for
  those authors.
- ``corresponding_source``: whether the corresponding author came from an
  explicit OpenAlex flag or from the configured last-author fallback.
- ``title_zh``: Simplified-Chinese translation of the English paper title.
- ``corresponding_affiliations_zh``: Chinese translations of the affiliation
  strings above.

Corresponding-author policy
---------------------------
OpenAlex's explicit ``is_corresponding`` flag is always preferred. If OpenAlex
does not mark any authorship as corresponding, this tracker follows the project
policy requested by the repository owner: the final listed author is treated as
the corresponding author and that authorship's institution/affiliation is used.
The provenance is preserved in ``corresponding_source`` so the inferred fallback
can still be distinguished from explicit metadata in the dataset.

Translation policy
------------------
Translations are cached across workflow runs. Google Translate's public web
endpoint is tried first because it handles scientific sentence structure and
institution names better than the previous fallback. MyMemory is retained as a
secondary fallback. If both fail, the Chinese field is left empty rather than
inventing text.

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
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
TRANSLATION_VERSION = 2
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


def authorship_name(authorship):
    """Return the normalized display name for one OpenAlex authorship."""
    return ((authorship.get("author") or {}).get("display_name") or "").strip()


def authorship_affiliations(authorship):
    """Return normalized institution names, falling back to raw affiliations."""
    institutions = authorship.get("institutions") or []
    institution_names = [
        (institution or {}).get("display_name") or ""
        for institution in institutions
    ]
    institution_names = dedupe_preserve_order(institution_names)
    if institution_names:
        return institution_names

    return dedupe_preserve_order(
        authorship.get("raw_affiliation_strings") or []
    )


def corresponding_metadata(work):
    """Return corresponding-author metadata using explicit flags, then fallback.

    Returns
    -------
    tuple[list[str], list[str], str]
        ``(names, affiliations, source)`` where source is ``openalex_explicit``
        or ``last_author_fallback``. Empty strings/lists are returned only when
        the work contains no usable authorship information at all.
    """
    authorships = work.get("authorships") or []
    explicit = [
        authorship
        for authorship in authorships
        if authorship.get("is_corresponding")
    ]

    if explicit:
        names = [authorship_name(authorship) for authorship in explicit]
        affiliations = []
        for authorship in explicit:
            affiliations.extend(authorship_affiliations(authorship))
        return (
            dedupe_preserve_order(names),
            dedupe_preserve_order(affiliations),
            "openalex_explicit",
        )

    # Project policy: when the source does not identify a corresponding author,
    # treat the final listed author as corresponding rather than leaving the
    # card blank. The source flag keeps this inference auditable in papers.json.
    if authorships:
        last_authorship = authorships[-1]
        name = authorship_name(last_authorship)
        affiliations = authorship_affiliations(last_authorship)
        return (
            [name] if name else [],
            affiliations,
            "last_author_fallback",
        )

    return [], [], ""


def load_translation_cache():
    """Recover translations generated by the current translation version."""
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
        # A version gate lets us deliberately refresh old translations after a
        # translation-provider/quality-policy change.
        if paper.get("translation_version") != TRANSLATION_VERSION:
            continue

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


def google_translate(text):
    """Translate one short English string with Google's public web endpoint."""
    params = urllib.parse.urlencode({
        "client": "gtx",
        "sl": "en",
        "tl": "zh-CN",
        "dt": "t",
        "q": text,
    })
    request = urllib.request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)

    segments = payload[0] if isinstance(payload, list) and payload else []
    translated = "".join(
        str(segment[0])
        for segment in segments
        if isinstance(segment, list) and segment and segment[0]
    ).strip()
    return html.unescape(translated)


def mymemory_translate(text):
    """Translate one short English string using MyMemory as a fallback."""
    params = urllib.parse.urlencode({
        "q": text[:490],
        "langpair": "en|zh-CN",
    })
    request = urllib.request.Request(
        f"{MYMEMORY_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    return html.unescape(
        str(((payload.get("responseData") or {}).get("translatedText") or "")).strip()
    )


def translate_to_chinese(text, cache):
    """Translate one English string to Simplified Chinese with cache reuse."""
    text = " ".join(str(text or "").split()).strip()
    if not text:
        return ""
    if text in cache:
        return cache[text]

    for provider_name, provider in (
        ("Google", google_translate),
        ("MyMemory", mymemory_translate),
    ):
        try:
            translated = provider(text)
            if translated and translated.casefold() != text.casefold():
                cache[text] = translated
                time.sleep(0.12)
                return translated
        except Exception as exc:
            print(
                f"{provider_name} Chinese translation failed for "
                f"{text[:80]!r}: {exc}"
            )

    return ""


def enrich_paper(paper, translation_cache):
    """Return one paper with corresponding-author and Chinese metadata added."""
    enriched = dict(paper)
    doi = normalized_doi(paper)
    work = fetch_openalex_work(doi)
    names, affiliations, source = corresponding_metadata(work)

    enriched["corresponding_authors"] = names
    enriched["corresponding_affiliations"] = affiliations
    enriched["corresponding_source"] = source
    enriched["title_zh"] = translate_to_chinese(
        paper.get("title") or "",
        translation_cache,
    )
    enriched["corresponding_affiliations_zh"] = [
        translate_to_chinese(affiliation, translation_cache)
        for affiliation in affiliations
    ]
    enriched["translation_version"] = TRANSLATION_VERSION

    if source == "openalex_explicit":
        print(
            f"Explicit corresponding author: {paper.get('title', 'Untitled')} — "
            f"{', '.join(names)}"
        )
    elif source == "last_author_fallback":
        print(
            f"Corresponding-author fallback (last author): "
            f"{paper.get('title', 'Untitled')} — {', '.join(names) or 'unknown'}"
        )
    else:
        print(
            f"Corresponding-author metadata unavailable: "
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

    explicit_count = sum(
        paper.get("corresponding_source") == "openalex_explicit"
        for paper in payload["papers"]
    )
    fallback_count = sum(
        paper.get("corresponding_source") == "last_author_fallback"
        for paper in payload["papers"]
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
        f"Bilingual metadata enrichment complete: {explicit_count} explicit + "
        f"{fallback_count} last-author fallback corresponding records; "
        f"{with_affiliations}/{len(papers)} papers have affiliations; "
        f"{with_chinese_titles}/{len(papers)} have Chinese titles; "
        f"translation version {TRANSLATION_VERSION}."
    )


if __name__ == "__main__":
    main()
