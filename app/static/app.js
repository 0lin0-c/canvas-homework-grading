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

const vaultForm = document.getElementById("vault-form");
if (vaultForm) {
  vaultForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = document.getElementById("vault-msg");
    const token = new FormData(vaultForm).get("token");
    msg.textContent = "验证中…";
    try {
      const st = await api("/api/credentials", {
        method: "POST",
        body: JSON.stringify({ token }),
      });
      msg.textContent = st.valid
        ? `已保存并验证：${st.user_name || ""} (${st.hint})`
        : `已保存（未能在线验证）：${st.hint}`;
      vaultForm.reset();
    } catch (err) {
      msg.textContent = "失败：" + err.message;
    }
  });
  document.getElementById("vault-clear").addEventListener("click", async () => {
    await api("/api/credentials", { method: "DELETE" });
    document.getElementById("vault-msg").textContent = "已清除";
    location.reload();
  });
}

const modelKeyForm = document.getElementById("model-key-form");
if (modelKeyForm) {
  modelKeyForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = document.getElementById("model-key-msg");
    const apiKey = new FormData(modelKeyForm).get("api_key");
    msg.textContent = "保存中…";
    try {
      const st = await api("/api/credentials/model", {
        method: "POST",
        body: JSON.stringify({ api_key: apiKey }),
      });
      msg.textContent = st.present ? `已保存（${st.hint}）` : "已保存";
      modelKeyForm.reset();
      location.reload();
    } catch (err) {
      msg.textContent = "失败：" + err.message;
    }
  });
  document.getElementById("model-key-clear").addEventListener("click", async () => {
    await api("/api/credentials/model", { method: "DELETE" });
    document.getElementById("model-key-msg").textContent = "已清除";
    location.reload();
  });
}

const answerSave = document.getElementById("answer-save");
if (answerSave) {
  answerSave.addEventListener("click", async () => {
    const text = document.getElementById("answer-text").value;
    const msg = document.getElementById("answer-msg");
    try {
      await api("/api/question-bank/answers", {
        method: "POST",
        body: JSON.stringify({ text, chapter: "习题一" }),
      });
      msg.textContent = "答案已保存到 local/answers/";
    } catch (err) {
      msg.textContent = err.message;
    }
  });
  document.getElementById("answer-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    fd.append("chapter", "习题一");
    const res = await fetch("/api/question-bank/answers/upload", { method: "POST", body: fd });
    document.getElementById("answer-msg").textContent = res.ok ? "上传成功" : "上传失败";
  });
}

const btnResolve = document.getElementById("btn-resolve");
if (btnResolve) {
  btnResolve.addEventListener("click", async () => {
    const url = document.getElementById("assignment-url").value;
    const out = document.getElementById("resolve-out");
    try {
      const data = await api("/api/assignments/resolve", {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      out.textContent = JSON.stringify(data, null, 2);
    } catch (err) {
      out.textContent = err.message;
    }
  });

  document.getElementById("btn-create").addEventListener("click", async () => {
    const msg = document.getElementById("assign-msg");
    msg.textContent = "拉取提交中…";
    try {
      const data = await api("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          assignment_url: document.getElementById("assignment-url").value,
          mode: "suggest",
          download_attachments: document.getElementById("dl-att").checked,
        }),
      });
      msg.textContent = `已创建 run ${data.id}，共 ${data.students.length} 人`;
      location.href = `/runs/${data.id}`;
    } catch (err) {
      msg.textContent = err.message;
    }
  });
}
