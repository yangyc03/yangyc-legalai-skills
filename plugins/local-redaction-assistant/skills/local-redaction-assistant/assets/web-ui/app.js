(() => {
  const token = new URLSearchParams(window.location.search).get("token") || "";
  const endpoint = (path) => `${path}?token=${encodeURIComponent(token)}`;
  const $ = (id) => document.getElementById(id);
  let session = null;
  let pollTimer = null;
  let currentPage = 1;
  let regions = {};
  let drawing = null;

  const FRIENDLY_ERRORS = {
    region_selector_pymupdf_missing: "当前启动环境缺少 PDF 预览组件。请关闭本页并重新双击启动文件；启动器会自动尝试本机 PDF 专用环境。",
    web_visual_preview_unavailable: "PDF/图片预览不可用，请确认文件可正常打开后重试。",
    web_docx_package_invalid: "这不是可读取的 DOCX 文件，请选择 Word 副本。",
    web_upload_extension_unsupported: "文件格式不支持，请选择 DOCX、PDF、PNG 或 JPG。",
    web_upload_too_large: "文件超过本机网页的大小限制（默认 200 MB）。",
    web_dictionary_extension_unsupported: "词典只接受 JSON 文件。",
    web_dictionary_too_large: "词典文件超过本机网页限制（512 KB）。",
    redaction_dictionary_json_invalid: "词典不是有效的 UTF-8 JSON 文件。",
    redaction_dictionary_schema_error: "词典结构不符合示例模板，请检查 version、terms 和 allowlist。",
    redaction_dictionary_category_error: "词典包含不支持的分类，请以示例模板中的分类为准。",
    web_docx_scan_incomplete: "DOCX 支持范围未完整扫描，已停止生成结果；请换用正常的 Word 副本后重试。",
  };
  const friendlyError = (code) => FRIENDLY_ERRORS[code] || code;

  const setGlobalStatus = (message, error = false) => {
    $("global-status").textContent = message || "";
    $("global-status").classList.toggle("error", error);
  };
  const show = (id) => $(id).classList.remove("hidden");
  const hide = (id) => $(id).classList.add("hidden");

  async function request(path, options = {}) {
    const response = await fetch(endpoint(path), { cache: "no-store", ...options });
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* handled below */ }
    if (!response.ok) throw new Error(payload.error || "web_request_failed");
    return payload;
  }

  function clearPanels() {
    ["upload-panel", "cleaning-panel", "docx-panel", "visual-panel", "status-panel"].forEach(hide);
  }

  function renderErrors(errorCodes) {
    const area = $("error-list");
    area.replaceChildren();
    if (!errorCodes || !errorCodes.length) { hide("error-list"); return; }
    const title = document.createElement("strong");
    title.textContent = "安全错误码：";
    area.append(title);
    const list = document.createElement("ul");
    errorCodes.forEach((code) => { const item = document.createElement("li"); item.textContent = code; list.append(item); });
    area.append(list);
    show("error-list");
  }

  function renderStatus(data) {
    session = data;
    $("version").textContent = `v${data.version || ""}`;
    $("status-file-id").textContent = data.file_id || "";
    $("status-message").textContent = data.message || "";
    $("status-title").textContent = data.phase === "completed" ? "处理完成" : data.phase === "failed" ? "处理失败" : "本机处理中";
    const progress = $("progress");
    progress.classList.toggle("done", data.phase === "completed");
    progress.classList.toggle("failed", data.phase === "failed");
    renderErrors(data.error_codes || []);
    const download = $("download-area");
    if (data.download_available) {
      $("download-link").href = endpoint("/api/download");
      $("download-link").download = data.download_name || "脱敏文件";
      show("download-area");
    } else hide("download-area");
  }

  function startPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = window.setInterval(async () => {
      try {
        const data = await request("/api/session");
        renderStatus(data);
        if (!["processing"].includes(data.phase)) {
          window.clearInterval(pollTimer); pollTimer = null;
        }
      } catch (_) { setGlobalStatus("本地服务连接中断。", true); }
    }, 900);
  }

  function categoryCard(category, values, labels) {
    const card = document.createElement("section");
    card.className = "candidate-card";
    card.dataset.category = category;
    const heading = document.createElement("h3"); heading.textContent = labels[category] || category; card.append(heading);
    const list = document.createElement("div"); list.className = "candidate-list";
    (values || []).forEach((candidate) => {
      const row = document.createElement("label"); row.className = "candidate-row";
      const box = document.createElement("input"); box.type = "checkbox"; box.value = typeof candidate === "string" ? candidate : candidate.id;
      if (candidate && candidate.replacement_supported === false) box.disabled = true;
      const text = document.createElement("span");
      text.textContent = typeof candidate === "string" ? candidate : candidate.display;
      if (candidate && (candidate.part_scope || candidate.auto_eligible)) { const small = document.createElement("small"); small.textContent = `${candidate.part_scope || ""}${candidate.auto_eligible ? " · 高置信格式项" : ""}${candidate.replacement_supported === false ? " · 需人工处理" : ""}`; text.append(small); }
      row.append(box, text); list.append(row);
    });
    const input = document.createElement("input"); input.className = "add-input"; input.type = "text"; input.maxLength = 80; input.placeholder = `补充${labels[category] || category}后按回车`;
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault(); const value = input.value.trim(); if (!value) return;
      const row = document.createElement("label"); row.className = "candidate-row";
      const box = document.createElement("input"); box.type = "checkbox"; box.value = value; box.checked = true; box.dataset.addition = "true";
      const text = document.createElement("span"); text.textContent = value; row.append(box, text); list.append(row); input.value = "";
    });
    card.append(list, input); return card;
  }

  function renderDocx(data) {
    clearPanels(); show("docx-panel");
    $("docx-file-id").textContent = `${data.file_id} · ${data.display_name || "DOCX"}`;
    $("docx-mode-hint").textContent = data.profile === "legal-template"
      ? "模板提炼模式：所有候选均需人工确认；未勾选的年份、比例和金额不会替换。"
      : "普通共享模式：请确认候选词，并补充遗漏的姓名、公司、地址或项目变量。高置信格式项默认不替换。";
    const highConfidenceRow = $("high-confidence-row");
    const highConfidence = $("high-confidence-auto");
    if (data.profile === "ai-share" && (data.high_confidence_count || 0) > 0) {
      highConfidenceRow.classList.remove("hidden");
      highConfidence.checked = false;
      highConfidenceRow.lastChild.textContent = `启用高置信格式项自动处理（手机号、邮箱、有效身份证号、统一社会信用代码；共 ${data.high_confidence_count} 处）`;
    } else {
      highConfidenceRow.classList.add("hidden");
      highConfidence.checked = false;
    }
    const coverage = data.preflight || {};
    const coverageParts = [
      `扫描 ${coverage.docx_text_units_scanned || 0} 个文本单元 / ${coverage.docx_text_nodes_scanned || 0} 个文字节点`,
      `表格 ${coverage.docx_tables_scanned || 0} 个、${coverage.docx_rows_scanned || 0} 行、${coverage.docx_cells_scanned || 0} 个单元格`,
    ];
    if (coverage.docx_parts_skipped) coverageParts.push(`跳过 ${coverage.docx_parts_skipped} 个非本模式范围部件`);
    $("docx-coverage").textContent = `本次覆盖统计：${coverageParts.join("；")}。${coverage.docx_scan_complete ? "扫描完成。" : "扫描未完整完成，不能生成成功结果。"}`;
    const grid = $("candidate-grid"); grid.replaceChildren();
    let categories = Object.keys(data.category_labels || {});
    if (data.profile !== "legal-template") categories = categories.filter((category) => (data.candidate_terms?.[category] || []).length);
    if (!categories.length) categories = ["persons", "companies", "addresses"];
    categories.forEach((category) => grid.append(categoryCard(category, data.candidate_terms?.[category] || [], data.category_labels || {})));
    const associations = $("associations"); associations.replaceChildren();
    if ((data.candidate_associations || []).length) {
      const heading = document.createElement("h3"); heading.textContent = "主体全称与简称关联候选"; associations.append(heading);
      data.candidate_associations.forEach((item) => { const row = document.createElement("p"); row.className = "association-row"; row.textContent = `${item.full_name} ↔ ${item.alias}`; associations.append(row); });
      show("associations");
    } else hide("associations");
    const tables = $("table-actions"); tables.replaceChildren();
    if ((data.table_candidates || []).length) {
      const heading = document.createElement("h3"); heading.textContent = data.profile === "legal-template" ? "敏感表格逐表处理" : "正文表格扫描状态"; tables.append(heading);
      data.table_candidates.forEach((table) => {
        const row = document.createElement("label"); row.className = "table-row";
        const text = document.createElement("span"); text.textContent = `${table.table_id} · ${table.part_scope} · ${table.row_count} 行 · ${table.cell_count || 0} 个单元格${table.scan_complete === false ? " · 扫描不完整" : " · 已扫描"}`;
        row.append(text);
        if (data.profile === "legal-template") {
          const select = document.createElement("select"); select.dataset.tableId = table.table_id; select.innerHTML = '<option value="term-only">仅替换词项</option><option value="clear-detail-rows">保留表头和合计行，清空其他明细行</option>'; row.append(select);
        }
        tables.append(row);
      }); show("table-actions");
    } else hide("table-actions");
  }

  function docxSelection() {
    const selected = {}; const additions = {};
    document.querySelectorAll(".candidate-card[data-category]").forEach((card) => {
      const category = card.dataset.category; selected[category] = []; additions[category] = [];
      card.querySelectorAll("input[type=checkbox]:checked").forEach((box) => (box.dataset.addition ? additions[category] : selected[category]).push(box.value));
    });
    const table_actions = {}; document.querySelectorAll("select[data-table-id]").forEach((select) => { table_actions[select.dataset.tableId] = select.value; });
    return { selected, additions, table_actions, auto_high_confidence: $("high-confidence-auto").checked };
  }

  function renderCleaning(data) {
    clearPanels(); show("cleaning-panel");
    const preflight = data.preflight || {};
    $("cleaning-summary").textContent = `文件：${data.display_name || "DOCX"} · 修订节点：${preflight.revision_nodes || 0} · 批注：${preflight.comments || 0}`;
    $("revision-check-row").hidden = !(preflight.revision_nodes || preflight.track_revisions_enabled);
    $("comment-check-row").hidden = !(preflight.comments || 0);
    $("accept-revisions").checked = Boolean(preflight.revision_nodes || preflight.track_revisions_enabled);
    $("remove-comments").checked = Boolean(preflight.comments);
  }

  function pageRegions(page) { return regions[page] || (regions[page] = []); }
  function drawRegions() {
    const canvas = $("region-canvas"); const image = $("page-image");
    if (!image.naturalWidth) return;
    canvas.width = image.naturalWidth; canvas.height = image.naturalHeight;
    const context = canvas.getContext("2d"); context.clearRect(0, 0, canvas.width, canvas.height);
    context.strokeStyle = "#d62828"; context.fillStyle = "rgba(214,40,40,.18)"; context.lineWidth = Math.max(3, canvas.width / 500);
    pageRegions(currentPage).forEach((region) => { const x = region.x0 * canvas.width; const y = region.y0 * canvas.height; const w = (region.x1 - region.x0) * canvas.width; const h = (region.y1 - region.y0) * canvas.height; context.fillRect(x, y, w, h); context.strokeRect(x, y, w, h); });
    const start = drawing; if (start) { const rect = canvas.getBoundingClientRect(); const x = Math.min(start.x, (start.currentX - rect.left) / rect.width); const y = Math.min(start.y, (start.currentY - rect.top) / rect.height); const w = Math.abs((start.currentX - rect.left) / rect.width - start.x); const h = Math.abs((start.currentY - rect.top) / rect.height - start.y); context.fillRect(x * canvas.width, y * canvas.height, w * canvas.width, h * canvas.height); context.strokeRect(x * canvas.width, y * canvas.height, w * canvas.width, h * canvas.height); }
  }
  function renderRegionList() {
    const list = $("region-items"); list.replaceChildren(); const all = Object.entries(regions).flatMap(([page, items]) => items.map((_, index) => `第 ${page} 页 · 区域 ${index + 1}`));
    $("region-count").textContent = `${all.length} 个区域`; all.forEach((label) => { const item = document.createElement("li"); item.textContent = label; list.append(item); });
    $("generate-visual").disabled = all.length === 0 || !session?.regions_saved;
  }
  function loadPage(page) {
    currentPage = Math.max(1, Math.min(page, session.page_count)); $("page-label").textContent = `第 ${currentPage} 页 / 共 ${session.page_count} 页`; $("page-image").onload = drawRegions; $("page-image").src = endpoint(`/api/preview/${currentPage}.png`); renderRegionsAndList();
  }
  function renderRegionsAndList() { drawRegions(); renderRegionList(); }
  function renderVisual(data) {
    clearPanels(); show("visual-panel"); $("visual-file-id").textContent = `${data.file_id} · ${data.display_name || data.source_type}`; session = data; regions = {}; currentPage = 1; loadPage(1); renderRegionList();
  }

  $("file-input").addEventListener("change", () => { $("file-name").textContent = $("file-input").files[0]?.name || "尚未选择文件"; });
  $("upload-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const file = $("file-input").files[0]; if (!file) return;
    const form = new FormData(); form.append("file", file); form.append("profile", $("profile").value);
    const dictionary = $("dictionary-input").files[0]; if (dictionary) form.append("dictionary", dictionary);
    $("upload-button").disabled = true; setGlobalStatus("正在将文件送入本机临时工作区……");
    try { const data = await request("/api/upload", { method: "POST", body: form }); renderStatus(data); route(data); } catch (error) { setGlobalStatus(`载入失败：${friendlyError(error.message)}`, true); } finally { $("upload-button").disabled = false; }
  });
  function route(data) { session = data; $("version").textContent = `v${data.version || ""}`; if (data.phase === "docx_cleaning_required") renderCleaning(data); else if (data.phase === "docx_review") renderDocx(data); else if (data.phase === "visual_review") renderVisual(data); else if (["processing", "completed", "failed"].includes(data.phase)) { clearPanels(); show("status-panel"); renderStatus(data); if (data.phase === "processing") startPolling(); } }
  $("confirm-cleaning").addEventListener("click", async () => { try { const data = await request("/api/docx/clean", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ accept_revisions: $("accept-revisions").checked, remove_comments: $("remove-comments").checked }) }); clearPanels(); show("status-panel"); renderStatus(data); startPolling(); } catch (error) { setGlobalStatus(`清洁确认失败：${friendlyError(error.message)}`, true); } });
  $("confirm-docx").addEventListener("click", async () => { $("confirm-docx").disabled = true; try { const data = await request("/api/docx/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(docxSelection()) }); clearPanels(); show("status-panel"); renderStatus(data); startPolling(); } catch (error) { setGlobalStatus(`确认失败：${friendlyError(error.message)}`, true); $("confirm-docx").disabled = false; } });
  $("save-regions").addEventListener("click", async () => { try { const data = await request("/api/visual/save-regions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ regions: Object.entries(regions).flatMap(([page, items]) => items.map((item) => ({ ...item, page: Number(page) }))) }) }); session = data; renderRegionList(); setGlobalStatus("区域已保存；请检查后确认生成。", false); } catch (error) { setGlobalStatus(`区域保存失败：${friendlyError(error.message)}`, true); } });
  $("generate-visual").addEventListener("click", async () => { try { const data = await request("/api/visual/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); clearPanels(); show("status-panel"); renderStatus(data); startPolling(); } catch (error) { setGlobalStatus(`生成失败：${friendlyError(error.message)}`, true); } });
  $("previous-page").addEventListener("click", () => loadPage(currentPage - 1)); $("next-page").addEventListener("click", () => loadPage(currentPage + 1)); $("clear-page").addEventListener("click", () => { regions[currentPage] = []; renderRegionsAndList(); });
  $("region-canvas").addEventListener("pointerdown", (event) => { const rect = event.currentTarget.getBoundingClientRect(); drawing = { x: (event.clientX - rect.left) / rect.width, y: (event.clientY - rect.top) / rect.height, currentX: event.clientX, currentY: event.clientY }; event.currentTarget.setPointerCapture(event.pointerId); drawRegions(); });
  $("region-canvas").addEventListener("pointermove", (event) => { if (!drawing) return; drawing.currentX = event.clientX; drawing.currentY = event.clientY; drawRegions(); });
  $("region-canvas").addEventListener("pointerup", (event) => { if (!drawing) return; const rect = event.currentTarget.getBoundingClientRect(); const x1 = (event.clientX - rect.left) / rect.width; const y1 = (event.clientY - rect.top) / rect.height; const region = { x0: Math.max(0, Math.min(drawing.x, x1)), y0: Math.max(0, Math.min(drawing.y, y1)), x1: Math.min(1, Math.max(drawing.x, x1)), y1: Math.min(1, Math.max(drawing.y, y1)) }; drawing = null; if (region.x1 - region.x0 > .005 && region.y1 - region.y0 > .005) pageRegions(currentPage).push(region); drawRegions(); renderRegionList(); });
  $("dictionary-input").addEventListener("change", () => { $("dictionary-name").textContent = $("dictionary-input").files[0]?.name || "未导入词典"; });
  async function reset() { try { const data = await request("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); session = data; clearPanels(); show("upload-panel"); setGlobalStatus(""); $("file-input").value = ""; $("file-name").textContent = "尚未选择文件"; $("dictionary-input").value = ""; $("dictionary-name").textContent = "未导入词典"; $("high-confidence-auto").checked = false; $("docx-coverage").textContent = ""; } catch (error) { setGlobalStatus(`无法重新开始：${friendlyError(error.message)}`, true); } }
  ["reset-cleaning", "reset-docx", "reset-visual", "reset-status"].forEach((id) => $(id).addEventListener("click", reset));
  $("shutdown").addEventListener("click", async () => { try { await request("/api/shutdown", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); $("shutdown").disabled = true; setGlobalStatus("本机服务已请求关闭，可以关闭此浏览器页面。", false); } catch (_) { setGlobalStatus("本机服务可能已经关闭。", true); } });
  request("/api/session").then((data) => { session = data; $("version").textContent = `v${data.version || ""}`; route(data); }).catch(() => setGlobalStatus("无法连接本机脱敏服务。", true));
})();
