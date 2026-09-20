"""Validate TOC metadata before papers.json is published.

The enrichment stage deliberately searches several metadata sources because
publisher pages are often difficult to access from GitHub Actions. That broad
search can expose generic social-preview images (for example PubMed/PMC/NLM
branding) that are not a paper's graphical abstract.

This validator is therefore intentionally conservative: only images explicitly
classified as a graphical abstract / TOC graphic are allowed to reach the
website. Generic article images, social-sharing images, repository branding,
and other lower-confidence fallbacks are cleared.

The policy favors correctness over visual coverage. A card with no verified TOC
shows the normal "TOC unavailable" state instead of displaying a misleading
image.
"""

import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
PAPERS_FILE = ROOT / "papers.json"

# These are the only image classes that the website should call a TOC.
ALLOWED_TOC_KINDS = {
    "graphical abstract",
    "toc graphic",
    "toc image",
}

# Even if upstream metadata labels an image incorrectly, these hosts/tokens are
# strong signals that it is a repository/service graphic rather than the
# publisher-provided graphical abstract itself.
REJECT_URL_TOKENS = (
    "pubmed",
    "ncbi.nlm.nih.gov",
    "nih.gov",
    "nlm.nih.gov",
    "pmc.ncbi",
    "favicon",
    "logo",
    "placeholder",
    "default-image",
    "default_image",
)


def is_verified_toc(paper):
    """Return True only for high-confidence TOC / graphical-abstract metadata."""
    toc_url = str(paper.get("toc_url") or "").strip()
    toc_kind = str(paper.get("toc_kind") or "").strip().lower()

    if not toc_url or toc_kind not in ALLOWED_TOC_KINDS:
        return False

    parsed = urlparse(toc_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    lowered = toc_url.lower()
    if any(token in lowered for token in REJECT_URL_TOKENS):
        return False

    return True


def main():
    """Remove unverified visual metadata and rewrite papers.json in place."""
    payload = json.loads(PAPERS_FILE.read_text(encoding="utf-8"))
    papers = payload.get("papers") or []

    kept = 0
    removed = 0

    for paper in papers:
        if is_verified_toc(paper):
            kept += 1
            continue

        if paper.get("toc_url") or paper.get("toc_kind"):
            removed += 1
            print(
                "Rejected unverified TOC: "
                f"{paper.get('title', 'Untitled')} — "
                f"kind={paper.get('toc_kind') or 'unknown'}"
            )

        paper["toc_url"] = ""
        paper["toc_kind"] = ""

    PAPERS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"TOC validation complete: kept {kept} verified TOC graphics; "
        f"removed {removed} unverified images."
    )


if __name__ == "__main__":
    main()
