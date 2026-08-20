/* EcoSentia client.
   The client renders what the service returns and computes nothing. Any
   derivation performed here would be absent from the audit trail, so a figure
   on screen would have no counterpart in the record of the scan. */

const state = {
  config: null,
  plan: null,
  request: null,
};

const $ = (id) => document.getElementById(id);

// -----------------------------------------------------------------------
// Utilities
// -----------------------------------------------------------------------

/* All interpolated text passes through this. Titles and abstracts are third-party
   strings and are inserted into markup; escaping them is not optional. */
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function status(message, failed = false) {
  const node = $("status");
  node.textContent = message;
  node.className = failed ? "status fail" : "status";
  node.hidden = false;
  if (!failed) setTimeout(() => { node.hidden = true; }, 2600);
}

function clearStatus() { $("status").hidden = true; }

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body;
  try {
    body = await response.json();
  } catch {
    // A non-JSON response means the request did not reach the application.
    // Reporting it as such prevents it being mistaken for an empty result.
    throw { status: response.status, error: "unreachable",
            message: "The service did not return a valid response." };
  }
  if (!response.ok) throw { status: response.status, ...body };
  return body;
}

function setStep(current) {
  document.querySelectorAll(".steps li").forEach((item) => {
    const step = Number(item.dataset.step);
    item.classList.toggle("active", step === current);
    item.classList.toggle("done", step < current);
  });
}

function show(id) {
  ["step-claim", "step-query", "step-result"].forEach((section) => {
    $(section).hidden = section !== id;
  });
}

function chips(container, items, emptyText) {
  container.innerHTML = items.length
    ? items.map((item) => `<li>${esc(item)}</li>`).join("")
    : `<li class="empty">${esc(emptyText)}</li>`;
}

// -----------------------------------------------------------------------
// Configuration
// -----------------------------------------------------------------------

/* Presets and lenses are fetched rather than hardcoded. A client-side copy
   would drift from the server's definitions, and the interface would then
   describe a scan other than the one executed. */
async function loadConfig() {
  state.config = await api("/api/config");

  const preset = $("preset");
  preset.innerHTML = state.config.presets
    .map((item) => `<option value="${esc(item.key)}">${esc(item.label)}</option>`)
    .join("");
  preset.addEventListener("change", showPresetHint);
  showPresetHint();

  $("lens-list").innerHTML = state.config.lenses.map((lens) => `
    <label class="lens-item">
      <input type="checkbox" name="lens" value="${esc(lens.key)}" checked>
      <span>
        <strong>${esc(lens.label)}</strong>${
          lens.expert_verification_required
            ? '<span class="needs-expert" title="Lexical signal cannot establish this lens">Expert verification</span>'
            : ""
        }
        <span class="lens-q">${esc(lens.question)}</span>
      </span>
    </label>`).join("");

  const claim = $("claim");
  claim.minLength = state.config.claim_length.min;
  claim.maxLength = state.config.claim_length.max;
  claim.addEventListener("input", showClaimHint);
  showClaimHint();
}

function showPresetHint() {
  const chosen = state.config.presets.find((item) => item.key === $("preset").value);
  $("preset-hint").textContent = chosen ? chosen.description : "";
}

/* Length feedback is given while typing rather than on submit. A claim rejected
   after submission has already cost the user the wait, and the two failure modes
   it guards against — over-general and over-narrow queries — both surface as an
   apparently empty literature. */
function showClaimHint() {
  const length = $("claim").value.trim().length;
  const { min, max } = state.config.claim_length;
  const hint = $("claim-hint");
  $("claim-count").textContent = length;

  if (length === 0) {
    hint.textContent = "";
    hint.className = "hint";
  } else if (length < min) {
    hint.textContent = `${min - length} more characters needed. State the mechanism and the outcome.`;
    hint.className = "hint warn";
  } else if (length > max * 0.9) {
    hint.textContent = "Long claims often contain more than one assertion. Consider splitting.";
    hint.className = "hint warn";
  } else {
    hint.textContent = "";
    hint.className = "hint";
  }
}

function readForm() {
  return {
    claim: $("claim").value.trim(),
    preset: $("preset").value,
    source: $("source").value,
    limit: Number($("limit").value) || 40,
    lenses: Array.from(document.querySelectorAll('input[name="lens"]:checked'))
      .map((box) => box.value),
  };
}

// -----------------------------------------------------------------------
// Step 2: query review
// -----------------------------------------------------------------------

$("claim-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = readForm();

  if (!payload.lenses.length) {
    status("Select at least one lens. A scan with no lens asks no question.", true);
    return;
  }

  const button = $("btn-plan");
  button.disabled = true;
  button.textContent = "Building…";

  try {
    state.request = payload;
    state.plan = await api("/api/plan", {
      method: "POST",
      body: JSON.stringify({ claim: payload.claim, preset: payload.preset }),
    });
    renderPlan(state.plan);
    show("step-query");
    setStep(2);
    window.scrollTo({ top: 0, behavior: "smooth" });
    clearStatus();
  } catch (error) {
    status(error.message || "The query could not be built.", true);
  } finally {
    button.disabled = false;
    button.textContent = "Build query";
  }
});

function renderPlan(plan) {
  $("query-canonical").textContent = plan.canonical;
  chips($("query-anchors"), plan.anchors, "No anchors for this preset");
  chips($("query-terms"), plan.terms, "No terms extracted");
  chips($("query-excluded"), plan.excluded, "No exclusions applied");

  $("plan-warnings").innerHTML = (plan.warnings || []).length
    ? `<div class="banner warn"><strong>Before you run this</strong>${
        plan.warnings.map((text) => `<div>${esc(text)}</div>`).join("")
      }</div>`
    : "";
}

$("btn-back").addEventListener("click", () => {
  show("step-claim");
  setStep(1);
  $("claim").focus();
});

// -----------------------------------------------------------------------
// Step 3: scan
// -----------------------------------------------------------------------

$("btn-scan").addEventListener("click", async () => {
  const button = $("btn-scan");
  button.disabled = true;
  button.textContent = "Scanning…";
  status("Querying indexes. This may take several seconds.");

  try {
    const result = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify(state.request),
    });
    renderResult(result);
    show("step-result");
    setStep(3);
    window.scrollTo({ top: 0, behavior: "smooth" });
    clearStatus();
  } catch (error) {
    renderFailure(error);
    show("step-result");
    setStep(3);
    window.scrollTo({ top: 0, behavior: "smooth" });
  } finally {
    button.disabled = false;
    button.textContent = "Run the scan";
  }
});

/* A retrieval failure is rendered as a failure, never as a result.
   Presenting an unreachable index as 'no records found' would let an
   infrastructure fault stand as a finding about the literature. */
function renderFailure(error) {
  const isRetrieval = error.error === "retrieval_failed";
  const detail = error.errors
    ? `<ul>${Object.entries(error.errors)
        .map(([name, message]) => `<li><strong>${esc(name)}</strong> — ${esc(message)}</li>`)
        .join("")}</ul>`
    : "";

  $("result-body").innerHTML = `
    <div class="banner fail">
      <strong>${isRetrieval ? "The scan did not run" : "The scan did not complete"}</strong>
      ${esc(error.message || "An unexpected error occurred.")}
      ${detail}
    </div>
    <p class="lede">
      No conclusion about the literature follows from this. The claim has not been
      screened, and this outcome is not evidence that support is absent.
    </p>
    <div class="actions">
      <button type="button" class="secondary" onclick="location.reload()">Start again</button>
    </div>`;
}

function renderResult(data) {
  const machine = data.machine_output;
  const risk = data.translation_risk;
  const reading = data.interpretation;

  $("result-body").innerHTML = [
    renderBanners(data),
    renderJudgement(reading),
    renderSupport(machine, data.query.relaxed),
    renderMetrics(machine, data.retrieval),
    renderLenses(machine.lenses),
    renderRisk(risk),
    renderRecords(data.records, data.retrieval),
    renderProvenance(data),
    `<div class="actions">
       <button type="button" class="secondary" onclick="location.reload()">New scan</button>
     </div>`,
  ].join("");
}

/* Retrieval defects are shown at the top and are not collapsible. A support
   level computed over a partial corpus is a different quantity from one computed
   over the intended corpus, and the difference must not be discoverable only by
   expanding a section. */
function renderBanners(data) {
  const parts = [];

  if (data.retrieval.partial_failure) {
    parts.push(`
      <div class="banner fail">
        <strong>Partial retrieval — the corpus is incomplete</strong>
        ${esc(data.retrieval.sources_failed.join(", "))} could not be reached.
        Everything below rests on ${esc(data.retrieval.sources_used.join(", ") || "no source")}
        and understates whatever the missing index contains.
      </div>`);
  }

  if (data.query.relaxed) {
    parts.push(`
      <div class="banner warn">
        <strong>A broader query was executed</strong>
        The query you approved returned too few records, so a relaxed version ran in
        its place. This result answers a wider question than the one you posed.
      </div>`);
  }

  if (risky(data)) {
    parts.push(`
      <div class="banner warn">
        <strong>Risk detection ran at reduced sensitivity</strong>
        ${esc(String(data.translation_risk.degraded_records))} abstract(s) arrived without
        punctuation, so sentence boundaries could not be identified. Fewer flags here
        does not mean fewer risks in those records.
      </div>`);
  }

  return parts.join("");
}

function risky(data) {
  return Number(data.translation_risk.degraded_records) > 0;
}

/* Rendered above the support level, deliberately. In an earlier build these
   items sat beneath the score and reviewers reported reading the score alone. */
function renderJudgement(reading) {
  const items = reading.human_judgement_required || [];
  return `
    <div class="judgement">
      <h3>What this result does not settle</h3>
      <p class="none">${esc(reading.what_this_is_not)}</p>
      ${items.length ? `
        <p class="none" style="margin-top:10px"><strong>Outstanding human judgement:</strong></p>
        <ol>${items.map((text) => `<li>${esc(text)}</li>`).join("")}</ol>`
        : `<p class="none" style="margin-top:10px">
             No lens-specific verification was flagged. The result still reports
             vocabulary overlap only, and the sources remain unread.
           </p>`}
      ${reading.limiting_factor ? `
        <p class="none" style="margin-top:10px">
          <strong>Binding constraint:</strong> ${esc(reading.limiting_factor)}
        </p>` : ""}
    </div>`;
}

function renderSupport(machine, relaxed) {
  const captions = {
    strong: "The claim's vocabulary co-occurs widely in the retrieved abstracts. This is a property of the literature's wording, not a verification of the claim.",
    moderate: "Partial vocabulary overlap. Some retrieved work uses related terms; whether it addresses the claim requires reading.",
    weak: "Sparse overlap. The claim may be novel, may be phrased differently in the literature, or may be unsupported. These are not distinguishable here.",
    none: "No meaningful overlap. This is a statement about vocabulary in the retrieved records, not about the claim's validity.",
  };

  return `
    <div class="support">
      <span class="level" data-level="${esc(machine.support_level)}">${esc(machine.support_level.toUpperCase())}</span>
      <span class="score">score ${esc(machine.aggregate_score.toFixed(3))}</span>
      ${machine.downgraded ? '<span class="flag">Capped</span>' : ""}
      ${relaxed ? '<span class="flag">Relaxed query</span>' : ""}
      <p class="caption">${esc(captions[machine.support_level] || "")}</p>
      ${machine.downgrade_reason
        ? `<p class="caption"><strong>Cap applied:</strong> ${esc(machine.downgrade_reason)}</p>`
        : ""}
    </div>`;
}

function renderMetrics(machine, retrieval) {
  const cells = [
    ["Records scored", retrieval.records_scored],
    ["Direct hits", machine.direct_hits],
    ["Partial hits", machine.partial_hits],
    ["Term coverage", `${Math.round(machine.term_coverage * 100)}%`],
  ];
  const unmatched = (machine.unmatched_terms || []).length
    ? `<p class="hint" style="margin-top:-8px;margin-bottom:20px">
         Terms with no match in any record: ${esc(machine.unmatched_terms.join(", "))}.
         Consider whether the literature names these differently.
       </p>`
    : "";

  return `<div class="metrics">${
    cells.map(([name, value]) => `
      <div class="metric">
        <div class="value">${esc(String(value))}</div>
        <div class="name">${esc(name)}</div>
      </div>`).join("")
  }</div>${unmatched}`;
}

/* A lens carrying both supporting and contradicting vocabulary is marked as
   contested rather than shown as a middling bar. A single bar at the midpoint
   would represent disagreement and weak evidence identically. */
function renderLenses(lenses) {
  const cards = lenses.map((lens) => {
    const contested = lens.positive_matches > 0 && lens.negative_matches > 0;
    return `
      <div class="lens-card${contested ? " contested" : ""}">
        <div class="lens-head">
          <strong>${esc(lens.label)}${
            lens.expert_verification_required
              ? '<span class="needs-expert">Expert verification</span>' : ""
          }</strong>
          <span class="lens-meta">${esc(lens.score.toFixed(3))}</span>
        </div>
        <div class="lens-bar"><i style="width:${Math.round(lens.score * 100)}%"></i></div>
        <p class="lens-meta">
          ${esc(String(lens.positive_matches))} supporting,
          ${esc(String(lens.negative_matches))} contradicting term occurrence(s)
          ${lens.matched_terms.length ? ` · matched: ${esc(lens.matched_terms.join(", "))}` : ""}
          ${lens.contradicting_terms.length ? ` · contradicted by: ${esc(lens.contradicting_terms.join(", "))}` : ""}
        </p>
        ${lens.note ? `<p class="lens-note">${esc(lens.note)}</p>` : ""}
      </div>`;
  }).join("");

  return `
    <details class="block" open>
      <summary>Lens results (${lenses.length})</summary>
      <div>${cards}</div>
    </details>`;
}

function renderRisk(risk) {
  if (!risk.findings.length) {
    return `
      <details class="block" open>
        <summary>Translation risk — no patterns matched</summary>
        <div><p class="lede" style="margin:0">${esc(risk.summary)}</p></div>
      </details>`;
  }

  const findings = risk.findings.map((item) => `
    <div class="finding" data-severity="${esc(item.severity)}">
      <div class="finding-head">
        <span class="sev" data-severity="${esc(item.severity)}">${esc(item.severity)}</span>
        <strong>${esc(item.label)}</strong>
      </div>
      <p class="excerpt">${esc(item.excerpt)}</p>
      <p class="guidance">${esc(item.guidance)}</p>
      <p class="provenance">${esc(item.record_id)} · segmentation: ${esc(item.segmentation)}</p>
    </div>`).join("");

  return `
    <details class="block" open>
      <summary>Translation risk (${risk.findings.length} pattern match(es))</summary>
      <div>
        <p class="lede">${esc(risk.summary)}</p>
        ${findings}
      </div>
    </details>`;
}

function renderRecords(records, retrieval) {
  const rows = records.map((record) => `
    <div class="record">
      <a href="${esc(record.url)}" target="_blank" rel="noopener noreferrer">${esc(record.title)}</a>
      <p class="meta">${esc(record.source)} · ${esc(String(record.year || "year unknown"))} · ${esc(record.identifier)}</p>
      <p class="abs">${esc(record.abstract)}${record.abstract_truncated ? "…" : ""}</p>
    </div>`).join("");

  return `
    <details class="block">
      <summary>Retrieved records (showing ${records.length} of ${retrieval.records_scored} scored)</summary>
      <div>
        <p class="hint" style="margin-bottom:12px">
          Abstracts are shown so that a flag can be checked against its source without
          leaving the page. They are not a substitute for the full text.
        </p>
        ${rows || "<p>No records to display.</p>"}
      </div>
    </details>`;
}

function renderProvenance(data) {
  const counts = Object.entries(data.retrieval.counts)
    .map(([name, value]) => `<li>${esc(name)}: ${esc(String(value))}</li>`).join("");
  const notes = (data.notes || []).length
    ? `<h4 style="margin-top:14px">Notes recorded in the audit entry</h4>
       <ul>${data.notes.map((text) => `<li>${esc(text)}</li>`).join("")}</ul>`
    : "";

  return `
    <details class="block">
      <summary>Provenance</summary>
      <div>
        <h4>Query executed</h4>
        <pre style="white-space:pre-wrap;font-size:12.5px">${esc(data.query.executed)}</pre>
        <h4 style="margin-top:14px">Record counts at each stage</h4>
        <ul>${counts}</ul>
        ${notes}
        <p class="hint" style="margin-top:14px">
          Audit entry <code>${esc(data.audit.entry_id)}</code> ·
          completed in ${esc(String(data.elapsed_ms))} ms ·
          sources: ${esc(data.retrieval.sources_used.join(", ") || "none")}
        </p>
      </div>
    </details>`;
}

// -----------------------------------------------------------------------
// Audit
// -----------------------------------------------------------------------

$("btn-verify").addEventListener("click", async () => {
  try {
    const report = await api("/api/audit/verify");
    $("audit-out").innerHTML = `
      <div class="banner ${report.valid ? "warn" : "fail"}"
           style="${report.valid ? "background:var(--accent-soft);border-color:var(--accent);color:var(--accent)" : ""}">
        <strong>${report.valid ? "Chain intact" : "Chain broken"}</strong>
        ${esc(report.detail)}
        ${report.first_break !== null
          ? ` Entries before position ${esc(String(report.first_break))} remain verifiable.`
          : ""}
      </div>`;
  } catch (error) {
    status(error.message || "Verification failed.", true);
  }
});

$("btn-recent").addEventListener("click", async () => {
  try {
    const { entries } = await api("/api/audit/recent?limit=20");
    if (!entries.length) {
      $("audit-out").innerHTML = '<p class="hint">No scans have been recorded yet.</p>';
      return;
    }

    /* Expert reviews are listed alongside the scans they concern rather than
       folded into them. A verdict is a human conclusion; merging it into the
       machine entry would make the two indistinguishable on inspection. */
    const rows = entries.map((entry) => {
      const isReview = entry.kind === "expert_review";
      return `
        <tr>
          <td><code>${esc(entry.entry_id)}</code></td>
          <td>${esc((entry.timestamp || "").replace("T", " ").slice(0, 19))}</td>
          <td>${isReview
            ? `<em>review of ${esc(entry.reviews_entry || "—")}</em>`
            : esc(truncate(entry.claim, 70))}</td>
          <td>${isReview
            ? `<strong>${esc(entry.verdict || "—")}</strong>`
            : esc(entry.support_level || "—")}</td>
          <td>${isReview ? esc(entry.reviewer || "—") : esc(entry.preset || "—")}</td>
        </tr>`;
    }).join("");

    $("audit-out").innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Entry</th><th>Timestamp (UTC)</th>
            <th>Claim / subject</th><th>Level / verdict</th><th>Preset / reviewer</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
      <p class="hint" style="margin-top:10px">
        Listing does not verify. Use chain verification to confirm that these entries
        have not been altered since they were written.
      </p>`;
  } catch (error) {
    status(error.message || "The audit log could not be read.", true);
  }
});

function truncate(text, length) {
  const value = String(text ?? "");
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

// -----------------------------------------------------------------------
// Boot
// -----------------------------------------------------------------------

/* If configuration cannot be loaded the form is disabled rather than shown with
   empty selectors. A form offering no presets and no lenses would accept input
   and fail at submission, after the user had committed effort to the claim. */
loadConfig().catch((error) => {
  document.querySelectorAll("#claim-form input, #claim-form select, #claim-form textarea, #claim-form button")
    .forEach((node) => { node.disabled = true; });
  $("claim-form").insertAdjacentHTML("beforebegin", `
    <div class="banner fail">
      <strong>The tool could not start</strong>
      Domain configuration is unavailable, so no scan can be specified.
      ${esc(error.message || "")}
    </div>`);
});

setStep(1);