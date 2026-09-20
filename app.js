/*
 * Literature Tracker frontend controller.
 *
 * Responsibilities:
 * 1. Load literature data from papers.json.
 * 2. Render topic filters, statistics, and paper cards.
 * 3. Apply keyword search and sorting in the browser.
 * 4. Show bilingual title / corresponding-author affiliation metadata.
 * 5. Show verified publisher-provided TOC graphics when available.
 * 6. Keep all dynamic page updates in one place.
 *
 * The frontend deliberately does not fetch papers from external APIs directly.
 * External literature retrieval and enrichment are handled by the Python
 * scripts under scripts/ through GitHub Actions, which keep papers.json current.
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
 * bilingual/corresponding-author metadata and verified TOC metadata. The
 * no-store cache option helps ensure that visitors see the newest dataset.
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
 * Both English and Chinese title/affiliation metadata are searchable so users
 * can find a paper using either language.
 *
 * @returns {Array<Object>} A new filtered and sorted paper array.
 */
function filteredPapers() {
  const query = el("searchInput").value.trim().toLowerCase();
  const sort = el("sortSelect").value;

  const result = papers.filter(paper => {
    const topicMatch = activeTopic === "All" || (paper.tags || []).includes(activeTopic);

    const haystack = [
      paper.title,
      paper.title_zh,
      paper.journal,
      (paper.authors || []).join(" "),
      (paper.corresponding_authors || []).join(" "),
      (paper.corresponding_affiliations || []).join(" "),
      (paper.corresponding_affiliations_zh || []).join(" "),
      paper.abstract,
      (paper.tags || []).join(" ")
    ].join(" ").toLowerCase();

    return topicMatch && (!query || haystack.includes(query));
  });

  if (sort === "relevance-desc") {
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
 */
function relevanceLabel(score = 0) {
  if (score >= 80) return ["Highly relevant", "rel-high"];
  if (score >= 50) return ["Relevant", "rel-medium"];
  return ["Peripheral", "rel-low"];
}

/**
 * Escape text before inserting external metadata into HTML templates.
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
 * Build the English + Chinese metadata immediately below the English title.
 *
 * OpenAlex must explicitly mark an authorship as corresponding before its
 * institution is shown here. Missing metadata is reported rather than guessed.
 */
function bilingualMetadataBlock(paper) {
  const affiliations = paper.corresponding_affiliations || [];
  const affiliationsZh = paper.corresponding_affiliations_zh || [];
  const titleZh = (paper.title_zh || "").trim();

  const affiliationEn = affiliations.length
    ? affiliations.join("; ")
    : "Reliable corresponding-author affiliation not available from metadata";

  const affiliationZh = affiliationsZh.length
    ? affiliationsZh.join("；")
    : "暂无可靠的通讯作者单位元数据";

  return `
    <div class="corresponding-affiliation-en">
      <span class="metadata-label">Corresponding author affiliation:</span>
      ${escapeHTML(affiliationEn)}
    </div>
    <div class="bilingual-block">
      <div class="paper-title-zh">${escapeHTML(titleZh || "中文标题翻译暂不可用")}</div>
      <div class="corresponding-affiliation-zh">
        <span class="metadata-label-zh">通讯作者单位：</span>${escapeHTML(affiliationZh)}
      </div>
    </div>`;
}

/**
 * Build the verified TOC / graphical-abstract region shown on each card.
 */
function tocBlock(paper, targetUrl) {
  if (!paper.toc_url) {
    return `
      <div class="toc-unavailable" aria-label="TOC graphic unavailable">
        <span>TOC</span>
        <small>Verified TOC not available</small>
      </div>`;
  }

  return `
    <figure class="toc-figure">
      <a href="${escapeHTML(targetUrl)}" target="_blank" rel="noopener">
        <img
          class="toc-image"
          src="${escapeHTML(paper.toc_url)}"
          alt="TOC graphic for ${escapeHTML(paper.title || "this paper")}" 
          loading="lazy"
          decoding="async"
          referrerpolicy="no-referrer"
          onerror="this.closest('.toc-figure').classList.add('toc-load-error'); this.remove();"
        >
      </a>
      <figcaption>TOC / graphical abstract</figcaption>
    </figure>`;
}

/**
 * Build the HTML for one literature card.
 */
function paperCard(paper) {
  const [label, relevanceClass] = relevanceLabel(paper.relevance);

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
      ${bilingualMetadataBlock(paper)}
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

el("searchInput").addEventListener("input", render);
el("sortSelect").addEventListener("change", render);

loadPapers();
