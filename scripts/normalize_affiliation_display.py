"""Normalize Chinese affiliation display to the project-wide institution-country format.

This script is a final formatting guard for bilingual card metadata. The upstream
enrichment step already stores structured English affiliations as
``Institution, Country`` whenever OpenAlex supplies a country code. Historical
translation-cache entries, however, may contain Chinese strings such as
``中国宁波大学`` or ``美国莱斯大学``.

To keep every card consistent, this script uses the English country suffix as
the source of truth, removes any existing occurrence of that country name from
the Chinese institution text, and then appends the country exactly once at the
end as ``机构，国家``.

The script deliberately changes display formatting only. It does not alter the
identified corresponding author or the underlying English affiliation metadata.
"""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAPERS_FILE = ROOT / "papers.json"

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


def split_structured_affiliation(affiliation):
    """Return ``(institution, country_en)`` for a recognized country suffix."""
    affiliation = " ".join(str(affiliation or "").split()).strip()
    for country_en in sorted(COUNTRY_NAMES_ZH, key=len, reverse=True):
        suffix = f", {country_en}"
        if affiliation.endswith(suffix):
            return affiliation[:-len(suffix)].strip(), country_en
    return affiliation, ""


def clean_chinese_institution(text, country_zh):
    """Remove an already translated country from Chinese institution text."""
    text = " ".join(str(text or "").split()).strip(" ，,")
    if not text or not country_zh:
        return text

    # Old translation providers sometimes placed the country before the school
    # (e.g. ``美国莱斯大学``), after it without punctuation, or even inside a
    # longer institutional phrase. Remove it before applying the canonical
    # ``机构，国家`` layout below.
    text = text.replace(country_zh, "")
    return " ".join(text.split()).strip(" ，,")


def normalize_affiliation_pair(affiliation_en, affiliation_zh):
    """Return Chinese text in canonical ``机构，国家`` order when possible."""
    institution_en, country_en = split_structured_affiliation(affiliation_en)
    if not country_en:
        return str(affiliation_zh or "").strip()

    country_zh = COUNTRY_NAMES_ZH[country_en]
    institution_zh = clean_chinese_institution(affiliation_zh, country_zh)

    # If the upstream translator unexpectedly returned nothing, keep the English
    # institution name rather than showing a misleading country-only value.
    if not institution_zh:
        institution_zh = institution_en

    return f"{institution_zh}，{country_zh}"


def normalize_paper(paper):
    """Normalize all corresponding-author affiliation translations on one card."""
    affiliations_en = paper.get("corresponding_affiliations") or []
    affiliations_zh = paper.get("corresponding_affiliations_zh") or []

    normalized = []
    for index, affiliation_en in enumerate(affiliations_en):
        affiliation_zh = affiliations_zh[index] if index < len(affiliations_zh) else ""
        normalized.append(normalize_affiliation_pair(affiliation_en, affiliation_zh))

    updated = dict(paper)
    updated["corresponding_affiliations_zh"] = normalized
    return updated


def main():
    """Normalize all paper cards and write the corrected dataset back to disk."""
    payload = json.loads(PAPERS_FILE.read_text(encoding="utf-8"))
    papers = payload.get("papers") or []
    payload["papers"] = [normalize_paper(paper) for paper in papers]

    PAPERS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Normalized Chinese affiliation display for {len(papers)} paper cards.")


if __name__ == "__main__":
    main()
