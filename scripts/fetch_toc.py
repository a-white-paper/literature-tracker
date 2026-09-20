"""Enrich papers.json with publisher-provided TOC or article graphics.

This script runs after fetch_papers.py. It does not decide which papers belong
in the tracker; it only adds visual metadata to papers that have already passed
the synthetic-organic filtering pipeline.

For each paper, the script visits the DOI/article landing page and looks for
image metadata in this order:

1. Explicit graphical-abstract / TOC metadata or images.
2. Structured article-image metadata (JSON-LD / itemprop=image).
3. Open Graph or Twitter article images as a lower-confidence fallback.

The script never fabricates a TOC graphic. When a publisher exposes no suitable
image, ``toc_url`` is left empty and the frontend displays an unobtrusive
"TOC not available" state instead.

No third-party Python packages are required.
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PAPERS_FILE = ROOT / "papers.json"

# A modest worker count keeps the enrichment step reasonably fast without
# sending an unnecessarily large burst of requests to publisher websites.
MAX_WORKERS = 5
REQUEST_TIMEOUT = 15

# Generic website assets should never be presented as a paper's TOC graphic.
REJECT_IMAGE_TOKENS = (
    "favicon",
    "site-logo",
    "site_logo",
    "brand-logo",
    "brand_logo",
    "publisher-logo",
    "publisher_logo",
    "avatar",
    "sprite",
    "placeholder",
    "default-image",
    "default_image",
)

# Metadata keys are compared after lowercasing. Explicit graphical-abstract
# fields have the highest confidence and therefore the highest priority.
EXPLICIT_GRAPHIC_KEYS = (
    "citation_graphical_abstract",
    "graphical_abstract",
    "graphical-abstract",
    "toc_graphic",
    "toc-graphic",
    "abstract_graphic",
    "abstract-graphic",
)


class ArticleImageParser(HTMLParser):
    """Collect image candidates from article-page HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.candidates = []
        self._json_ld_depth = 0
        self._json_ld_chunks = []

    def handle_starttag(self, tag, attrs):
        attrs = {str(key).lower(): value for key, value in attrs if key}
        tag = tag.lower()

        if tag == "meta":
            key = (
                attrs.get("name")
                or attrs.get("property")
                or attrs.get("itemprop")
                or ""
            ).lower()
            value = attrs.get("content") or ""

            if value:
                if any(token in key for token in EXPLICIT_GRAPHIC_KEYS):
                    self.candidates.append((100, "graphical abstract", value))
                elif key in {"image", "thumbnailurl", "thumbnail_url"}:
                    self.candidates.append((70, "article image", value))
                elif key in {"og:image", "og:image:url", "og:image:secure_url"}:
                    self.candidates.append((55, "article image", value))
                elif key in {"twitter:image", "twitter:image:src"}:
                    self.candidates.append((45, "article image", value))

        elif tag == "img":
            alt = (attrs.get("alt") or "").lower()
            src = (
                attrs.get("src")
                or attrs.get("data-src")
                or attrs.get("data-original")
                or ""
            )
            if src and (
                "graphical abstract" in alt
                or "graphic abstract" in alt
                or "table of contents" in alt
                or "toc graphic" in alt
            ):
                self.candidates.append((95, "graphical abstract", src))

        elif tag == "script":
            script_type = (attrs.get("type") or "").lower()
            if "ld+json" in script_type:
                self._json_ld_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._json_ld_depth:
            self._json_ld_depth -= 1

    def handle_data(self, data):
        if self._json_ld_depth and data.strip():
            self._json_ld_chunks.append(data)

    def add_json_ld_candidates(self):
        """Parse JSON-LD blocks and add any declared article images."""
        for chunk in self._json_ld_chunks:
            try:
                payload = json.loads(chunk)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

            for image in iter_json_images(payload):
                self.candidates.append((65, "article image", image))


def iter_json_images(value):
    """Yield image URLs found recursively in JSON-LD structures."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"image", "thumbnailurl"}:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, list):
                    for part in item:
                        if isinstance(part, str):
                            yield part
                        elif isinstance(part, dict) and isinstance(part.get("url"), str):
                            yield part["url"]
                elif isinstance(item, dict) and isinstance(item.get("url"), str):
                    yield item["url"]
            else:
                yield from iter_json_images(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_images(item)


def is_suitable_image(url):
    """Return True for plausible public article/TOC image URLs."""
    if not url:
        return False

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    lowered = url.lower()
    return not any(token in lowered for token in REJECT_IMAGE_TOKENS)


def article_url(paper):
    """Return the best page to inspect for one normalized paper."""
    if paper.get("doi"):
        doi = str(paper["doi"]).replace("https://doi.org/", "")
        return f"https://doi.org/{doi}"
    return paper.get("url") or ""


def extract_toc_for_paper(paper):
    """Return ``(toc_url, toc_kind)`` for one paper, or empty strings."""
    page_url = article_url(paper)
    if not page_url:
        return "", ""

    request = Request(
        page_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; LiteratureTracker/1.0; "
                "+https://github.com/a-white-paper/literature-tracker)"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type.lower():
                return "", ""
            html = response.read(2_500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        print(f"TOC unavailable: {paper.get('title', 'Untitled')} — {exc}")
        return "", ""

    parser = ArticleImageParser()
    try:
        parser.feed(html)
        parser.add_json_ld_candidates()
    except Exception as exc:
        print(f"TOC parse warning: {paper.get('title', 'Untitled')} — {exc}")

    # Resolve relative image paths against the publisher page and then choose
    # the highest-confidence candidate. Duplicate URLs are collapsed first.
    best_by_url = {}
    for priority, kind, raw_url in parser.candidates:
        resolved = urljoin(final_url, raw_url.strip())
        if not is_suitable_image(resolved):
            continue

        previous = best_by_url.get(resolved)
        if previous is None or priority > previous[0]:
            best_by_url[resolved] = (priority, kind)

    if not best_by_url:
        return "", ""

    toc_url, (priority, toc_kind) = max(
        best_by_url.items(),
        key=lambda item: item[1][0],
    )
    return toc_url, toc_kind


def enrich_one(index, paper):
    """Enrich one paper while preserving list order for final serialization."""
    toc_url, toc_kind = extract_toc_for_paper(paper)
    enriched = dict(paper)
    enriched["toc_url"] = toc_url
    enriched["toc_kind"] = toc_kind
    return index, enriched


def main():
    """Load papers.json, enrich its records concurrently, and save it back."""
    payload = json.loads(PAPERS_FILE.read_text(encoding="utf-8"))
    papers = payload.get("papers") or []

    enriched = [None] * len(papers)
    found_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(enrich_one, index, paper)
            for index, paper in enumerate(papers)
        ]

        for future in as_completed(futures):
            index, paper = future.result()
            enriched[index] = paper
            if paper.get("toc_url"):
                found_count += 1
                print(
                    f"TOC found ({paper.get('toc_kind')}): "
                    f"{paper.get('title', 'Untitled')}"
                )

    payload["papers"] = enriched
    PAPERS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"TOC enrichment complete: {found_count}/{len(papers)} papers "
        "have publisher-provided visual metadata."
    )


if __name__ == "__main__":
    main()
