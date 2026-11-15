/**************************************
 * ELEMENTS & STATE
 **************************************/
const els = {
  form: document.getElementById("searchForm"),
  input: document.getElementById("search"),
  clear: document.getElementById("searchClear"),
  language: document.getElementById("language"),
  suggestions: document.getElementById("suggestions"),
  result: document.getElementById("result"),
};

const state = {
  selected: null,        // { qid, label } picked from autocomplete
  suggestions: [],
  activeIndex: -1,
  searchRequestId: 0,
  report: null,          // last successful /validate response (+ duration)
  filters: { status: "all", category: "all", text: "" },
};

const QID_PATTERN = /^[Qq]\d+$/;

/**************************************
 * DISPLAY LANGUAGE
 **************************************/
const LANGUAGE_STORAGE_KEY = "displayLanguage";

/** Language currently chosen in the picker (the template guarantees a valid code). */
function currentLanguage() {
  return els.language?.value || "en";
}

/** Select a language if the picker offers it. Returns whether it applied. */
function setLanguage(code) {
  if (!els.language || !code) return false;
  if (![...els.language.options].some(option => option.value === code)) return false;
  els.language.value = code;
  syncLanguageLabel();
  return true;
}

/** Mirror the selected option into the visible pill text. */
function syncLanguageLabel() {
  const option = els.language?.options[els.language.selectedIndex];
  const name = option ? option.text : "English";
  const code = option ? option.value.toUpperCase() : "EN";
  document.querySelectorAll("[data-language-value]").forEach(el => { el.textContent = name; });
  document.querySelectorAll("[data-language-code]").forEach(el => { el.textContent = code; });
}

function rememberLanguage(code) {
  try { localStorage.setItem(LANGUAGE_STORAGE_KEY, code); } catch { /* storage unavailable */ }
}

function storedLanguage() {
  try { return localStorage.getItem(LANGUAGE_STORAGE_KEY); } catch { return null; }
}

function languageFromUrl() {
  return new URLSearchParams(window.location.search).get("lang");
}

/** Result URL that keeps the chosen language when it is not the default. */
function reportUrl(qid) {
  const params = new URLSearchParams({ item: qid });
  if (currentLanguage() !== "en") params.set("lang", currentLanguage());
  return `${window.location.pathname}?${params}`;
}

// Restore the language used last time (a ?lang= link still takes priority below).
setLanguage(storedLanguage());

// Shorter placeholder on small screens so it isn't cut off mid-word.
const PLACEHOLDERS = { wide: els.input.placeholder, narrow: "Name or QID" };
const phoneScreen = window.matchMedia("(max-width: 480px)");
function applyPlaceholder() {
  if (els.input.dataset.listening === "true") return;
  els.input.placeholder = phoneScreen.matches ? PLACEHOLDERS.narrow : PLACEHOLDERS.wide;
}
applyPlaceholder();
phoneScreen.addEventListener("change", applyPlaceholder);

/**************************************
 * AUTOCOMPLETE
 **************************************/
function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function closeSuggestions() {
  state.suggestions = [];
  state.activeIndex = -1;
  els.suggestions.innerHTML = "";
  els.suggestions.hidden = true;
  els.input.setAttribute("aria-expanded", "false");
  els.input.removeAttribute("aria-activedescendant");
}

function renderSuggestions() {
  if (!state.suggestions.length) {
    closeSuggestions();
    return;
  }

  els.suggestions.innerHTML = state.suggestions.map((item, i) => `
    <li id="suggestion-${i}" role="option" class="suggestion ${i === state.activeIndex ? "is-active" : ""}"
        aria-selected="${i === state.activeIndex}" data-index="${i}">
      <span class="suggestion__text">
        <span class="suggestion__label">${escapeHtml(item.label)}</span>
        ${item.description ? `<span class="suggestion__desc">${escapeHtml(item.description)}</span>` : ""}
      </span>
      <span class="qid-pill">${escapeHtml(item.id)}</span>
    </li>
  `).join("");

  els.suggestions.hidden = false;
  els.input.setAttribute("aria-expanded", "true");
  if (state.activeIndex >= 0) {
    els.input.setAttribute("aria-activedescendant", `suggestion-${state.activeIndex}`);
    document.getElementById(`suggestion-${state.activeIndex}`)?.scrollIntoView({ block: "nearest" });
  } else {
    els.input.removeAttribute("aria-activedescendant");
  }
}

const fetchSuggestions = debounce(async query => {
  const requestId = ++state.searchRequestId;
  const language = currentLanguage();
  try {
    const params = new URLSearchParams({ q: query, language });
    const response = await fetch(`/search?${params}`);
    if (!response.ok) throw new Error(`Search request failed (HTTP ${response.status})`);
    const data = await response.json();
    // Ignore responses that arrive after a newer query or language was picked.
    if (requestId !== state.searchRequestId || language !== currentLanguage()) return;
    state.suggestions = Array.isArray(data) ? data : [];
    state.activeIndex = -1;
    renderSuggestions();
  } catch {
    if (requestId === state.searchRequestId) closeSuggestions();
  }
}, 220);

function selectSuggestion(index) {
  const item = state.suggestions[index];
  if (!item) return;
  state.selected = { qid: item.id, label: item.label };
  els.input.value = item.label;
  closeSuggestions();
  validate(item.id, item.label);
}

els.input.addEventListener("input", () => {
  const query = els.input.value.trim();
  state.selected = null;
  els.clear.hidden = !els.input.value;

  if (query.length < 2) {
    state.searchRequestId++;
    closeSuggestions();
    return;
  }
  fetchSuggestions(query);
});

els.input.addEventListener("keydown", e => {
  const open = !els.suggestions.hidden && state.suggestions.length > 0;

  if (e.key === "ArrowDown" && open) {
    e.preventDefault();
    state.activeIndex = (state.activeIndex + 1) % state.suggestions.length;
    renderSuggestions();
  } else if (e.key === "ArrowUp" && open) {
    e.preventDefault();
    state.activeIndex = state.activeIndex <= 0 ? state.suggestions.length - 1 : state.activeIndex - 1;
    renderSuggestions();
  } else if (e.key === "Enter" && open && state.activeIndex >= 0) {
    e.preventDefault();
    selectSuggestion(state.activeIndex);
  } else if (e.key === "Escape") {
    closeSuggestions();
  }
});

els.suggestions.addEventListener("mousedown", e => {
  // mousedown (not click) so the input doesn't lose focus first
  const li = e.target.closest("[data-index]");
  if (!li) return;
  e.preventDefault();
  selectSuggestion(Number(li.dataset.index));
});

document.addEventListener("click", e => {
  if (!els.form.contains(e.target)) closeSuggestions();
});

els.clear.addEventListener("click", () => {
  els.input.value = "";
  els.clear.hidden = true;
  state.selected = null;
  closeSuggestions();
  els.input.focus();
});

els.form.addEventListener("submit", e => {
  e.preventDefault();
  const typed = els.input.value.trim();
  if (!typed) {
    els.input.focus();
    els.form.classList.add("is-invalid");
    setTimeout(() => els.form.classList.remove("is-invalid"), 600);
    return;
  }
  closeSuggestions();
  if (state.selected) {
    validate(state.selected.qid, state.selected.label);
  } else if (QID_PATTERN.test(typed)) {
    validate(typed.toUpperCase(), typed.toUpperCase());
  } else {
    validate(null, typed);
  }
});

els.language?.addEventListener("change", () => {
  const language = currentLanguage();
  rememberLanguage(language);
  syncLanguageLabel();

  // Keep a shared "?item=..." URL in sync with the chosen language.
  if (state.report && itemFromUrl()) {
    history.replaceState({ qid: state.report.qid }, "", reportUrl(state.report.qid));
  }

  // Refresh an open suggestion list in the new language.
  const query = els.input.value.trim();
  if (!els.suggestions.hidden && query.length >= 2) fetchSuggestions(query);

  // Labels in a visible report depend on the language, so check the item again.
  if (state.report) {
    const name = els.language.options[els.language.selectedIndex]?.text || language;
    toast(`Re-checking with labels in ${name}…`);
    validate(state.report.qid, state.report.label, { updateHistory: false });
  }
});

/**************************************
 * VALIDATION
 **************************************/
let elapsedTimer = null;

function showLoading(label) {
  document.body.classList.add("has-results");
  const start = Date.now();
  els.result.innerHTML = `
    <div class="loading" role="status">
      <div class="loading__head">
        <span class="spinner" aria-hidden="true"></span>
        <div>
          <p class="loading__title">Checking references for <strong>${escapeHtml(label)}</strong>&hellip;</p>
          <p class="loading__hint">
            <span id="elapsed">0s</span> elapsed &middot; items with many references can take up to a minute
          </p>
        </div>
      </div>
      <div class="skeleton" aria-hidden="true">
        <span class="skeleton__line w-40"></span>
        <span class="skeleton__line w-90"></span>
        <span class="skeleton__line w-75"></span>
        <span class="skeleton__line w-85"></span>
        <span class="skeleton__line w-60"></span>
      </div>
    </div>`;

  clearInterval(elapsedTimer);
  elapsedTimer = setInterval(() => {
    const el = document.getElementById("elapsed");
    if (el) el.textContent = `${Math.floor((Date.now() - start) / 1000)}s`;
  }, 1000);
}

function showNotice(kind, message, { retry } = {}) {
  const icon = kind === "error" ? "bi-exclamation-octagon" : "bi-info-circle";
  els.result.innerHTML = `
    <div class="notice notice--${kind}" role="${kind === "error" ? "alert" : "status"}">
      <i class="bi ${icon} notice__icon" aria-hidden="true"></i>
      <div class="notice__body">
        <p class="notice__message">${escapeHtml(message)}</p>
        <div class="notice__actions">
          ${retry ? `<button type="button" class="btn-ghost" data-action="retry"><i class="bi bi-arrow-clockwise"></i> Try again</button>` : ""}
          <button type="button" class="btn-ghost" data-action="new-search"><i class="bi bi-search"></i> New search</button>
        </div>
      </div>
    </div>`;
  if (retry) els.result.querySelector('[data-action="retry"]').addEventListener("click", retry);
}

async function validate(qid, label, { updateHistory = true } = {}) {
  tts.stop();
  showLoading(label || qid);
  const start = Date.now();

  try {
    const response = await fetch("/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ qid, query: qid ? "" : label, language: currentLanguage() }),
    });

    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const duration = Math.round((Date.now() - start) / 1000);

    if (data.error) {
      showNotice("error", data.error, { retry: () => validate(qid, label) });
      return;
    }

    if (updateHistory && data.qid) {
      const url = reportUrl(data.qid);
      history.pushState({ qid: data.qid, label: data.label }, "", url);
    }
    if (data.label) {
      document.title = `${data.label} – Wikidata Reference Validator`;
      els.input.value = data.label;
      els.clear.hidden = false;
      state.selected = { qid: data.qid, label: data.label };
    }

    if (data.message) {
      showNotice("info", data.message);
      return;
    }

    state.report = { ...data, duration };
    state.filters = { status: "all", category: "all", text: "" };
    saveReport(state.report);
    renderReport();
  } catch (err) {
    showNotice("error", err.message || String(err), { retry: () => validate(qid, label) });
  } finally {
    clearInterval(elapsedTimer);
  }
}

function saveReport(report) {
  const refs = report.references;
  try {
    localStorage.setItem("reportData", JSON.stringify({
      qid: report.qid,
      label: report.label,
      description: report.description,
      language: report.language || currentLanguage(),
      aliveCount: refs.filter(r => r.status === "alive").length,
      deadCount: refs.filter(r => r.status === "dead").length,
      references: refs,
      duration: report.duration,
      checkedAt: new Date().toISOString(),
    }));
  } catch { /* storage unavailable: report page will show its empty state */ }
}

/**************************************
 * RESULTS RENDERING
 **************************************/
function summarize(refs) {
  const counts = { total: refs.length, alive: 0, dead: 0, archived: 0 };
  const categories = {};
  const domains = new Set();
  refs.forEach(r => {
    if (r.status === "alive") counts.alive++;
    if (r.status === "dead") counts.dead++;
    if (r.status === "dead" && r.suggestedArchive) counts.archived++;
    const cat = r.sourceCategory || "other";
    categories[cat] = categories[cat] || { total: 0, dead: 0 };
    categories[cat].total++;
    if (r.status === "dead") categories[cat].dead++;
    if (r.domain) domains.add(r.domain);
  });
  counts.domains = domains.size;
  counts.statements = new Set(refs.map(r => r.propertyId)).size;
  return { counts, categories };
}

function groupByProperty(refs) {
  const groups = new Map();
  refs.forEach(r => {
    const key = r.propertyId || r.propertyLabel || "other";
    if (!groups.has(key)) {
      groups.set(key, { id: key, label: r.propertyLabel || key, refs: [], dead: 0 });
    }
    const g = groups.get(key);
    g.refs.push(r);
    if (r.status === "dead") g.dead++;
  });
  // Statements with broken references first, then alphabetical.
  return [...groups.values()].sort((a, b) => (b.dead - a.dead) || a.label.localeCompare(b.label));
}

function matchesFilters(ref) {
  const { status, category, text } = state.filters;
  if (status === "dead" && ref.status !== "dead") return false;
  if (status === "alive" && ref.status !== "alive") return false;
  if (status === "archived" && !(ref.status === "dead" && ref.suggestedArchive)) return false;
  if (category !== "all" && (ref.sourceCategory || "other") !== category) return false;
  if (text) {
    const haystack = `${ref.url} ${ref.propertyLabel} ${ref.statementValue} ${ref.domain}`.toLowerCase();
    if (!haystack.includes(text)) return false;
  }
  return true;
}

function displayUrl(url) {
  return url.replace(/^https?:\/\/(www\.)?/i, "").replace(/\/$/, "");
}

function wikidataUrl(qid, pid) {
  return `https://www.wikidata.org/wiki/${encodeURIComponent(qid)}${pid ? `#${encodeURIComponent(pid)}` : ""}`;
}

function renderReport() {
  const report = state.report;
  const { counts, categories } = summarize(report.references);
  const health = counts.total ? Math.round((counts.alive / counts.total) * 100) : 0;
  const deadPct = counts.total ? Math.round((counts.dead / counts.total) * 100) : 0;
  const groups = groupByProperty(report.references);
  const qid = report.qid;

  const lede = counts.dead
    ? `<strong>${escapeHtml(report.label)}</strong> has <strong>${counts.total}</strong> referenced URLs across
       <strong>${counts.statements}</strong> statements. <strong class="text-dead">${counts.dead}</strong>
       (${deadPct}%) could not be reached${counts.archived ? `; archived copies were found for <strong>${counts.archived}</strong> of them` : ""}.`
    : `<strong>${escapeHtml(report.label)}</strong> has <strong>${counts.total}</strong> referenced URLs across
       <strong>${counts.statements}</strong> statements, and every one of them responded. Nice.`;

  const categoryOrder = Object.keys(SOURCE_CATEGORIES).filter(k => categories[k]);

  els.result.innerHTML = `
    <div class="article-layout">
      <nav class="toc" aria-label="Contents">
        <details class="toc__box" ${window.matchMedia("(max-width: 1000px)").matches ? "" : "open"}>
          <summary class="toc__title">Contents</summary>
          <ol class="toc__list">
            <li><a href="#overview">Overview</a></li>
            ${groups.map(g => `
              <li data-toc-for="${escapeHtml(g.id)}">
                <a href="#sec-${escapeHtml(g.id)}">
                  <span class="toc__label">${escapeHtml(g.label)}</span>
                  ${g.dead ? `<span class="toc__badge" title="${g.dead} unreachable">${g.dead}</span>` : ""}
                </a>
              </li>`).join("")}
            <li><a href="#categories">Categories</a></li>
          </ol>
        </details>
      </nav>

      <article class="article">
        <header class="article__header">
          <div>
            <h1 class="article__title">${escapeHtml(report.label)}</h1>
            <p class="article__tagline">
              <a href="${wikidataUrl(qid)}" target="_blank" rel="noopener">${escapeHtml(qid)}</a>
              <span class="sep">/</span>
              <span>Checked against the live web in ${report.duration}s</span>
              <span class="sep">/</span>
              <span>${counts.domains} domains</span>
            </p>
          </div>
          <div class="article__actions">
            <a class="btn-ghost" href="${wikidataUrl(qid)}" target="_blank" rel="noopener" title="Edit on Wikidata">
              <i class="bi bi-pencil-square"></i><span>Edit</span>
            </a>
            <a class="btn-ghost" href="/report" target="_blank" rel="noopener" title="Open full report">
              <i class="bi bi-bar-chart"></i><span>Report</span>
            </a>
            <button type="button" class="btn-ghost" data-action="share" title="Copy link to this result">
              <i class="bi bi-link-45deg"></i><span>Share</span>
            </button>
            ${tts.supported ? `
            <button type="button" class="btn-ghost btn-listen" data-action="listen" aria-pressed="false" title="Read these results aloud">
              <i class="bi bi-volume-up"></i><span>Listen</span>
            </button>` : ""}
          </div>
        </header>
        ${report.description ? `<p class="article__desc">${escapeHtml(report.description)}</p>` : ""}

        <div class="summary" role="group" aria-label="Reference health summary">
          <div class="summary__cell">
            <div class="summary__label">Health</div>
            <div class="summary__value">${health}%<small>reachable</small></div>
            <div class="meter" role="img" aria-label="${counts.alive} alive, ${counts.dead} unreachable">
              <span class="meter__alive" style="width:${counts.total ? (counts.alive / counts.total) * 100 : 0}%"></span>
              <span class="meter__dead" style="width:${counts.total ? (counts.dead / counts.total) * 100 : 0}%"></span>
            </div>
          </div>
          <div class="summary__cell summary__cell--alive">
            <div class="summary__label">Alive</div>
            <div class="summary__value">${counts.alive}<small>/ ${counts.total}</small></div>
          </div>
          <div class="summary__cell summary__cell--dead">
            <div class="summary__label">Unreachable</div>
            <div class="summary__value">${counts.dead}</div>
          </div>
          <div class="summary__cell summary__cell--archive">
            <div class="summary__label">Archived</div>
            <div class="summary__value">${counts.archived}</div>
          </div>
        </div>

        <p class="article__lede">${lede}</p>

        <section class="filters" id="overview" aria-label="Filter references">
          <div class="filters__row">
            <div class="segmented" role="radiogroup" aria-label="Status">
              ${[
                ["all", "All", counts.total],
                ["dead", "Unreachable", counts.dead],
                ["archived", "Archived", counts.archived],
                ["alive", "Alive", counts.alive],
              ].map(([value, text, n]) => `
                <button type="button" role="radio" class="segmented__btn" data-status="${value}"
                        aria-checked="${state.filters.status === value}">
                  ${text}<span class="count">${n}</span>
                </button>`).join("")}
            </div>
            <div class="filter-text">
              <i class="bi bi-search" aria-hidden="true"></i>
              <label class="visually-hidden" for="filterText">Filter by URL, domain or statement</label>
              <input type="search" id="filterText" placeholder="Filter by URL, domain or statement">
            </div>
          </div>

          <div class="category-chips" role="radiogroup" aria-label="Source type">
            <button type="button" role="radio" class="chip chip--filter" data-category="all"
                    aria-checked="${state.filters.category === "all"}">All sources</button>
            ${categoryOrder.map(key => {
              const meta = sourceCategoryMeta(key);
              const c = categories[key];
              return `
                <button type="button" role="radio" class="chip chip--filter" data-category="${key}"
                        aria-checked="${state.filters.category === key}">
                  <i class="bi ${meta.icon}"></i> ${meta.label}<span class="count">${c.total}</span>${c.dead ? `<span class="count count--dead" title="${c.dead} unreachable">${c.dead}</span>` : ""}
                </button>`;
            }).join("")}
          </div>
        </section>

        <div id="sections"></div>

        <footer class="catlinks" id="categories">
          <span class="catlinks__label">Categories:</span>
          <ul class="catlinks__list">
            ${counts.dead ? `<li><button type="button" data-status-link="dead">Items with unreachable references</button></li>` : ""}
            ${counts.archived ? `<li><button type="button" data-status-link="archived">References with archived copies</button></li>` : ""}
            ${counts.dead && counts.archived < counts.dead ? `<li><span>Dead references without archives (${counts.dead - counts.archived})</span></li>` : ""}
            ${categoryOrder.map(key => `
              <li><button type="button" data-category-link="${key}">${sourceCategoryMeta(key).label} (${categories[key].total})</button></li>
            `).join("")}
          </ul>
        </footer>
      </article>
    </div>`;

  renderSections();
  bindReportEvents();
  els.result.querySelector(".article")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderReference(ref) {
  const qid = state.report.qid;
  const isDead = ref.status === "dead";
  const meta = sourceCategoryMeta(ref.sourceCategory);
  const statusLabel = isDead ? "Unreachable" : ref.status === "alive" ? "Alive" : ref.status;
  const http = ref.httpStatus ? `HTTP ${ref.httpStatus}` : isDead ? "No response" : "";
  const error = ref.error ? `${ref.httpStatus ? " - " : ""}${ref.error}` : "";
  const archive = ref.suggestedArchive;

  return `
    <li class="ref ${isDead ? "ref--dead" : "ref--alive"}" data-ref="${state.report.references.indexOf(ref)}">
      <span class="ref__status" title="${escapeHtml(statusLabel)}${http ? ` (${escapeHtml(http)})` : ""}${error ? ` - ${escapeHtml(error)}` : ""}">
        <i class="bi ${isDead ? "bi-x-circle-fill" : "bi-check-circle-fill"}" aria-hidden="true"></i>
        <span class="visually-hidden">${escapeHtml(statusLabel)}</span>
      </span>

      <div class="ref__body">
        <a class="ref__url" href="${escapeHtml(safeUrl(ref.url))}" target="_blank" rel="noopener noreferrer"
           title="${escapeHtml(ref.url)}">${escapeHtml(displayUrl(ref.url))}</a>
        <div class="ref__meta">
          <span class="tag"><i class="bi ${meta.icon}"></i> ${meta.label}</span>
          ${ref.statementValue ? `<span class="ref__value" title="Statement value">${escapeHtml(ref.statementValue)}</span>` : ""}
          ${http ? `<span class="ref__http ${isDead ? "text-dead" : ""}">${escapeHtml(http)}</span>` : ""}
          ${error ? `<span class="ref__error text-dead">${escapeHtml(error)}</span>` : ""}
        </div>

        ${isDead ? `
          <div class="ref__archive ${archive ? "" : "ref__archive--none"}">
            ${archive ? `
              <i class="bi bi-archive" aria-hidden="true"></i>
              <span>Archived copy${formatArchiveDate(archive.timestamp) ? ` from <strong>${escapeHtml(formatArchiveDate(archive.timestamp))}</strong>` : ""}</span>
              <a href="${escapeHtml(safeUrl(archive.archiveUrl))}" target="_blank" rel="noopener noreferrer">Open snapshot <i class="bi bi-box-arrow-up-right"></i></a>
              <button type="button" class="link-btn" data-copy="${escapeHtml(archive.archiveUrl)}">Copy archive URL</button>
            ` : `
              <i class="bi bi-inbox" aria-hidden="true"></i>
              <span>No snapshot found automatically.</span>
              <a href="https://web.archive.org/web/*/${escapeHtml(encodeURI(ref.url))}" target="_blank" rel="noopener noreferrer">Search the Wayback Machine</a>
            `}
          </div>
          <div class="ref__replacement mt-2">
            <button type="button" class="btn-ghost btn-sm" data-action="find-replacement"
                    data-qid="${qid}" data-property="${ref.propertyId}"
                    data-dead-url="${escapeHtml(ref.url)}"
                    title="Find replacement references from authoritative sources">
              <i class="bi bi-search"></i> Find Replacement
            </button>
          </div>` : ""}
      </div>

      <div class="ref__actions">
        <a class="icon-btn" href="${wikidataUrl(qid, ref.propertyId)}" target="_blank" rel="noopener"
           title="Edit this statement on Wikidata" aria-label="Edit this statement on Wikidata">
          <i class="bi bi-pencil"></i>
        </a>
        <button type="button" class="icon-btn" data-copy="${escapeHtml(ref.url)}" title="Copy URL" aria-label="Copy URL">
          <i class="bi bi-clipboard"></i>
        </button>
      </div>
    </li>`;
}

function renderSections() {
  const container = document.getElementById("sections");
  if (!container) return;
  tts.stop(); // the elements being read are about to be replaced

  const groups = groupByProperty(state.report.references.filter(matchesFilters));
  const visible = new Set(groups.map(g => g.id));

  // Dim table-of-contents entries for sections hidden by the filters.
  els.result.querySelectorAll("[data-toc-for]").forEach(li => {
    li.classList.toggle("is-filtered", !visible.has(li.dataset.tocFor));
  });

  if (!groups.length) {
    container.innerHTML = `
      <div class="empty">
        <i class="bi bi-funnel" aria-hidden="true"></i>
        <p>No references match these filters.</p>
        <button type="button" class="btn-ghost" data-action="clear-filters">Clear filters</button>
      </div>`;
    return;
  }

  container.innerHTML = groups.map(g => {
    const refs = [...g.refs].sort((a, b) => (a.status === "dead" ? 0 : 1) - (b.status === "dead" ? 0 : 1));
    return `
      <section class="statement" id="sec-${escapeHtml(g.id)}">
        <h2 class="statement__title">
          <span>${escapeHtml(g.label)}</span>
          <a class="statement__pid" href="https://www.wikidata.org/wiki/Property:${encodeURIComponent(g.id)}"
             target="_blank" rel="noopener" title="View property on Wikidata">${escapeHtml(g.id)}</a>
          <span class="statement__count">
            ${refs.length} reference${refs.length === 1 ? "" : "s"}${g.dead ? ` &middot; <span class="text-dead">${g.dead} unreachable</span>` : ""}
          </span>
        </h2>
        <ul class="ref-list">${refs.map(renderReference).join("")}</ul>
      </section>`;
  }).join("");
  observeSections();
}

function setFilter(key, value) {
  state.filters[key] = value;
  els.result.querySelectorAll("[data-status]").forEach(b =>
    b.setAttribute("aria-checked", String(b.dataset.status === state.filters.status)));
  els.result.querySelectorAll("[data-category]").forEach(b =>
    b.setAttribute("aria-checked", String(b.dataset.category === state.filters.category)));
  renderSections();
}

function bindReportEvents() {
  const filterText = document.getElementById("filterText");
  filterText?.addEventListener("input", debounce(() => {
    setFilter("text", filterText.value.trim().toLowerCase());
  }, 150));
}

/** Keep the contents list expanded on wide screens and collapsed on narrow ones, even after resizing/rotating. */
const narrowScreen = window.matchMedia("(max-width: 1000px)");
narrowScreen.addEventListener("change", e => {
  const toc = els.result.querySelector(".toc__box");
  if (toc) toc.open = !e.matches;
});

/** Highlight the table-of-contents entry for the section currently in view. */
let sectionObserver = null;
function observeSections() {
  sectionObserver?.disconnect();
  if (!("IntersectionObserver" in window)) return;
  const links = new Map(
    [...els.result.querySelectorAll(".toc__list a")].map(a => [a.getAttribute("href").slice(1), a])
  );
  sectionObserver = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      links.forEach(a => a.classList.remove("is-current"));
      links.get(entry.target.id)?.classList.add("is-current");
    });
  }, { rootMargin: "-20% 0px -70% 0px" });
  els.result.querySelectorAll(".statement, #overview, #categories").forEach(el => sectionObserver.observe(el));
}

// "/" focuses the search box (like most search-first sites).
document.addEventListener("keydown", e => {
  if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
  const tag = document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable) return;
  e.preventDefault();
  els.input.focus();
  els.input.select();
});

/**************************************
 * DELEGATED ACTIONS (results area)
 **************************************/
els.result.addEventListener("click", e => {
  const target = e.target.closest("button, a");
  if (!target) return;

  if (target.dataset.status) return setFilter("status", target.dataset.status);
  if (target.dataset.category) return setFilter("category", target.dataset.category);

  if (target.dataset.statusLink || target.dataset.categoryLink) {
    if (target.dataset.statusLink) setFilter("status", target.dataset.statusLink);
    if (target.dataset.categoryLink) setFilter("category", target.dataset.categoryLink);
    document.getElementById("overview")?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }

  if (target.dataset.copy) return copyText(target.dataset.copy, "Copied to clipboard");

  switch (target.dataset.action) {
    case "new-search":
      resetSearch();
      break;
    case "share":
      copyText(window.location.href, "Link copied — share it to show this result");
      break;
    case "listen":
      tts.toggle();
      break;
    case "clear-filters": {
      const filterText = document.getElementById("filterText");
      if (filterText) filterText.value = "";
      state.filters = { status: "all", category: "all", text: "" };
      setFilter("status", "all");
      break;
    }
    case "find-replacement":
      openReplacementModal(target);
      break;
  }
});

/**************************************
 * MODAL ACTIONS (document-level for dynamic modals)
 **************************************/
document.addEventListener("click", e => {
  const target = e.target.closest("button[data-action='confirm-replacement']");
  if (!target) return;
  
  confirmReplacement(target);
});


async function confirmReplacement(button) {
  const qid = button.dataset.qid;
  const propertyId = button.dataset.property;
  const newUrl = button.dataset.newUrl;
  const deadUrl = button.dataset.deadUrl;
  const modal = document.getElementById("replacementModal");
  const statementHash = modal?.dataset.statementHash || "";
  
  if (!qid || !propertyId || !newUrl) {
    toast("Missing required data for replacement");
    return;
  }
  
  // Check OAuth login
  const user = await fetch("/api/user").then(r => r.json()).catch(() => null);
  if (!user?.username) {
    toast("Please log in with Wikimedia to edit references");
    return;
  }
  
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Updating...';
  
  try {
    const response = await fetch("/api/replace-reference", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        qid,
        property_id: propertyId,
        statement_hash: statementHash,
        new_url: newUrl,
        dead_url: deadUrl
      })
    });
    
    const data = await response.json();
    
    if (data.error) {
      throw new Error(data.error);
    }
    
    toast("Reference updated successfully!");
    const bsModal = bootstrap.Modal.getInstance(modal);
    bsModal?.hide();
    
    // Refresh the validation
    validate(qid, state.report.label);
    
  } catch (err) {
    toast("Failed to update reference: " + err.message);
  } finally {
    button.disabled = false;
    button.innerHTML = '<i class="bi bi-check-circle"></i> Use This';
  }
}


/**************************************
 * REPLACEMENT CANDIDATE MODAL
 **************************************/
function openReplacementModal(button) {
  const qid = button.dataset.qid;
  const propertyId = button.dataset.property;
  const deadUrl = button.dataset.deadUrl;
  
  // Find the statement hash
  const refIndex = parseInt(button.closest("[data-ref]")?.dataset.ref || "-1");
  let statementHash = "";
  if (refIndex >= 0 && state.report && state.report.references[refIndex]) {
    const ref = state.report.references[refIndex];
    // Generate hash from claim data
    statementHash = hashCode(`${qid}-${propertyId}-${ref.statementValue}`);
  }
  
  // Create modal
  const modalId = "replacementModal";
  let modal = document.getElementById(modalId);
  if (!modal) {
    modal = document.createElement("div");
    modal.id = modalId;
    modal.className = "modal fade";
    modal.setAttribute("tabindex", "-1");
    modal.setAttribute("aria-labelledby", "replacementModalLabel");
    modal.setAttribute("aria-hidden", "true");
    modal.innerHTML = `
      <div class="modal-dialog modal-lg modal-dialog-scrollable">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title" id="replacementModalLabel"><i class="bi bi-search"></i> Find Replacement References</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>
          <div class="modal-body">
            <div id="replacementLoading" class="text-center py-4">
              <div class="spinner-border" role="status"><span class="visually-hidden">Searching...</span></div>
              <p class="mt-2">Searching authoritative sources...</p>
            </div>
            <div id="replacementResults" class="d-none">
              <div class="mb-3">
                <label class="form-label"><strong>Claim:</strong> <span id="replacementClaim"></span></label>
                <div class="text-muted small">Dead URL: <a id="replacementDeadUrl" href="#" target="_blank" class="text-break"></a></div>
              </div>
              <div id="replacementCandidates" class="list-group"></div>
            </div>
            <div id="replacementError" class="alert alert-danger d-none"></div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(modal);
  }
  
  const bsModal = new bootstrap.Modal(modal);
  bsModal.show();
  
  // Reset modal content
  document.getElementById("replacementLoading").classList.remove("d-none");
  document.getElementById("replacementResults").classList.add("d-none");
  document.getElementById("replacementError").classList.add("d-none");
  document.getElementById("replacementCandidates").innerHTML = "";
  
  // Fetch candidates
  fetch("/api/candidates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      qid,
      property_id: propertyId,
      statement_hash: statementHash,
      dead_url: deadUrl
    })
  })
    .then(r => r.json())
    .then(data => {
      document.getElementById("replacementLoading").classList.add("d-none");
      if (data.error) {
        document.getElementById("replacementError").textContent = data.error;
        document.getElementById("replacementError").classList.remove("d-none");
        return;
      }
      
      const claim = data.claim;
      document.getElementById("replacementClaim").innerHTML = 
        `<code>${escapeHtml(claim.property_id)}</code> ${escapeHtml(claim.property_label)}: <strong>${escapeHtml(claim.statement_value_label)}</strong>`;
      document.getElementById("replacementDeadUrl").href = claim.dead_url;
      document.getElementById("replacementDeadUrl").textContent = claim.dead_url;
      
      const candidates = data.candidates || [];
      const container = document.getElementById("replacementCandidates");
      
      if (candidates.length === 0) {
        container.innerHTML = `
          <div class="list-group-item text-center text-muted py-4">
            <i class="bi bi-search-x" style="font-size: 2rem;"></i>
            <p class="mt-2">No suitable replacement candidates found.</p>
            <p class="small">Try searching manually or check the Wayback Machine.</p>
          </div>`;
      } else {
        container.innerHTML = candidates.map((cand, i) => `
          <div class="list-group-item">
            <div class="d-flex justify-content-between align-items-start">
              <div class="flex-grow-1">
                <div class="d-flex align-items-center gap-2 mb-1">
                  <span class="badge bg-${getCategoryColor(cand.source_category)}">${escapeHtml(cand.source_category)}</span>
                  <span class="badge bg-info text-dark">Score: ${Math.round(cand.score * 100)}%</span>
                </div>
                <div class="small text-muted mb-1">${escapeHtml(cand.domain)}</div>
                <div class="snippet">${cand.snippet}</div>
              </div>
              <div class="ms-3 d-flex flex-column gap-1">
                <a href="${escapeHtml(cand.deep_link)}" target="_blank" class="btn btn-sm btn-outline-primary" title="Open at relevant section">
                  <i class="bi bi-box-arrow-up-right"></i> Verify
                </a>
                <button type="button" class="btn btn-sm btn-success" data-action="confirm-replacement"
                        data-qid="${escapeHtml(qid)}" data-property="${escapeHtml(propertyId)}"
                        data-new-url="${escapeHtml(cand.url)}" data-dead-url="${escapeHtml(deadUrl)}"
                        title="Confirm and add this as a reference via Wikidata edit">
                  <i class="bi bi-check-circle"></i> Use This
                </button>
              </div>
            </div>
          </div>`).join("");
      }
      
      document.getElementById("replacementResults").classList.remove("d-none");
    })
    .catch(err => {
      document.getElementById("replacementLoading").classList.add("d-none");
      document.getElementById("replacementError").textContent = "Failed to fetch candidates: " + err.message;
      document.getElementById("replacementError").classList.remove("d-none");
    });
  
  // Store current claim context for confirmation
  modal.dataset.qid = qid;
  modal.dataset.property = propertyId;
  modal.dataset.deadUrl = deadUrl;
  modal.dataset.statementHash = statementHash;
}


function getCategoryColor(category) {
  const colors = {
    "reference": "primary",
    "academic": "info",
    "government": "success",
    "wikimedia": "warning",
    "archive": "secondary",
    "news": "danger",
    "social": "dark",
    "other": "light"
  };
  return colors[category] || "secondary";
}


function hashCode(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash;
  }
  return Math.abs(hash).toString(16).substring(0, 16);
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch {
    toast("Couldn't copy — your browser blocked clipboard access");
  }
}

function toast(message) {
  let el = document.getElementById("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.className = "toast-msg";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.add("is-visible");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("is-visible"), 2200);
}

/**************************************
 * READ ALOUD (speech synthesis)
 **************************************/
const tts = (() => {
  const supported = "speechSynthesis" in window && "SpeechSynthesisUtterance" in window;
  const synth = supported ? window.speechSynthesis : null;
  const RATES = [1, 1.25, 1.5, 0.75];

  let queue = [];          // [{ text, el, label }]
  let index = 0;
  let playing = false;
  let paused = false;
  let token = 0;           // invalidates callbacks from cancelled utterances
  let rate = 1;
  let voice = null;
  let current = null;      // keep a reference so Chrome doesn't garbage-collect it mid-sentence
  let player = null;

  function pickVoice() {
    const voices = synth.getVoices();
    const english = voices.filter(v => /^en([-_]|$)/i.test(v.lang));
    return english.find(v => v.default) || english.find(v => v.localService) || english[0] || null;
  }
  if (supported) {
    voice = pickVoice();
    synth.addEventListener?.("voiceschanged", () => { voice = pickVoice(); });
  }

  function spokenDate(ts) {
    if (!ts || ts.length < 8) return "";
    const d = new Date(Date.UTC(+ts.slice(0, 4), +ts.slice(4, 6) - 1, +ts.slice(6, 8)));
    return isNaN(d) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  }

  function describeRef(ref) {
    const domain = ref.domain || displayUrl(ref.url).split("/")[0];
    const value = ref.statementValue ? `, supporting "${ref.statementValue}"` : "";
    if (ref.status !== "dead") return `Alive: ${domain}${value}.`;

    const reason = ref.httpStatus ? `The server returned error ${ref.httpStatus}.` : "The server did not respond.";
    const archive = ref.suggestedArchive
      ? `An archived copy${spokenDate(ref.suggestedArchive.timestamp) ? ` from ${spokenDate(ref.suggestedArchive.timestamp)}` : ""} is available.`
      : "No archived copy was found.";
    return `Unreachable: ${domain}${value}. ${reason} ${archive}`;
  }

  /** Turn what is currently on screen (respecting filters) into short, speakable chunks. */
  function buildQueue() {
    const report = state.report;
    if (!report) return [];
    const items = [];
    const lede = els.result.querySelector(".article__lede");
    const ledeText = lede ? lede.textContent.replace(/\s+/g, " ").trim() : "";
    items.push({ text: `Reference results for ${report.label}. ${ledeText}`, el: lede, label: "Summary" });

    const { status, category, text } = state.filters;
    const refs = report.references.filter(matchesFilters);
    if (status !== "all" || category !== "all" || text) {
      const parts = [];
      if (status === "dead") parts.push("unreachable references");
      else if (status === "alive") parts.push("alive references");
      else if (status === "archived") parts.push("references with archived copies");
      else parts.push("references");
      if (category !== "all") parts.push(`from ${sourceCategoryMeta(category).label}`);
      if (text) parts.push(`matching "${text}"`);
      items.push({ text: `Showing ${refs.length} ${parts.join(" ")}.`, el: document.getElementById("overview"), label: "Filters" });
    } else if (refs.filter(r => r.status === "alive").length > 5) {
      items.push({
        text: "Tip: to hear only broken links, stop reading, choose the Unreachable filter, then press Listen again.",
        el: document.getElementById("overview"),
        label: "Tip",
      });
    }

    groupByProperty(refs).forEach(g => {
      const n = g.refs.length;
      items.push({
        text: `${g.label}. ${n} reference${n === 1 ? "" : "s"}${g.dead ? `, ${g.dead} unreachable` : ""}.`,
        el: document.getElementById(`sec-${g.id}`),
        label: g.label,
      });
      [...g.refs]
        .sort((a, b) => (a.status === "dead" ? 0 : 1) - (b.status === "dead" ? 0 : 1))
        .forEach(ref => items.push({
          text: describeRef(ref),
          el: els.result.querySelector(`[data-ref="${report.references.indexOf(ref)}"]`),
          label: `${g.label} · ${ref.domain || "reference"}`,
        }));
    });

    items.push({ text: "End of results.", el: null, label: "End" });
    return items;
  }

  /* ---------- Player UI ---------- */
  function ensurePlayer() {
    if (player) return player;
    player = document.createElement("div");
    player.className = "tts-player";
    player.setAttribute("role", "region");
    player.setAttribute("aria-label", "Read aloud controls");
    player.hidden = true;
    player.innerHTML = `
      <span class="tts-player__wave" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
      <div class="tts-player__text">
        <span class="tts-player__label">Reading aloud</span>
        <span class="tts-player__now" data-tts="now"></span>
      </div>
      <span class="tts-player__progress" data-tts="progress"></span>
      <div class="tts-player__controls">
        <button type="button" class="tts-btn" data-tts="prev" aria-label="Previous item" title="Previous"><i class="bi bi-skip-backward-fill"></i></button>
        <button type="button" class="tts-btn tts-btn--main" data-tts="play" aria-label="Pause" title="Pause"><i class="bi bi-pause-fill"></i></button>
        <button type="button" class="tts-btn" data-tts="next" aria-label="Next item" title="Next"><i class="bi bi-skip-forward-fill"></i></button>
        <button type="button" class="tts-btn tts-btn--rate" data-tts="rate" aria-label="Reading speed 1 times" title="Reading speed">1×</button>
        <button type="button" class="tts-btn" data-tts="stop" aria-label="Stop reading" title="Stop"><i class="bi bi-x-lg"></i></button>
      </div>`;
    document.body.appendChild(player);

    player.addEventListener("click", e => {
      const action = e.target.closest("[data-tts]")?.dataset.tts;
      if (action === "play") paused ? resume() : pause();
      else if (action === "prev") jump(-1);
      else if (action === "next") jump(1);
      else if (action === "rate") cycleRate();
      else if (action === "stop") stop();
    });
    return player;
  }

  function updatePlayer() {
    if (!player) return;
    const item = queue[index];
    player.querySelector('[data-tts="now"]').textContent = item ? item.label : "";
    player.querySelector('[data-tts="progress"]').textContent = queue.length ? `${Math.min(index + 1, queue.length)} / ${queue.length}` : "";
    const play = player.querySelector('[data-tts="play"]');
    play.innerHTML = `<i class="bi ${paused ? "bi-play-fill" : "bi-pause-fill"}"></i>`;
    play.setAttribute("aria-label", paused ? "Resume" : "Pause");
    play.title = paused ? "Resume" : "Pause";
    player.classList.toggle("is-paused", paused);
    const rateBtn = player.querySelector('[data-tts="rate"]');
    rateBtn.textContent = `${rate}×`;
    rateBtn.setAttribute("aria-label", `Reading speed ${rate} times`);
  }

  function setListenButton(on) {
    const btn = els.result.querySelector('[data-action="listen"]');
    if (!btn) return;
    btn.setAttribute("aria-pressed", String(on));
    btn.classList.toggle("is-active", on);
    btn.innerHTML = on
      ? `<i class="bi bi-stop-fill"></i><span>Stop</span>`
      : `<i class="bi bi-volume-up"></i><span>Listen</span>`;
  }

  function clearHighlight() {
    els.result.querySelectorAll(".is-speaking").forEach(el => el.classList.remove("is-speaking"));
  }

  function highlight(item) {
    clearHighlight();
    if (!item?.el) return;
    item.el.classList.add("is-speaking");
    const r = item.el.getBoundingClientRect();
    if (r.top < 80 || r.bottom > window.innerHeight - 110) {
      item.el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  /* ---------- Playback ---------- */
  function speakCurrent() {
    const my = ++token;
    synth.cancel();
    const item = queue[index];
    if (!item) return stop();
    paused = false;
    highlight(item);
    updatePlayer();

    const u = new SpeechSynthesisUtterance(item.text);
    u.lang = voice?.lang || "en-US";
    if (voice) u.voice = voice;
    u.rate = rate;
    u.onend = () => {
      if (my !== token) return;
      index++;
      if (index >= queue.length) stop();
      else speakCurrent();
    };
    u.onerror = e => {
      if (my !== token || e.error === "interrupted" || e.error === "canceled") return;
      toast("Reading aloud isn't available right now.");
      stop();
    };
    current = u;
    // A short delay after cancel() avoids Chrome silently dropping the next utterance.
    setTimeout(() => { if (my === token) synth.speak(u); }, 60);
  }

  function start() {
    queue = buildQueue();
    if (!queue.length) return;
    index = 0;
    playing = true;
    ensurePlayer().hidden = false;
    document.body.classList.add("tts-active");
    setListenButton(true);
    speakCurrent();
  }

  function pause() {
    token++;            // ignore the end event caused by cancel()
    synth.cancel();     // cancel + re-speak is more reliable across browsers than pause()/resume()
    paused = true;
    updatePlayer();
  }

  function resume() {
    speakCurrent();     // restarts the current item from its beginning
  }

  function jump(delta) {
    index = Math.max(0, Math.min(queue.length - 1, index + delta));
    speakCurrent();
  }

  function cycleRate() {
    rate = RATES[(RATES.indexOf(rate) + 1) % RATES.length];
    if (paused) updatePlayer();
    else speakCurrent();
  }

  function stop() {
    if (!supported) return;
    token++;
    if (playing || synth.speaking || synth.pending) synth.cancel();
    playing = false;
    paused = false;
    current = null;
    clearHighlight();
    if (player) player.hidden = true;
    document.body.classList.remove("tts-active");
    setListenButton(false);
  }

  if (supported) {
    window.addEventListener("pagehide", stop);
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && playing) stop();
    });
  }

  return {
    supported,
    toggle: () => (playing ? stop() : start()),
    stop,
  };
})();

/**************************************
 * VOICE SEARCH (Web Speech API)
 **************************************/
(function initVoiceSearch() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const btn = document.getElementById("voiceBtn");
  const status = document.getElementById("voiceStatus");
  if (!SpeechRecognition || !btn) return; // unsupported browser: keep the mic hidden

  btn.hidden = false;
  const recognition = new SpeechRecognition();
  recognition.lang = document.documentElement.lang || "en";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.maxAlternatives = 1;

  let listening = false;
  let finalText = "";

  function setStatus(text) {
    status.textContent = text || "";
    status.hidden = !text;
  }

  function setListening(on) {
    listening = on;
    btn.classList.toggle("is-listening", on);
    btn.setAttribute("aria-pressed", String(on));
    btn.setAttribute("aria-label", on ? "Stop voice search" : "Search by voice");
    btn.querySelector("i").className = `bi ${on ? "bi-mic-fill" : "bi-mic"}`;
    els.input.dataset.listening = String(on);
    if (on) els.input.placeholder = phoneScreen.matches ? "Listening…" : "Listening… say an item name or QID";
    else applyPlaceholder();
  }

  /** "q 42" / "Q forty two" style transcripts -> "Q42" when it looks like a QID. */
  function asQid(text) {
    const compact = text.replace(/[\s.\-]/g, "");
    return QID_PATTERN.test(compact) ? compact.toUpperCase() : null;
  }

  btn.addEventListener("click", () => {
    if (listening) {
      recognition.stop();
      return;
    }
    finalText = "";
    closeSuggestions();
    try {
      recognition.start();
    } catch {
      /* start() throws if already running; ignore */
    }
  });

  recognition.addEventListener("start", () => {
    setListening(true);
    setStatus("Listening…");
  });

  recognition.addEventListener("result", e => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const transcript = e.results[i][0].transcript;
      if (e.results[i].isFinal) finalText += transcript;
      else interim += transcript;
    }
    const shown = (finalText + interim).trim();
    els.input.value = shown;
    els.clear.hidden = !shown;
    state.selected = null;
  });

  recognition.addEventListener("error", e => {
    const messages = {
      "not-allowed": "Microphone access was blocked. Allow it in your browser settings to search by voice.",
      "service-not-allowed": "Voice search isn't available in this browser.",
      "no-speech": "Didn't catch that. Tap the mic and try again.",
      "audio-capture": "No microphone was found.",
      "network": "Voice search needs an internet connection.",
    };
    if (e.error !== "aborted") toast(messages[e.error] || "Voice search stopped unexpectedly.");
  });

  recognition.addEventListener("end", () => {
    setListening(false);
    setStatus("");
    const text = finalText.trim();
    if (!text) return;

    const qid = asQid(text);
    if (qid) {
      els.input.value = qid;
      validate(qid, qid);
      return;
    }
    // Show matching items so the user can confirm which one they meant.
    els.input.value = text;
    els.input.focus();
    els.input.dispatchEvent(new Event("input"));
  });

  // Esc stops listening.
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && listening) recognition.abort();
  });
})();

/**************************************
 * RESET, HISTORY & DEEP LINKS
 **************************************/
function resetSearch({ updateHistory = true } = {}) {
  tts.stop();
  clearInterval(elapsedTimer);
  state.report = null;
  state.selected = null;
  els.result.innerHTML = "";
  els.input.value = "";
  els.clear.hidden = true;
  document.body.classList.remove("has-results");
  document.title = "Wikidata Reference Validator";
  if (updateHistory) history.pushState({}, "", window.location.pathname);
  window.scrollTo({ top: 0, behavior: "smooth" });
  els.input.focus();
}

function itemFromUrl() {
  const item = new URLSearchParams(window.location.search).get("item");
  return item && QID_PATTERN.test(item) ? item.toUpperCase() : null;
}

window.addEventListener("popstate", () => {
  const language = languageFromUrl();
  if (language) setLanguage(language);
  const qid = itemFromUrl();
  if (qid) {
    els.input.value = qid;
    validate(qid, qid, { updateHistory: false });
  } else {
    resetSearch({ updateHistory: false });
  }
});

// Open a shared link like /?item=Q42&lang=fr directly.
(function initFromUrl() {
  const language = languageFromUrl();
  if (setLanguage(language)) rememberLanguage(language);

  const qid = itemFromUrl();
  if (qid) {
    els.input.value = qid;
    els.clear.hidden = false;
    validate(qid, qid, { updateHistory: false });
  }
})();
