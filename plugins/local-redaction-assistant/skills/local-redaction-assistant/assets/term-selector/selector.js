(() => {
  const token = new URLSearchParams(window.location.search).get("token") || "";
  const parameters = new URLSearchParams({ token });
  const grid = document.getElementById("term-grid");
  const status = document.getElementById("status");
  const confirm = document.getElementById("confirm-selection");
  const cancel = document.getElementById("cancel-selection");
  let labels = { persons: "姓名", companies: "公司", addresses: "地址" };
  let categories = ["persons", "companies", "addresses"];
  let session = null;

  const endpoint = (path) => `${path}?${parameters.toString()}`;
  const setStatus = (message, isError = false) => { status.textContent = message; status.classList.toggle("error", isError); };

  function card(category, candidates) {
    const section = document.createElement("section");
    section.className = "term-card";
    section.dataset.category = category;
    const heading = document.createElement("h2");
    heading.textContent = labels[category];
    section.append(heading);
    const list = document.createElement("div");
    list.className = "candidate-list";
    if (candidates.length) {
      candidates.forEach((candidate) => {
        const value = typeof candidate === "string" ? candidate : candidate.display;
        const occurrenceId = typeof candidate === "string" ? candidate : candidate.id;
        const label = document.createElement("label");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.value = occurrenceId;
        if (candidate.replacement_supported === false) box.disabled = true;
        const text = document.createElement("span");
        const scope = candidate.part_scope ? ` · ${candidate.part_scope}` : "";
        const manual = candidate.replacement_supported === false ? " · 需人工处理" : "";
        text.textContent = `${value}${scope}${manual}`;
        label.append(box, text);
        list.append(label);
      });
    } else {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "未发现可确认候选。可在下方补充。";
      list.append(empty);
    }
    const add = document.createElement("div");
    add.className = "add-row";
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 80;
    input.placeholder = `补充${labels[category]}后按回车`;
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const value = input.value.trim();
      if (!value) return;
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = value;
      box.checked = true;
      box.dataset.addition = "true";
      const text = document.createElement("span");
      text.textContent = value;
      label.append(box, text);
      list.append(label);
      input.value = "";
    });
    add.append(input);
    section.append(list, add);
    return section;
  }

  function selection() {
    const selected = {}; const additions = {};
    categories.forEach((category) => {
      selected[category] = []; additions[category] = [];
      grid.querySelectorAll(`section[data-category="${category}"] input[type="checkbox"]:checked`).forEach((box) => {
        (box.dataset.addition ? additions[category] : selected[category]).push(box.value);
      });
    });
    const tableActions = {};
    document.querySelectorAll("select[data-table-id]").forEach((select) => {
      tableActions[select.dataset.tableId] = select.value;
    });
    return { selected, additions, table_actions: tableActions };
  }

  async function post(path, payload = {}) {
    const response = await fetch(endpoint(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), cache: "no-store" });
    if (!response.ok) throw new Error("request_failed");
  }

  confirm.addEventListener("click", async () => {
    confirm.disabled = true;
    try {
      await post("/confirm", selection());
      setStatus("已确认。本机正在生成脱敏副本；请返回工具等待完成。");
    } catch (_) {
      confirm.disabled = false;
      setStatus("确认失败，未生成副本。请检查至少确认或补充一个词项。", true);
    }
  });
  cancel.addEventListener("click", async () => {
    try { await post("/cancel"); } catch (_) { /* local cancellation is best effort */ }
    setStatus("已取消，未生成副本。");
  });

  fetch(endpoint("/session"), { cache: "no-store" })
    .then((response) => response.json())
    .then((data) => {
      session = data;
      labels = data.category_labels || labels;
      categories = data.redaction_profile === "legal-template"
        ? Object.keys(labels)
        : ["persons", "companies", "addresses"];
      document.getElementById("file-meta").textContent = `${session.file_id} · DOCX`;
      categories.forEach((category) => grid.append(card(category, data.candidate_terms[category] || [])));
      const associations = data.candidate_associations || [];
      if (associations.length) {
        const panel = document.getElementById("candidate-associations");
        const heading = document.createElement("h2");
        heading.textContent = "主体全称与简称关联候选";
        panel.append(heading);
        associations.forEach((item) => {
          const row = document.createElement("p");
          row.textContent = `${item.full_name} ↔ ${item.alias}`;
          panel.append(row);
        });
        panel.hidden = false;
      }
      const tables = data.table_candidates || [];
      if (tables.length) {
        const panel = document.getElementById("table-actions");
        const heading = document.createElement("h2");
        heading.textContent = "敏感表格逐表处理";
        panel.append(heading);
        tables.forEach((table) => {
          const row = document.createElement("label");
          row.className = "table-action-row";
          const text = document.createElement("span");
          const totals = table.total_rows_detected.length ? table.total_rows_detected.join("、") : "未识别";
          text.textContent = `${table.table_id} · ${table.part_scope} · ${table.row_count} 行 · 合计行 ${totals}`;
          const select = document.createElement("select");
          select.dataset.tableId = table.table_id;
          select.innerHTML = '<option value="term-only">仅替换词项</option><option value="clear-detail-rows">保留表头和合计行，清空其他明细行</option>';
          row.append(text, select);
          panel.append(row);
        });
        panel.hidden = false;
      }
      setStatus(data.redaction_profile === "legal-template"
        ? "模板提炼模式：所有候选均需人工确认；未勾选的年份、比例、金额等不会替换。"
        : "请确认候选词，并补充遗漏的姓名、公司或地址。");
    })
    .catch(() => setStatus("无法连接本地确认页。", true));
})();
