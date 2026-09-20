"""Enrich papers.json with publisher-provided TOC or article graphics.

This script runs after fetch_papers.py. It does not decide which papers belong
in the tracker; it only adds visual metadata to papers that have already passed
the synthetic-organic filtering pipeline.

Image discovery is intentionally layered:

1. Reuse a previously resolved image for the same DOI/title when possible.
2. Ask OpenAlex for direct publisher / open-access landing pages and inspect
   those pages for explicit graphical-abstract / TOC metadata.
3. Fall back to structured article-image metadata such as JSON-LD / Open Graph.
4. If publishers block the GitHub Actions runner, use Microlink's public
   metadata endpoint to resolve the page's primary article image.

The script never fabricates a TOC graphic. Real graphical abstracts are labelled
as such. Lower-confidence page images are explicitly labelled "article image".
When no suitable image can be found, ``toc_url`` stays empty and the frontend
shows an unobtrusive unavailable state.

No third-party Python packages are required.
"""

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PAPERS_FILE = ROOT / "papers.json"

MAX_WORKERS = 4
REQUEST_TIMEOUT = 15
MICROLINK_MAX_REQUESTS = 25

USER_AGENT = (
    "Mozilla/5.0 (compatible; LiteratureTracker/1.0; "
    "+https://github.com/a-white-paper/literature-tracker)"
)

# Microlink's anonymous endpoint has a limited daily allowance. A lock keeps
# parallel workers from exceeding the configured per-run cap.
_microlink_lock = Lock()
_microlink_requests = 0

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
        """Parse JSON-LD blocks and add declared article images."""
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


def normalize_title(title):
    """Create a stable title key for matching today's papers to prior data."""
    return re.sub(r"[^a-z0-9]+", "", str(title or "").lower())


def paper_cache_keys(paper):
    """Return DOI/title cache keys for one paper."""
    keys = []
    doi = str(paper.get("doi") or "").replace("https://doi.org/", "").lower()
    if doi:
        keys.append(f"doi:{doi}")

    title_key = normalize_title(paper.get("title"))
    if title_key:
        keys.append(f"title:{title_key}")
    return keys


def load_previous_image_cache():
    """Load TOC metadata from the currently committed papers.json.

    fetch_papers.py overwrites the working-tree file before this script runs.
    ``git show HEAD:papers.json`` lets us recover the previously published data
    and avoid repeating remote image lookups for papers that remain in the feed.
    """
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
        toc_url = paper.get("toc_url") or ""
        if not toc_url:
            continue
        value = (toc_url, paper.get("toc_kind") or "article image")
        for key in paper_cache_keys(paper):
            cache[key] = value
    return cache


def is_suitable_image(url):
    """Return True for plausible public article/TOC image URLs."""
    if not url:
        return False

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False

    lowered = url.lower()
    return not any(token in lowered for token in REJECT_IMAGE_TOKENS)


def add_unique_url(urls, value):
    """Append a usable URL to a list once, preserving preference order."""
    if not value:
        return
    value = str(value).strip()
    if value.startswith("http") and value not in urls:
        urls.append(value)


def openalex_article_pages(paper):
    """Resolve direct publisher/repository landing pages through OpenAlex."""
    urls = []
    doi = str(paper.get("doi") or "").replace("https://doi.org/", "")

    if doi:
        identifier = quote(f"https://doi.org/{doi}", safe=":/")
        api_url = f"https://api.openalex.org/works/{identifier}"
        request = Request(
            api_url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                work = json.load(response)

            primary = work.get("primary_location") or {}
            best_oa = work.get("best_oa_location") or {}
            add_unique_url(urls, primary.get("landing_page_url"))
            add_unique_url(urls, best_oa.get("landing_page_url"))

            for location in work.get("locations") or []:
                add_unique_url(urls, (location or {}).get("landing_page_url"))
        except Exception as exc:
            print(f"OpenAlex landing-page lookup failed for {doi}: {exc}")

    stored_url = str(paper.get("url") or "")
    if stored_url and "doi.org/" not in stored_url:
        add_unique_url(urls, stored_url)

    if doi:
        add_unique_url(urls, f"https://doi.org/{doi}")

    return urls


def page_image_candidates(page_url):
    """Return ranked image candidates extracted from one article page."""
    request = Request(
        page_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )

    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        final_url = response.geturl()
        content_type = response.headers.get("Content-Type", "")
        if "html" not in content_type.lower():
            return []
        html = response.read(2_500_000).decode("utf-8", errors="replace")

    parser = ArticleImageParser()
    parser.feed(html)
    parser.add_json_ld_candidates()

    candidates = []
    for priority, kind, raw_url in parser.candidates:
        resolved = urljoin(final_url, raw_url.strip())
        if is_suitable_image(resolved):
            candidates.append((priority, kind, resolved))
    return candidates


def reserve_microlink_request():
    """Atomically reserve one fallback metadata request for this workflow run."""
    global _microlink_requests
    with _microlink_lock:
        if _microlink_requests >= MICROLINK_MAX_REQUESTS:
            return False
        _microlink_requests += 1
        return True


def microlink_article_image(page_url):
    """Resolve a page's primary image through Microlink as a final fallback."""
    if not reserve_microlink_request():
        return ""

    query = urlencode({"url": page_url})
    request = Request(
        f"https://api.microlink.io?{query}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )

    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.load(response)
    except Exception as exc:
        print(f"Microlink lookup failed for {page_url}: {exc}")
        return ""

    if payload.get("status") != "success":
        return ""

    data = payload.get("data") or {}
    image = data.get("image") or {}
    logo = data.get("logo") or {}
    image_url = image.get("url") or ""

    # Do not mistake the publisher/site logo for an article visual.
    if image_url and image_url == (logo.get("url") or ""):
        return ""

    width = image.get("width")
    height = image.get("height")
    if isinstance(width, (int, float)) and isinstance(height, (int, float)):
        if width < 180 or height < 120:
            return ""

    return image_url if is_suitable_image(image_url) else ""


def cached_image_for_paper(paper, cache):
    """Return a prior TOC result for this DOI/title when available."""
    for key in paper_cache_keys(paper):
        if key in cache:
            return cache[key]
    return "", ""


def extract_toc_for_paper(paper, cache):
    """Return ``(toc_url, toc_kind)`` for one paper, or empty strings."""
    cached_url, cached_kind = cached_image_for_paper(paper, cache)
    if cached_url:
        return cached_url, cached_kind

    pages = openalex_article_pages(paper)
    best_candidate = None
    errors = []

    for page_url in pages:
        try:
            candidates = page_image_candidates(page_url)
        except Exception as exc:
            errors.append(f"{urlparse(page_url).netloc}: {exc}")
            continue

        if not candidates:
            continue

        page_best = max(candidates, key=lambda item: item[0])
        if best_candidate is None or page_best[0] > best_candidate[0]:
            best_candidate = page_best

        if best_candidate[0] >= 95:
            break

    if best_candidate:
        _, toc_kind, toc_url = best_candidate
        return toc_url, toc_kind

    # Hosted CI runners are blocked by several publisher/DOI endpoints. Use the
    # metadata proxy only after direct extraction fails, and keep the fallback
    # clearly labelled as an article image rather than a verified TOC graphic.
    doi = str(paper.get("doi") or "").replace("https://doi.org/", "")
    fallback_page = pages[0] if pages else ""
    if doi:
        fallback_page = f"https://doi.org/{doi}"

    if fallback_page:
        fallback_image = microlink_article_image(fallback_page)
        if fallback_image:
            return fallback_image, "article image"

    if errors:
        print(
            f"TOC unavailable: {paper.get('title', 'Untitled')} — "
            + " | ".join(errors[:3])
        )
    return "", ""


def enrich_one(index, paper, cache):
    """Enrich one paper while preserving list order for final serialization."""
    toc_url, toc_kind = extract_toc_for_paper(paper, cache)
    enriched = dict(paper)
    enriched["toc_url"] = toc_url
    enriched["toc_kind"] = toc_kind
    return index, enriched


def main():
    """Load papers.json, enrich its records concurrently, and save it back."""
    payload = json.loads(PAPERS_FILE.read_text(encoding="utf-8"))
    papers = payload.get("papers") or []
    cache = load_previous_image_cache()

    enriched = [None] * len(papers)
    found_count = 0
    graphical_abstract_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(enrich_one, index, paper, cache)
            for index, paper in enumerate(papers)
        ]

        for future in as_completed(futures):
            index, paper = future.result()
            enriched[index] = paper
            if paper.get("toc_url"):
                found_count += 1
                if paper.get("toc_kind") == "graphical abstract":
                    graphical_abstract_count += 1
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
        f"TOC enrichment complete: {found_count}/{len(papers)} papers have a "
        f"visual ({graphical_abstract_count} verified graphical abstracts); "
        f"Microlink fallback requests used: {_microlink_requests}."
    )


if __name__ == "__main__":
    main()
