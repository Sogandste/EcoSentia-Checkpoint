/*
 * Interface logic.
 *
 * The interface never computes a support level, never merges lens results, and
 * never hides a finding behind a control the operator must open. Anything the
 * reader must see to interpret the numbers is rendered next to the numbers.
 */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let CONFIG = null;
let PLAN = null;

async function api(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({ error: "unreadable response" }));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `request failed (${res.status})`);
    err.payload = data;
    throw err;
  }
  return data;
}

function showError(message, payload) {
  let html = `<b>${esc(message)}</b>`;
  // Per-source diagnostics are shown on failure so that an outage is legible
  // as an outage. Without them, a failed scan looks like an empty literature.
  if (payload && payload.sources) {
    html += "<div style='margin-top:8px;font-size:13px'>";
    payload.sources.forEach((s) => {
      html += `<div>${esc(s.source)}: ${esc(s.status)}` +
        (s.error ? ` — ${esc(s.error)}` : "") + "</div>";
    });
    html += "</div>";
  }
  $("errBox").innerHTML = html;
  $("errBox").classList.remove("hide");
  $("errBox").scrollIntoView({ behavior: "smooth", block: "center" });
}

function clearError() { $("errBox").classList.add("hide"); }

/* ---------------------------------------------------------------- config */

async function init() {
  try {
    CONFIG = await api("/api/config");
  } catch (e) {
    showError("Could not load configuration: " + e.message);
    return;
  }
  $("scope").textContent = CONFIG.scope_statement;

  $("preset").innerHTML = CONFIG.presets
    .map((p) => `<option value="${esc(p.key)}">${esc(p.label)}</option>`).join("");

  $("lenses").innerHTML = CONFIG.lenses.map((l) => `
    <div class="lens">
      <label>
        <input type="checkbox" value="${esc(l.key)}" ${l.default ? "checked" : ""}>
        <span>${esc(l.label)}${l.requires_expert
          ? '<span class="badge b-expert">expert</span>' : ""}</span>
      </label>
      <p>${esc(l.question)}</p>
    </div>`).join("");
}

function selectedLenses() {
  return Array.from($("lenses").querySelectorAll("input:checked"))
    .map((i) => i.value);
}

/* ------------------------------------------------------------------ plan */

$("btnPlan").addEventListener("click", async () => {
  clearError();
  const claim = $("claim").value.trim();
  if (claim.length < 8) { showError("Enter a claim of at least 8 characters."); return; }
  const lenses = selectedLenses();
  if (!lenses.length) { showError("Select at least one lens."); return; }

  $("btnPlan").disabled = true;
  try {
    const data = await api("/api/plan", {
      claim, preset: $("preset").value, lenses,
      max_records: parseInt($("max").value, 10) || 50,
    });
    PLAN = data.plan;
    renderPlan(data);
  } catch (e) {
    showError(e.message, e.payload);
  } finally {
    $("btnPlan").disabled = false;
  }
});

function renderPlan(data) {
  const p = data.plan;
  let h = "";

  h += `<p class="kv"><b>Claim:</b> ${esc(p.claim)}</p>`;
  h += `<p class="kv"><b>Search terms:</b> ${p.claim_terms.map(esc).join(", ")}</p>`;
  if (p.matched_vocabulary.length)
    h += `<p class="kv"><b>Matched preset vocabulary:</b> ${p.matched_vocabulary.map(esc).join(", ")}</p>`;
  if (p.dropped_terms.length)
    h += `<p class="kv"><b>Removed before querying:</b> ${p.dropped_terms.map(esc).join(", ")}</p>`;
  h += `<p class="kv"><b>Plan hash:</b> <code>${esc(data.plan_hash)}</code></p>`;

  p.warnings.forEach((w) => { h += `<div class="warn">${esc(w)}</div>`; });

  h += `<h3>PubMed</h3><pre>${esc(p.queries.pubmed)}</pre>`;
  h += "<ul style='font-size:13px;color:#5f5f58;margin:0 0 12px;padding-left:20px'>" +
    (p.dialect_notes.pubmed || []).map((n) => `<li>${esc(n)}</li>`).join("") + "</ul>";

  h += `<h3>OpenAlex</h3><pre>${esc(p.queries.openalex)}</pre>`;
  h += "<ul style='font-size:13px;color:#5f5f58;margin:0 0 12px;padding-left:20px'>" +
    (p.dialect_notes.openalex || []).map((n) => `<li>${esc(n)}</li>`).join("") + "</ul>";

  h += `<div class="warn">${esc(p.cross_source_note)}</div>`;

  $("planBody").innerHTML = h;
  $("approve").checked = false;
  $("btnScan").disabled = true;
  $("planPanel").classList.remove("hide");
  $("resPanel").classList.add("hide");
  $("planPanel").scrollIntoView({ behavior: "smooth", block: "start" });
}

$("approve").addEventListener("change", (e) => {
  $("btnScan").disabled = !e.target.checked;
});

$("btnEdit").addEventListener("click", () => {
  $("planPanel").classList.add("hide");
  $("claim").focus();
});

/* ------------------------------------------------------------------ scan */

$("btnScan").addEventListener("click", async () => {
  clearError();
  if (!PLAN || !$("approve").checked) return;
  $("btnScan").disabled = true;
  $("busy").classList.remove("hide");
  try {
    const data = await api("/api/scan", { plan: PLAN, approved: true });
    renderResult(data);
  } catch (e) {
    showError(e.message, e.payload);
  } finally {
    $("busy").classList.add("hide");
    $("btnScan").disabled = false;
  }
});

function renderResult(d) {
  const s = d.scoring;
  let h = "";

  /* Coverage first. What the scan could and could not see governs how every
     number below should be read, so it is not placed after them. */
  h += `<div class="warn">${esc(d.retrieval.coverage_note)}</div>`;
  h += `<p class="kv">${esc(s.corpus_note)}</p>`;
  h += `<p class="kv"><b>Plan hash:</b> <code>${esc(d.plan_hash)}</code> · ` +
       `<b>config:</b> ${esc(d.audit.config_version)} · ` +
       `<b>elapsed:</b> ${esc(d.audit.elapsed_s)}s</p>`;

  h += "<h3>Lenses</h3>";
  s.lenses.forEach((l) => {
    if (l.status === "requires_expert") {
      h += `<div class="lensres open">
        <h4>${esc(l.label)} <span class="badge b-expert">unanswered</span></h4>
        <p class="q">${esc(l.question)}</p>
        <div>${esc(l.note)}</div>
        <div class="act" style="color:#5f5f58;font-size:12.5px;margin-top:6px">
          Vocabulary appeared in ${l.records_matched} of ${l.records_analysed}
          record(s). This count is shown for orientation and is not an answer.
        </div></div>`;
      return;
    }
    h += `<div class="lensres ${esc(l.support)}">
      <h4>${esc(l.label)} — ${esc(l.support_label)}</h4>
      <p class="q">${esc(l.question)}</p>
      <div class="kv">${esc(l.support_meaning)}</div>
      <div class="kv">Matched <b>${l.records_matched}</b> of
        ${l.records_analysed} record(s)${l.distinct_first_authors
          ? `, across ${l.distinct_first_authors} distinct first author(s)` : ""}.</div>`;
    if (l.matched_terms.length)
      h += `<div class="kv">Terms found: ${l.matched_terms.map(esc).join(", ")}</div>`;
    if (l.negative_terms_present.length)
      h += `<div class="kv">Qualifying vocabulary present: ${l.negative_terms_present.map(esc).join(", ")}</div>`;
    l.caps_applied.forEach((c) => { h += `<div class="warn">${esc(c)}</div>`; });
    h += "</div>";
  });

  h += `<div class="warn">${esc(s.composite_note)}</div>`;

  h += `<h3>Structural checks — ${d.bias.counts.flag} flag, ` +
       `${d.bias.counts.note} note, ${d.bias.counts.not_assessable} not assessable</h3>`;
  h += `<p class="kv">${esc(d.bias.summary)}</p>`;
  d.bias.findings.forEach((f) => {
    h += `<div class="finding ${esc(f.severity)}">
      <b>${esc(f.title)}</b>${esc(f.detail)}
      <div class="act">→ ${esc(f.action)}</div></div>`;
  });

  if (d.bias.checklist && d.bias.checklist.length) {
    h += "<h3>Before citing this scan</h3><ul style='font-size:13.5px;padding-left:20px'>";
    d.bias.checklist.forEach((c) => { h += `<li>${esc(c)}</li>`; });
    h += "</ul>";
  }

  h += `<h3>Records (${d.records.length})</h3>`;
  d.records.forEach((r) => {
    h += `<div class="rec">
      <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || "(untitled)")}</a>
      <div class="meta">${esc(r.venue || "—")} · ${esc(r.year || "n.d.")} ·
        ${esc(r.seen_in.join(" + "))}${r.has_abstract ? "" : " · no abstract"}</div>
    </div>`;
  });

  h += `<div class="foot" style="margin-top:18px">${esc(d.audit.statement)}
    <div style="margin-top:10px">
      <button class="ghost" id="btnExport">Download audit record (JSON)</button>
    </div></div>`;

  $("resBody").innerHTML = h;
  $("resPanel").classList.remove("hide");
  $("resPanel").scrollIntoView({ behavior: "smooth", block: "start" });

  $("btnExport").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(d, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `ecosentia-${d.plan_hash}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
}

init();