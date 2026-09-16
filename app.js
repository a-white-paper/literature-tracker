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

let papers = [];
let activeTopic = "All";

const el = id => document.getElementById(id);

async function loadPapers() {
  try {
    const response = await fetch("papers.json", { cache: "no-store" });
    if (!response.ok) throw new Error("Failed to load papers.json");
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

function filteredPapers() {
  const query = el("searchInput").value.trim().toLowerCase();
  const sort = el("sortSelect").value;

  const result = papers.filter(paper => {
    const topicMatch = activeTopic === "All" || (paper.tags || []).includes(activeTopic);
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
    result.sort((a, b) => (b.relevance || 0) - (a.relevance || 0) || new Date(b.date) - new Date(a.date));
  } else if (sort === "journal") {
    result.sort((a, b) => (a.journal || "").localeCompare(b.journal || ""));
  } else {
    result.sort((a, b) => new Date(b.date) - new Date(a.date));
  }
  return result;
}

function relevanceLabel(score = 0) {
  if (score >= 80) return ["Highly relevant", "rel-high"];
  if (score >= 50) return ["Relevant", "rel-medium"];
  return ["Peripheral", "rel-low"];
}

function escapeHTML(value = "") {
  return String(value).replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  }[char]));
}

function paperCard(paper) {
  const [label, relevanceClass] = relevanceLabel(paper.relevance);
  const doiUrl = paper.doi ? `https://doi.org/${encodeURIComponent(paper.doi.replace(/^https?:\/\/doi\.org\//, ""))}` : paper.url || "#";
  const titleUrl = paper.url || doiUrl;
  const abstract = paper.abstract || "Abstract not available from the current metadata source.";

  return `
    <article class="paper-card">
      <div class="paper-top">
        <span class="journal">${escapeHTML(paper.journal || "Unknown journal")}</span>
        <span class="date">${escapeHTML(paper.date || "")}</span>
      </div>
      <h3 class="paper-title"><a href="${titleUrl}" target="_blank" rel="noopener">${escapeHTML(paper.title || "Untitled")}</a></h3>
      <div class="authors">${escapeHTML((paper.authors || []).join(", "))}</div>
      <p class="abstract">${escapeHTML(abstract.length > 320 ? abstract.slice(0, 320) + "…" : abstract)}</p>
      <div class="tags">${(paper.tags || []).map(tag => `<span class="tag">${escapeHTML(tag)}</span>`).join("")}</div>
      <div class="paper-footer">
        <span class="relevance ${relevanceClass}">${label} · ${paper.relevance || 0}</span>
        <a class="doi-link" href="${doiUrl}" target="_blank" rel="noopener">${paper.doi ? "DOI ↗" : "Paper ↗"}</a>
      </div>
    </article>`;
}

function renderStats() {
  const now = new Date();
  const oneWeekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
  el("totalCount").textContent = papers.length;
  el("weekCount").textContent = papers.filter(p => new Date(p.date) >= oneWeekAgo).length;
  el("highCount").textContent = papers.filter(p => (p.relevance || 0) >= 80).length;
}

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
