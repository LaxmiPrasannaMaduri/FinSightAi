const API = "";  // same origin as the backend
const NO_CACHE = { cache: "no-store" };   // never let the browser serve stale API data

const uploadForm = document.getElementById("uploadForm");
const uploadStatus = document.getElementById("uploadStatus");
const summaryDiv = document.getElementById("summary");
const askForm = document.getElementById("askForm");
const chatLog = document.getElementById("chatLog");
const txnTableBody = document.querySelector("#txnTable tbody");
const retrainBtn = document.getElementById("retrainBtn");
const monthSelect = document.getElementById("monthSelect");
const pieCredit = document.getElementById("pieCredit");
const pieDebit = document.getElementById("pieDebit");
const creditTotal = document.getElementById("creditTotal");
const debitTotal = document.getElementById("debitTotal");

let CATEGORIES = [];

/* ------------------------------------------------------------------ helpers */

const inr = (n) => "₹" + Math.round(n).toLocaleString("en-IN");

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

async function getJSON(path) {
  const res = await fetch(`${API}${path}`, NO_CACHE);
  if (!res.ok) throw new Error(`${path} failed (${res.status})`);
  return res.json();
}

/** Re-draw everything that depends on the stored transactions. */
function refreshAll() {
  return Promise.all([refreshSummary(), refreshTransactions(), refreshCharts(), refreshPlan()]);
}

async function loadCategories() {
  try { CATEGORIES = await getJSON("/api/categories"); } catch (e) { /* backend not up yet */ }
}

/* ------------------------------------------------------------------- upload */

uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = document.getElementById("fileInput").files[0];
  const password = document.getElementById("pdfPassword").value;
  if (!file) return;

  const fd = new FormData();
  fd.append("file", file);
  if (password) fd.append("password", password);

  uploadStatus.innerHTML = "Processing statement…";
  try {
    const res = await fetch(`${API}/api/upload`, { method: "POST", body: fd, cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed");

    uploadStatus.innerHTML =
      `<span class="status-ok">✓ Loaded ${data.inserted} transaction(s) from "${escapeHtml(data.filename)}" ` +
      `(replaced previous statement's data)</span>`;
    resetPlanInputs();
    await refreshAll();
  } catch (err) {
    uploadStatus.innerHTML = `<span class="status-err">✗ ${escapeHtml(err.message)}</span>`;
  }
});

/* --------------------------------------------------------------------- chat */

askForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("questionInput");
  const question = input.value.trim();
  if (!question) return;

  appendChat("q", question);
  input.value = "";

  const fd = new FormData();
  fd.append("question", question);
  try {
    const res = await fetch(`${API}/api/ask`, { method: "POST", body: fd, cache: "no-store" });
    const data = await res.json();
    appendChat("a", data.answer || "No answer returned.");
  } catch (err) {
    appendChat("a", "Error reaching the server: " + err.message);
  }
});

function appendChat(role, text) {
  const div = document.createElement("div");
  div.className = role === "q" ? "chat-q" : "chat-a";
  div.textContent = (role === "q" ? "You: " : "") + text;
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

/* ------------------------------------------------------------------ summary */

async function refreshSummary() {
  const data = await getJSON("/api/summary");
  if (!data.transaction_count) {
    summaryDiv.innerHTML = "Upload a statement to see your spending summary.";
    return;
  }
  const catRows = Object.entries(data.by_category)
    .sort((a, b) => b[1] - a[1])
    .map(([cat, total]) => `
      <div class="summary-item">
        <div class="label">${escapeHtml(cat)}</div>
        <div class="value">${inr(total)}</div>
      </div>`).join("");

  summaryDiv.innerHTML = `
    <div class="summary-grid">
      <div class="summary-item"><div class="label">Total Spend</div><div class="value">${inr(data.total_spend)}</div></div>
      <div class="summary-item"><div class="label">Total Income</div><div class="value">${inr(data.total_income)}</div></div>
      <div class="summary-item"><div class="label">Transactions</div><div class="value">${data.transaction_count}</div></div>
    </div>
    <div class="summary-grid" style="margin-top:10px;">${catRows}</div>
  `;
}

/* ------------------------------------------------- monthly credit/debit pies */

const PALETTE = [
  "#2f6d4f", "#a8442c", "#1b6b8c", "#b9791a", "#6b4e9b", "#3d8b8b", "#c4577a", "#7a8f2a",
  "#8a5a3c", "#4c6fa5", "#b0873a", "#5b8c5a", "#9c4f4f", "#4a7c8c", "#8d6e97", "#6f6f6f",
];

function colorFor(category) {
  let i = CATEGORIES.indexOf(category);
  if (i < 0) {  // unknown category: derive a stable colour from its name
    i = [...category].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 7);
  }
  return PALETTE[i % PALETTE.length];
}

function polar(cx, cy, r, angle) {
  return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
}

/** Draws a pie (plain SVG, no library) + a legend into `el`. */
function renderPie(el, data, noun) {
  const entries = Object.entries(data || {}).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, v]) => sum + v, 0);

  if (!total) {
    el.innerHTML = `<div class="pie-empty">No ${noun} in this period.</div>`;
    return;
  }

  const cx = 100, cy = 100, r = 94;
  let angle = -Math.PI / 2;          // start at 12 o'clock
  let slices = "";

  for (const [cat, val] of entries) {
    const frac = val / total;
    const color = colorFor(cat);
    const tip = escapeHtml(`${cat}: ${inr(val)} (${(frac * 100).toFixed(1)}%)`);

    if (frac > 0.9999) {             // a single category: an arc can't draw a full circle
      slices += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}"><title>${tip}</title></circle>`;
    } else {
      const next = angle + frac * 2 * Math.PI;
      const [x1, y1] = polar(cx, cy, r, angle);
      const [x2, y2] = polar(cx, cy, r, next);
      const large = frac > 0.5 ? 1 : 0;
      slices += `<path d="M${cx},${cy} L${x1.toFixed(2)},${y1.toFixed(2)} ` +
                `A${r},${r} 0 ${large} 1 ${x2.toFixed(2)},${y2.toFixed(2)} Z" ` +
                `fill="${color}" stroke="#f7f8f2" stroke-width="1.5"><title>${tip}</title></path>`;
      angle = next;
    }
  }

  const legend = entries.map(([cat, val]) => `
    <li>
      <span class="swatch" style="background:${colorFor(cat)}"></span>
      <span class="name" title="${escapeHtml(cat)}">${escapeHtml(cat)}</span>
      <span class="amt">${inr(val)}</span>
      <span class="pct">${((val / total) * 100).toFixed(0)}%</span>
    </li>`).join("");

  el.innerHTML = `
    <svg viewBox="0 0 200 200" role="img" aria-label="${noun} by category">${slices}</svg>
    <ul class="pie-legend">${legend}</ul>`;
}

async function refreshCharts() {
  const months = await getJSON("/api/months");
  const previous = monthSelect.value || "all";

  monthSelect.innerHTML = `<option value="all">All months</option>` +
    months.map(m => `<option value="${m.value}">${escapeHtml(m.label)}</option>`).join("");
  monthSelect.value = months.some(m => m.value === previous) ? previous : "all";
  monthSelect.disabled = months.length === 0;

  await loadBreakdown();
}

async function loadBreakdown() {
  const month = monthSelect.value || "all";
  const data = await getJSON(`/api/breakdown?month=${encodeURIComponent(month)}`);
  creditTotal.textContent = data.count ? inr(data.total_credit) : "–";
  debitTotal.textContent = data.count ? inr(data.total_debit) : "–";
  renderPie(pieCredit, data.credit, "credits");
  renderPie(pieDebit, data.debit, "debits");
}

monthSelect.addEventListener("change", () => loadBreakdown().catch(console.error));

/* ------------------------------------------------------------- transactions */

async function refreshTransactions() {
  const rows = await getJSON("/api/transactions");
  txnTableBody.innerHTML = "";

  for (const t of rows) {
    const tr = document.createElement("tr");
    const dateStr = new Date(t.date).toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
    const confClass = t.category_confidence < 0.5 ? "confidence-low" : "";

    tr.innerHTML = `
      <td>${dateStr}</td>
      <td>${escapeHtml(t.description)}</td>
      <td class="dir-${t.direction}">${inr(t.amount)}</td>
      <td class="dir-${t.direction}">${t.direction}</td>
      <td>${escapeHtml(t.category)}</td>
      <td class="${confClass}">${Math.round(t.category_confidence * 100)}%</td>
      <td></td>
    `;

    const select = document.createElement("select");
    select.className = "cat-fix";
    select.innerHTML = `<option value="">Fix…</option>` +
      CATEGORIES.map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
    select.addEventListener("change", async () => {
      if (!select.value) return;
      await correctCategory(t.id, select.value);
      await refreshAll();            // summary, table AND pie charts
    });
    tr.lastElementChild.appendChild(select);

    txnTableBody.appendChild(tr);
  }
}

async function correctCategory(transactionId, category) {
  const fd = new FormData();
  fd.append("transaction_id", transactionId);
  fd.append("correct_category", category);
  await fetch(`${API}/api/correct`, { method: "POST", body: fd, cache: "no-store" });
}

/* ---------------------------------------------- savings plan + expense tracker */

const planForm = document.getElementById("planForm");
const planBalance = document.getElementById("planBalance");
const planGoal = document.getElementById("planGoal");
const planSubmit = document.getElementById("planSubmit");
const planResetBtn = document.getElementById("planResetBtn");
const planMsgEl = document.getElementById("planMessage");
const planBody = document.getElementById("planBody");
const planHeadline = document.getElementById("planHeadline");
const planTiles = document.getElementById("planTiles");
const planTrack = document.getElementById("planTrack");
const planTableBody = document.querySelector("#planTable tbody");
const expenseForm = document.getElementById("expenseForm");
const expenseCategory = document.getElementById("expenseCategory");
const expenseAmount = document.getElementById("expenseAmount");
const expenseNote = document.getElementById("expenseNote");
const expenseList = document.getElementById("expenseList");

/** Rupees, keeping paise only when there are any (₹1,000 / ₹10.17). */
function money(n) {
  const whole = Number.isInteger(n);
  return "₹" + n.toLocaleString("en-IN", { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2 });
}

// Don't overwrite a field the user is typing into when the page refreshes.
[planBalance, planGoal].forEach(el => el.addEventListener("input", () => { el.dataset.touched = "1"; }));
function resetPlanInputs() { delete planBalance.dataset.touched; delete planGoal.dataset.touched; }
function setIfUntouched(el, value) { if (!el.dataset.touched) el.value = value ?? ""; }

function planMsg(text, kind, isHint = false) {
  planMsgEl.className = kind ? `status-${kind}` : "";
  planMsgEl.textContent = text || "";
  if (isHint) planMsgEl.dataset.hint = "1"; else delete planMsgEl.dataset.hint;
}

async function planRequest(path, options) {
  const res = await fetch(`${API}${path}`, { ...options, cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Something went wrong");
  return data;
}

async function refreshPlan() {
  try {
    renderPlan(await getJSON("/api/plan"));
  } catch (e) {
    planMsg("Couldn't load the savings plan. Make sure the backend (app.py) is updated and restarted.", "err");
  }
}

function renderPlan(data) {
  const plan = data.plan;
  planResetBtn.hidden = !plan;
  planSubmit.textContent = plan ? "Update plan" : "Create plan";

  if (plan) {
    setIfUntouched(planBalance, plan.balance);
    setIfUntouched(planGoal, plan.savings_goal);
  } else {
    setIfUntouched(planBalance, data.statement_balance != null ? data.statement_balance.toFixed(2) : "");
    setIfUntouched(planGoal, "");
  }

  if (!data.has_statement) {
    planMsg("Upload a statement first: your balance and usual spending come from it.", "", true);
  } else if (planMsgEl.dataset.hint) {
    planMsg("");
  }

  planBody.hidden = !plan;
  if (!plan) return;

  planHeadline.innerHTML =
    `After saving <strong>${money(plan.savings_goal)}</strong> you have ` +
    `<strong>${money(plan.spendable)}</strong> left to spend. Spend it like this:`;

  const left = plan.left_to_spend;
  planTiles.innerHTML = [
    ["Balance", money(plan.balance), ""],
    ["To save", money(plan.savings_goal), ""],
    ["Can spend", money(plan.spendable), ""],
    ["Spent so far", money(plan.total_spent), ""],
    ["Left to spend", money(left), left < 0 ? "dir-debit" : "dir-credit"],
  ].map(([label, value, cls]) => `
    <div class="summary-item"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`).join("");

  if (plan.on_track) {
    planTrack.className = "plan-track";
    planTrack.innerHTML = `On track. You'd keep <strong>${money(plan.projected_savings)}</strong>, ` +
      `which covers your ${money(plan.savings_goal)} savings goal.`;
  } else {
    planTrack.className = "plan-track at-risk";
    planTrack.innerHTML = `Over your limit. You'd keep only <strong>${money(plan.projected_savings)}</strong>, ` +
      `<strong>${money(plan.savings_goal - plan.projected_savings)}</strong> short of your ${money(plan.savings_goal)} goal.`;
  }

  planTableBody.innerHTML = plan.rows.map(r => {
    const pct = Math.min(100, r.percent);
    const leftText = r.left < 0 ? `over by ${money(-r.left)}` : money(r.left);
    return `
      <tr class="row-${r.status}">
        <td>${escapeHtml(r.category)}</td>
        <td class="num">${money(r.budget)}</td>
        <td class="num">${money(r.spent)}</td>
        <td class="num left-cell">${leftText}</td>
        <td class="bar-col">
          <div class="bar" role="progressbar" aria-label="${escapeHtml(r.category)} budget used"
               aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}"><span style="width:${pct}%"></span></div>
          <div class="bar-label">${Math.round(r.percent)}% used</div>
        </td>
      </tr>`;
  }).join("");

  // expense form: keep the chosen category across refreshes
  const chosen = expenseCategory.value;
  expenseCategory.innerHTML = (data.categories || [])
    .map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
  expenseCategory.value = chosen && (data.categories || []).includes(chosen) ? chosen : (plan.rows[0]?.category ?? "");

  expenseList.innerHTML = plan.expenses.length ? plan.expenses.map(e => {
    const when = new Date(e.date + "Z").toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
    return `
      <li>
        <span class="when">${when}</span>
        <span class="what">${escapeHtml(e.category)}${e.note ? `<small>${escapeHtml(e.note)}</small>` : ""}</span>
        <span class="amt">${money(e.amount)}</span>
        <button type="button" data-expense-id="${e.id}" aria-label="Remove ${money(e.amount)} ${escapeHtml(e.category)} expense">✕</button>
      </li>`;
  }).join("") : `<li class="expense-empty">No expenses logged yet.</li>`;
}

planForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("savings_goal", planGoal.value);
  if (planBalance.value !== "") fd.append("balance", planBalance.value);
  try {
    const data = await planRequest("/api/plan", { method: "POST", body: fd });
    resetPlanInputs();
    renderPlan(data);
    planMsg("Plan saved. Your spending limits are below.", "ok");
  } catch (err) {
    planMsg(err.message, "err");
  }
});

expenseForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData();
  fd.append("category", expenseCategory.value);
  fd.append("amount", expenseAmount.value);
  fd.append("note", expenseNote.value);
  try {
    const data = await planRequest("/api/plan/expense", { method: "POST", body: fd });
    const category = expenseCategory.value;
    expenseAmount.value = "";
    expenseNote.value = "";
    renderPlan(data);

    const row = data.plan.rows.find(r => r.category === category);
    if (row && row.status === "over") {
      planMsg(`Over budget on ${category} by ${money(-row.left)}.`, "err");
    } else if (row && row.status === "warn") {
      planMsg(`Added. ${category} is ${Math.round(row.percent)}% used; ${money(row.left)} left.`, "ok");
    } else {
      planMsg(`Added to ${category}. ${money(data.plan.left_to_spend)} left to spend overall.`, "ok");
    }
  } catch (err) {
    planMsg(err.message, "err");
  }
});

expenseList.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-expense-id]");
  if (!btn) return;
  try {
    renderPlan(await planRequest(`/api/plan/expense/${btn.dataset.expenseId}`, { method: "DELETE" }));
    planMsg("Expense removed.", "ok");
  } catch (err) {
    planMsg(err.message, "err");
  }
});

planResetBtn.addEventListener("click", async () => {
  if (!confirm("Reset your savings plan and all logged expenses?")) return;
  try {
    resetPlanInputs();
    renderPlan(await planRequest("/api/plan", { method: "DELETE" }));
    planMsg("Plan cleared.", "ok");
  } catch (err) {
    planMsg(err.message, "err");
  }
});

/* ------------------------------------------------------------------ retrain */

retrainBtn.addEventListener("click", async () => {
  retrainBtn.textContent = "Retraining…";
  retrainBtn.disabled = true;
  try {
    const res = await fetch(`${API}/api/retrain`, { method: "POST", cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "retrain failed");
    await refreshAll();
    retrainBtn.textContent = `✓ Retrained (${data.recategorized ?? 0} updated)`;
  } catch (e) {
    retrainBtn.textContent = "✗ Failed";
  } finally {
    setTimeout(() => { retrainBtn.textContent = "↻ Retrain model"; retrainBtn.disabled = false; }, 2500);
  }
});

/* --------------------------------------------------------------------- init */

(async function init() {
  await loadCategories();      // needed first: the category dropdowns + pie colours use it
  try { await refreshAll(); } catch (e) { console.error(e); }
})();
