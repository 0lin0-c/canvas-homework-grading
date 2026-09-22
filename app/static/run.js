let RUN = window.RUN_DATA;
const RUN_ID = window.RUN_ID;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || res.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

function render() {
  const filter = document.getElementById("filter").value;
  const tbody = document.getElementById("students-body");
  tbody.innerHTML = "";
  const students = [...(RUN.students || {})];
  for (const s of students) {
    const submitted = !!(s.submission && (s.submission.submitted_at || (s.submission.attachments || []).length));
    if (filter === "submitted" && !submitted) continue;
    if (filter === "skipped" && s.status !== "skipped") continue;
    if (filter === "needs_review" && s.status !== "needs_review") continue;
    if (filter === "proposed" && !(s.status === "proposed" || (s.force_authorized && s.final_score != null))) continue;
    if (filter === "failed" && !["failed", "partial", "conflict", "unknown_after_write"].includes(s.status)) continue;

    const tr = document.createElement("tr");
    const canSelect = s.status === "proposed" || s.force_authorized || ["partial", "failed", "grade_verified", "comment_verified"].includes(s.status);
    tr.innerHTML = `
      <td><input type="checkbox" class="sel" data-sid="${s.student_id}" ${canSelect ? "" : "disabled"} /></td>
      <td>${escapeHtml(s.student_name || s.student_id)}<div class="muted">${s.student_id}</div></td>
      <td>${submitted ? (s.submission.submitted_at || "已交") : "未提交"}</td>
      <td>${s.proposed_score == null ? "—" : s.proposed_score}</td>
      <td>${s.final_score == null ? "—" : s.final_score}</td>
      <td title="${escapeHtml(s.final_comment || s.proposed_comment || "")}">${escapeHtml(trunc(s.final_comment || s.proposed_comment || "", 40))}</td>
      <td>${s.overall_confidence == null ? "—" : Number(s.overall_confidence).toFixed(2)}</td>
      <td><span class="badge ${s.status}">${s.status}</span>${(s.review_reasons||[]).length ? `<div class="muted">${escapeHtml(s.review_reasons.join(", "))}</div>` : ""}</td>
      <td>${s.writeback_step || "idle"}${s.writeback_error ? `<div class="warn">${escapeHtml(s.writeback_error)}</div>` : ""}</td>
      <td><button type="button" class="secondary btn-detail" data-sid="${s.student_id}">详情</button></td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll(".btn-detail").forEach((btn) => {
    btn.addEventListener("click", () => openDetail(btn.dataset.sid));
  });
}

function trunc(s, n) { return s.length > n ? s.slice(0, n) + "…" : s; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;" }[c]));
}

function openDetail(sid) {
  const s = RUN.students.find((x) => x.student_id === sid);
  if (!s) return;
  document.getElementById("dlg-sid").value = sid;
  document.getElementById("dlg-title").textContent = s.student_name || sid;
  document.getElementById("dlg-meta").textContent = JSON.stringify({
    status: s.status,
    review_reasons: s.review_reasons,
    questions: s.questions,
    platform_before: s.platform_before,
    platform_after: s.platform_after,
    submission: { ...s.submission, attachments: (s.submission.attachments || []).map(a => ({ id: a.id, name: a.name, page_count: a.page_count })) },
    proposed_score: s.proposed_score,
    proposed_comment: s.proposed_comment,
    overall_confidence: s.overall_confidence,
  }, null, 2);
  document.getElementById("dlg-score").value = s.final_score ?? s.proposed_score ?? "";
  document.getElementById("dlg-comment").value = s.final_comment ?? s.proposed_comment ?? "";
  document.getElementById("dlg-force").checked = !!s.force_authorized;
  document.getElementById("dlg-msg").textContent = "";
  document.getElementById("detail-dlg").showModal();
}

document.getElementById("dlg-save").addEventListener("click", async () => {
  const sid = document.getElementById("dlg-sid").value;
  const body = {
    final_score: document.getElementById("dlg-score").value === "" ? null : Number(document.getElementById("dlg-score").value),
    final_comment: document.getElementById("dlg-comment").value,
    force_authorized: document.getElementById("dlg-force").checked,
  };
  try {
    const updated = await api(`/api/runs/${RUN_ID}/students/${sid}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
    const idx = RUN.students.findIndex((x) => x.student_id === sid);
    RUN.students[idx] = updated;
    document.getElementById("dlg-msg").textContent = "已保存";
    render();
  } catch (err) {
    document.getElementById("dlg-msg").textContent = err.message;
  }
});

document.getElementById("dlg-retry").addEventListener("click", async () => {
  const sid = document.getElementById("dlg-sid").value;
  try {
    const r = await api(`/api/runs/${RUN_ID}/writeback/${sid}/retry`, { method: "POST", body: "{}" });
    document.getElementById("dlg-msg").textContent = JSON.stringify(r);
    RUN = await api(`/api/runs/${RUN_ID}`);
    render();
  } catch (err) {
    document.getElementById("dlg-msg").textContent = err.message;
  }
});

document.getElementById("btn-grade").addEventListener("click", async () => {
  const msg = document.getElementById("run-msg");
  msg.textContent = "批改中…";
  try {
    RUN = await api(`/api/runs/${RUN_ID}/grade`, { method: "POST", body: "{}" });
    msg.textContent = "批改完成（高置信度已排前）";
    render();
  } catch (err) {
    msg.textContent = err.message;
  }
});

document.getElementById("btn-mode").addEventListener("click", async () => {
  const mode = document.getElementById("mode-select").value;
  const fd = new FormData();
  fd.append("mode", mode);
  const res = await fetch(`/api/runs/${RUN_ID}/mode`, { method: "POST", body: fd });
  const data = await res.json();
  document.getElementById("run-mode").textContent = data.mode;
  RUN.mode = data.mode;
  document.getElementById("run-msg").textContent = `模式已切换为 ${data.mode}`;
});

document.getElementById("btn-writeback").addEventListener("click", async () => {
  const ids = [...document.querySelectorAll(".sel:checked")].map((el) => el.dataset.sid);
  const msg = document.getElementById("run-msg");
  if (!ids.length) {
    msg.textContent = "请先勾选学生";
    return;
  }
  if (ids.length > window.WRITEBACK_MAX_BATCH) {
    msg.textContent = `门控：最多 ${window.WRITEBACK_MAX_BATCH} 人/批（当前 WRITEBACK_MAX_BATCH）`;
    return;
  }
  if (!confirm(`确认写回 ${ids.length} 人？\n评语对学生可见。\n模式=${RUN.mode}`)) return;
  try {
    const result = await api(`/api/runs/${RUN_ID}/writeback`, {
      method: "POST",
      body: JSON.stringify({ student_ids: ids, confirm: true }),
    });
    msg.textContent = (result.dry_run ? "[dry-run] " : "") + JSON.stringify(result.results);
    RUN = await api(`/api/runs/${RUN_ID}`);
    render();
  } catch (err) {
    msg.textContent = err.message;
  }
});

document.getElementById("btn-refresh").addEventListener("click", async () => {
  RUN = await api(`/api/runs/${RUN_ID}`);
  render();
});

document.getElementById("filter").addEventListener("change", render);
document.getElementById("check-all").addEventListener("change", (e) => {
  document.querySelectorAll(".sel:not(:disabled)").forEach((el) => { el.checked = e.target.checked; });
});

render();
