"""Enrich accepted literature records with corresponding-author affiliations and Chinese text.

This script runs after ``fetch_papers.py``. It adds card-level metadata without
changing literature admission/filtering decisions.

Corresponding-author policy
---------------------------
1. Prefer OpenAlex authors explicitly marked ``is_corresponding``.
2. If none are marked, treat the final listed author as corresponding, following
   the repository owner's requested convention.
3. If that authorship has no paper-level affiliation, use a small curated
   correction table for known OpenAlex institution-resolution errors; otherwise
   query the author's OpenAlex profile for recent institutions.
4. Append the institution country to every structured affiliation whenever
   OpenAlex exposes a country code.

The provenance is stored in ``corresponding_source`` so explicit metadata and
fallbacks remain distinguishable in papers.json.

Translation policy
------------------
Translations are cached across workflow runs. Google Translate's public web
endpoint is tried first; MyMemory is retained as a fallback. Institution names
are translated separately from their country so the Chinese card always keeps
the requested ``机构，国家`` order instead of allowing a machine translator to
move the country before the institution name.
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
OPENALEX_WORK_BASE = "https://api.openalex.org/works/"
OPENALEX_AUTHOR_BASE = "https://api.openalex.org/authors/"
GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
TRANSLATION_VERSION = 2
USER_AGENT = (
    "Mozilla/5.0 (compatible; LiteratureTracker/1.0; "
    "+https://github.com/a-white-paper/literature-tracker)"
)

# OpenAlex exposes ISO-style country codes on institution objects. Keep a compact
# name table for the countries most likely to occur in synthetic-chemistry
# literature. Unknown codes are still shown verbatim rather than silently lost.
COUNTRY_NAMES = {
    "AT": "Austria",
    "AU": "Australia",
    "BE": "Belgium",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CN": "China",
    "CZ": "Czech Republic",
    "DE": "Germany",
    "DK": "Denmark",
    "ES": "Spain",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GR": "Greece",
    "HK": "Hong Kong",
    "HU": "Hungary",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IT": "Italy",
    "JP": "Japan",
    "KR": "South Korea",
    "MX": "Mexico",
    "NL": "Netherlands",
    "NO": "Norway",
    "NZ": "New Zealand",
    "PL": "Poland",
    "PT": "Portugal",
    "RO": "Romania",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "TR": "Türkiye",
    "TW": "Taiwan",
    "US": "United States",
    "ZA": "South Africa",
}

COUNTRY_NAMES_ZH = {
    "Austria": "奥地利",
    "Australia": "澳大利亚",
    "Belgium": "比利时",
    "Brazil": "巴西",
    "Canada": "加拿大",
    "Switzerland": "瑞士",
    "China": "中国",
    "Czech Republic": "捷克",
    "Germany": "德国",
    "Denmark": "丹麦",
    "Spain": "西班牙",
    "Finland": "芬兰",
    "France": "法国",
    "United Kingdom": "英国",
    "Greece": "希腊",
    "Hong Kong": "中国香港",
    "Hungary": "匈牙利",
    "Ireland": "爱尔兰",
    "Israel": "以色列",
    "India": "印度",
    "Italy": "意大利",
    "Japan": "日本",
    "South Korea": "韩国",
    "Mexico": "墨西哥",
    "Netherlands": "荷兰",
    "Norway": "挪威",
    "New Zealand": "新西兰",
    "Poland": "波兰",
    "Portugal": "葡萄牙",
    "Romania": "罗马尼亚",
    "Russia": "俄罗斯",
    "Saudi Arabia": "沙特阿拉伯",
    "Sweden": "瑞典",
    "Singapore": "新加坡",
    "Türkiye": "土耳其",
    "Taiwan": "中国台湾",
    "United States": "美国",
    "South Africa": "南非",
}

# Curated corrections are used only when a paper-level affiliation is missing
# and OpenAlex's author profile is known to conflate or mis-normalize institutions.
# Countries are included directly because these rows bypass the normal OpenAlex
# institution formatter.
AUTHOR_AFFILIATION_OVERRIDES = {
    "Kenichiro Itami": [
        "Molecule Creation Laboratory, RIKEN, Japan",
        "Institute of Transformative Bio-Molecules (WPI-ITbM), Nagoya University, Japan",
    ],
}


def normalized_doi(paper):
    """Return a bare DOI string from the website paper schema."""
    return str(paper.get("doi") or "").replace("https://doi.org/", "").strip()


def fetch_json(url, label):
    """Fetch JSON from one public metadata endpoint, returning {} on failure."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception as exc:
        print(f"{label} lookup failed: {exc}")
        return {}


def fetch_openalex_work(doi):
    """Fetch one complete OpenAlex work record by DOI."""
    if not doi:
        return {}
    identifier = urllib.parse.quote(f"https://doi.org/{doi}", safe=":/")
    return fetch_json(OPENALEX_WORK_BASE + identifier, f"OpenAlex work {doi}")


def fetch_openalex_author(author_id):
    """Fetch an OpenAlex author profile from a full or bare OpenAlex author id."""
    author_id = str(author_id or "").strip()
    if not author_id:
        return {}
    short_id = author_id.rsplit("/", 1)[-1]
    return fetch_json(OPENALEX_AUTHOR_BASE + short_id, f"OpenAlex author {short_id}")


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


def country_name(country_code):
    """Return a readable country name for one OpenAlex country code."""
    code = str(country_code or "").strip().upper()
    if not code:
        return ""
    return COUNTRY_NAMES.get(code, code)


def format_institution(institution):
    """Format one OpenAlex institution as ``Institution, Country``."""
    institution = institution or {}
    name = " ".join(str(institution.get("display_name") or "").split()).strip()
    country = country_name(institution.get("country_code"))
    if name and country:
        return f"{name}, {country}"
    return name


def authorship_name(authorship):
    """Return the normalized display name for one OpenAlex authorship."""
    return ((authorship.get("author") or {}).get("display_name") or "").strip()


def authorship_affiliations(authorship):
    """Return paper-level institutions with countries, then raw affiliations."""
    institution_names = [
        format_institution(institution)
        for institution in (authorship.get("institutions") or [])
    ]
    institution_names = dedupe_preserve_order(institution_names)
    if institution_names:
        return institution_names

    # Raw affiliation strings have no reliable machine-readable country field.
    # Preserve them as-is rather than guessing a country.
    return dedupe_preserve_order(authorship.get("raw_affiliation_strings") or [])


def author_profile_affiliations(authorship):
    """Return the most recent author-profile institutions with countries."""
    author = authorship.get("author") or {}
    profile = fetch_openalex_author(author.get("id"))
    if not profile:
        return []

    last_known = dedupe_preserve_order([
        format_institution(institution)
        for institution in (profile.get("last_known_institutions") or [])
    ])
    if last_known:
        return last_known

    affiliation_rows = profile.get("affiliations") or []
    latest_year = None
    collected = []
    for row in affiliation_rows:
        years = [year for year in (row.get("years") or []) if isinstance(year, int)]
        row_latest = max(years) if years else None
        institution_name = format_institution(row.get("institution") or {})
        if not institution_name:
            continue
        if row_latest is None:
            collected.append((None, institution_name))
        else:
            latest_year = row_latest if latest_year is None else max(latest_year, row_latest)
            collected.append((row_latest, institution_name))

    if latest_year is not None:
        return dedupe_preserve_order([name for year, name in collected if year == latest_year])
    return dedupe_preserve_order([name for _, name in collected])


def metadata_for_authorship(authorship, source_prefix):
    """Return name, affiliations, and provenance for one selected authorship."""
    name = authorship_name(authorship)
    affiliations = authorship_affiliations(authorship)
    if affiliations:
        return [name] if name else [], affiliations, source_prefix

    # Known OpenAlex profile-normalization errors are corrected before using
    # the broader author-profile fallback. This avoids displaying false units.
    curated = AUTHOR_AFFILIATION_OVERRIDES.get(name, [])
    if curated:
        return [name], curated, source_prefix + "_curated"

    profile_affiliations = author_profile_affiliations(authorship)
    if profile_affiliations:
        return [name] if name else [], profile_affiliations, source_prefix + "_author_profile"

    return [name] if name else [], [], source_prefix


def corresponding_metadata(work):
    """Return corresponding-author metadata with explicit and fallback layers."""
    authorships = work.get("authorships") or []
    explicit = [a for a in authorships if a.get("is_corresponding")]

    if explicit:
        names = []
        affiliations = []
        sources = []
        for authorship in explicit:
            row_names, row_affiliations, row_source = metadata_for_authorship(
                authorship,
                "openalex_explicit",
            )
            names.extend(row_names)
            affiliations.extend(row_affiliations)
            sources.append(row_source)
        if any(source.endswith("_curated") for source in sources):
            source = "openalex_explicit_curated"
        elif any(source.endswith("_author_profile") for source in sources):
            source = "openalex_explicit_author_profile"
        else:
            source = "openalex_explicit"
        return dedupe_preserve_order(names), dedupe_preserve_order(affiliations), source

    if authorships:
        return metadata_for_authorship(authorships[-1], "last_author_fallback")

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
        if paper.get("translation_version") != TRANSLATION_VERSION:
            continue
        title = str(paper.get("title") or "").strip()
        title_zh = str(paper.get("title_zh") or "").strip()
        if title and title_zh:
            cache[title] = title_zh
        for source, translated in zip(
            paper.get("corresponding_affiliations") or [],
            paper.get("corresponding_affiliations_zh") or [],
        ):
            source = str(source or "").strip()
            translated = str(translated or "").strip()
            if source and translated:
                cache[source] = translated
    return cache


def google_translate(text):
    """Translate one short English string with Google's public web endpoint."""
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text,
    })
    request = urllib.request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    segments = payload[0] if isinstance(payload, list) and payload else []
    return html.unescape("".join(
        str(segment[0])
        for segment in segments
        if isinstance(segment, list) and segment and segment[0]
    ).strip())


def mymemory_translate(text):
    """Translate one short English string using MyMemory as a fallback."""
    params = urllib.parse.urlencode({"q": text[:490], "langpair": "en|zh-CN"})
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

    for provider_name, provider in (("Google", google_translate), ("MyMemory", mymemory_translate)):
        try:
            translated = provider(text)
            if translated and translated.casefold() != text.casefold():
                cache[text] = translated
                time.sleep(0.12)
                return translated
        except Exception as exc:
            print(f"{provider_name} Chinese translation failed for {text[:80]!r}: {exc}")
    return ""


def translate_affiliation_to_chinese(affiliation, cache):
    """Translate an affiliation while enforcing ``机构，国家`` word order."""
    affiliation = " ".join(str(affiliation or "").split()).strip()
    if not affiliation:
        return ""

    # Match the final country suffix produced by format_institution(). Translate
    # only the institution portion, then append a controlled Chinese country
    # name. Sorting by length prevents shorter country names from matching first.
    for country_en in sorted(COUNTRY_NAMES_ZH, key=len, reverse=True):
        suffix = f", {country_en}"
        if affiliation.endswith(suffix):
            institution_en = affiliation[:-len(suffix)].strip()
            institution_zh = translate_to_chinese(institution_en, cache)
            country_zh = COUNTRY_NAMES_ZH[country_en]
            if institution_zh:
                return f"{institution_zh}，{country_zh}"
            return country_zh

    # Raw affiliation strings may not have structured country metadata.
    return translate_to_chinese(affiliation, cache)


def enrich_paper(paper, translation_cache):
    """Return one paper with corresponding-author and Chinese metadata added."""
    enriched = dict(paper)
    work = fetch_openalex_work(normalized_doi(paper))
    names, affiliations, source = corresponding_metadata(work)

    enriched["corresponding_authors"] = names
    enriched["corresponding_affiliations"] = affiliations
    enriched["corresponding_source"] = source
    enriched["title_zh"] = translate_to_chinese(paper.get("title") or "", translation_cache)
    enriched["corresponding_affiliations_zh"] = [
        translate_affiliation_to_chinese(affiliation, translation_cache)
        for affiliation in affiliations
    ]
    enriched["translation_version"] = TRANSLATION_VERSION

    print(
        f"Corresponding metadata: {paper.get('title', 'Untitled')} — "
        f"{', '.join(names) or 'unknown'} — "
        f"{', '.join(affiliations) or 'affiliation unavailable'} — source={source or 'none'}"
    )
    return enriched


def main():
    """Enrich every accepted paper and write the dataset back to disk."""
    payload = json.loads(PAPERS_FILE.read_text(encoding="utf-8"))
    cache = load_translation_cache()
    papers = payload.get("papers") or []

    payload["papers"] = [enrich_paper(paper, cache) for paper in papers]
    PAPERS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with_affiliations = sum(bool(p.get("corresponding_affiliations")) for p in payload["papers"])
    with_chinese_titles = sum(bool(p.get("title_zh")) for p in payload["papers"])
    fallback_count = sum(
        "fallback" in str(p.get("corresponding_source") or "")
        for p in payload["papers"]
    )
    print(
        f"Bilingual metadata enrichment complete: {with_affiliations}/{len(papers)} "
        f"papers have affiliations; {fallback_count} use a fallback path; "
        f"{with_chinese_titles}/{len(papers)} have Chinese titles."
    )


if __name__ == "__main__":
    main()
