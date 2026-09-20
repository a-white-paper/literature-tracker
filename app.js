/*
 * Literature Tracker frontend controller.
 *
 * Responsibilities:
 * 1. Load literature data from papers.json.
 * 2. Render topic filters, statistics, and paper cards.
 * 3. Apply keyword search and sorting in the browser.
 * 4. Show publisher-provided TOC / article graphics when available.
 * 5. Keep all dynamic page updates in one place.
 *
 * The frontend deliberately does not fetch papers from external APIs directly.
 * External literature retrieval is handled by scripts/fetch_papers.py and
 * scripts/fetch_toc.py through GitHub Actions, which keep papers.json current.
 */

// Topics shown as filter buttons above the paper list.
const TOPICS = [
  "All",
  "Electrochemistry",
  "Photocatalysis",
  "Fe Catalysis",
  "Cu Catalysis",
  "Skeletal Editing",
  "Boron Chemistry",
  "Difluoromethylation",
  "Decarboxylative Coupling"
];

// In-memory copy of the literature dataset loaded from papers.json.
let papers = [];

// The currently selected topic filter. "All" disables topic filtering.
let activeTopic = "All";

/**
 * Return a DOM element by its id.
 *
 * @param {string} id - Element id defined in index.html.
 * @returns {HTMLElement|null} The matching DOM element.
 */
const el = id => document.getElementById(id);

/**
 * Load the generated literature dataset and initialize the interface.
 *
 * papers.json is produced by scripts/fetch_papers.py and then enriched with
 * TOC/article-image metadata by scripts/fetch_toc.py. The no-store cache option
 * helps ensure that visitors see the newest dataset after an update.
 *
 * @returns {Promise<void>}
 */
async function loadPapers() {
  try {
    const response = await fetch("papers.json", { cache: "no-store" });

    if (!response.ok) {
      throw new Error("Failed to load papers.json");
    }

    const data = await response.json();
    papers = Array.isArray(data.papers) ? data.papers : [];
    el("lastUpdated").textContent = `Updated ${data.updated || "recently"}`;
  } catch (error) {
    console.error(error);
    papers = [];
    el("lastUpdated").textContent = "Data unavailable";
  }

  renderFilters();
  render();
}

/**
 * Render topic filter buttons and attach their click handlers.
 *
 * Re-rendering the buttons after a click keeps the selected button's
 * "active" class synchronized with activeTopic.
 */
function renderFilters() {
  const box = el("filters");

  box.innerHTML = TOPICS.map(topic =>
    `<button class="filter-btn ${topic === activeTopic ? "active" : ""}" data-topic="${topic}">${topic}</button>`
  ).join("");

  box.querySelectorAll(".filter-btn").forEach(button => {
    button.addEventListener("click", () => {
      activeTopic = button.dataset.topic;
      renderFilters();
      render();
    });
  });
}

/**
 * Apply the current topic filter, search text, and sort order.
 *
 * Search is intentionally broad: title, journal, authors, abstract, and tags
 * are combined into one lowercase text string so a single search box can
 * match any of those fields.
 *
 * @returns {Array<Object>} A new filtered and sorted paper array.
 */
function filteredPapers() {
  const query = el("searchInput").value.trim().toLowerCase();
  const sort = el("sortSelect").value;

  const result = papers.filter(paper => {
    const topicMatch = activeTopic === "All" || (paper.tags || []).includes(activeTopic);

    // Build one searchable string from all user-relevant metadata fields.
    const haystack = [
      paper.title,
      paper.journal,
      (paper.authors || []).join(" "),
      paper.abstract,
      (paper.tags || []).join(" ")
    ].join(" ").toLowerCase();

    return topicMatch && (!query || haystack.includes(query));
  });

  if (sort === "relevance-desc") {
    // Relevance is the primary key; publication date breaks equal-score ties.
    result.sort((a, b) =>
      (b.relevance || 0) - (a.relevance || 0) ||
      new Date(b.date) - new Date(a.date)
    );
  } else if (sort === "journal") {
    result.sort((a, b) => (a.journal || "").localeCompare(b.journal || ""));
  } else {
    result.sort((a, b) => new Date(b.date) - new Date(a.date));
  }

  return result;
}

/**
 * Convert a numeric relevance score into a human-readable label and CSS class.
 *
 * @param {number} score - Relevance score from 0 to 100.
 * @returns {[string, string]} Display label and corresponding CSS class.
 */
function relevanceLabel(score = 0) {
  if (score >= 80) return ["Highly relevant", "rel-high"];
  if (score >= 50) return ["Relevant", "rel-medium"];
  return ["Peripheral", "rel-low"];
}

/**
 * Escape text before inserting external metadata into HTML templates.
 *
 * OpenAlex titles, abstracts, journal names, author names, and publisher image
 * URLs are external data. Escaping reserved HTML characters prevents those
 * values from being interpreted as page markup.
 *
 * @param {unknown} value - Value to convert into safe display text.
 * @returns {string} HTML-safe string.
 */
function escapeHTML(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;"
  }[char]));
}

/**
 * Build the visual-summary region shown on every literature card.
 *
 * A real publisher-provided graphical abstract / TOC image is preferred. If
 * only article-level image metadata is available, it is labelled accordingly.
 * If neither exists, the card explicitly says the TOC is unavailable rather
 * than fabricating an image or showing a publisher logo.
 *
 * @param {Object} paper - Normalized paper record from papers.json.
 * @param {string} targetUrl - Article URL opened when the graphic is clicked.
 * @returns {string} HTML markup for the visual-summary region.
 */
function tocBlock(paper, targetUrl) {
  if (!paper.toc_url) {
    return `
      <div class="toc-unavailable" aria-label="TOC graphic unavailable">
        <span>TOC</span>
        <small>Not available from publisher metadata</small>
      </div>`;
  }

  const kind = paper.toc_kind === "graphical abstract"
    ? "TOC / graphical abstract"
    : "Article graphic";

  return `
    <figure class="toc-figure">
      <a href="${escapeHTML(targetUrl)}" target="_blank" rel="noopener">
        <img
          class="toc-image"
          src="${escapeHTML(paper.toc_url)}"
          alt="Visual summary for ${escapeHTML(paper.title || "this paper")}" 
          loading="lazy"
          decoding="async"
          referrerpolicy="no-referrer"
          onerror="this.closest('.toc-figure').classList.add('toc-load-error'); this.remove();"
        >
      </a>
      <figcaption>${kind}</figcaption>
    </figure>`;
}

/**
 * Build the HTML for one literature card.
 *
 * The DOI is preferred for the DOI button, while the paper title uses the
 * best available landing-page URL. Abstract text is truncated only for the
 * card preview; the original full abstract remains in papers.json.
 *
 * @param {Object} paper - Normalized paper record from papers.json.
 * @returns {string} HTML markup for one paper card.
 */
function paperCard(paper) {
  const [label, relevanceClass] = relevanceLabel(paper.relevance);

  // Normalize DOI values whether papers.json stores a bare DOI or doi.org URL.
  const doiUrl = paper.doi
    ? `https://doi.org/${encodeURIComponent(paper.doi.replace(/^https?:\/\/doi\.org\//, ""))}`
    : paper.url || "#";

  const titleUrl = paper.url || doiUrl;
  const abstract = paper.abstract || "Abstract not available from the current metadata source.";

  return `
    <article class="paper-card">
      <div class="paper-top">
        <span class="journal">${escapeHTML(paper.journal || "Unknown journal")}</span>
        <span class="date">${escapeHTML(paper.date || "")}</span>
      </div>
      <h3 class="paper-title">
        <a href="${escapeHTML(titleUrl)}" target="_blank" rel="noopener">${escapeHTML(paper.title || "Untitled")}</a>
      </h3>
      <div class="authors">${escapeHTML((paper.authors || []).join(", "))}</div>
      ${tocBlock(paper, titleUrl)}
      <p class="abstract">${escapeHTML(abstract.length > 320 ? abstract.slice(0, 320) + "…" : abstract)}</p>
      <div class="tags">
        ${(paper.tags || []).map(tag => `<span class="tag">${escapeHTML(tag)}</span>`).join("")}
      </div>
      <div class="paper-footer">
        <span class="relevance ${relevanceClass}">${label} · ${paper.relevance || 0}</span>
        <a class="doi-link" href="${escapeHTML(doiUrl)}" target="_blank" rel="noopener">${paper.doi ? "DOI ↗" : "Paper ↗"}</a>
      </div>
    </article>`;
}

/**
 * Update summary counters shown above the search controls.
 *
 * "This week" means papers dated within the previous seven days relative to
 * the visitor's current browser time. "Highly relevant" uses the same >=80
 * threshold as relevanceLabel().
 */
function renderStats() {
  const now = new Date();
  const oneWeekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);

  el("totalCount").textContent = papers.length;
  el("weekCount").textContent = papers.filter(p => new Date(p.date) >= oneWeekAgo).length;
  el("highCount").textContent = papers.filter(p => (p.relevance || 0) >= 80).length;
}

/**
 * Render the current filtered paper list and associated summary UI.
 */
function render() {
  const result = filteredPapers();

  el("resultCount").textContent = `${result.length} paper${result.length === 1 ? "" : "s"}`;
  el("paperGrid").innerHTML = result.map(paperCard).join("");
  el("emptyState").hidden = result.length > 0;

  renderStats();
}

// Re-render immediately whenever the user changes search text or sort order.
el("searchInput").addEventListener("input", render);
el("sortSelect").addEventListener("change", render);

// Initial application startup: load data first, then render the page.
loadPapers();
