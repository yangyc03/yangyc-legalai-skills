(() => {
  const token = new URLSearchParams(window.location.search).get("token") || "";
  const parameters = new URLSearchParams({ token });
  const image = document.getElementById("page-image");
  const canvas = document.getElementById("region-canvas");
  const context = canvas.getContext("2d");
  const stage = document.getElementById("canvas-stage");
  const regionsList = document.getElementById("regions");
  const selectionCount = document.getElementById("selection-count");
  const pageLabel = document.getElementById("page-label");
  const status = document.getElementById("status");
  const previous = document.getElementById("previous-page");
  const next = document.getElementById("next-page");
  const undo = document.getElementById("undo-region");
  const clear = document.getElementById("clear-page");
  const save = document.getElementById("save-selection");
  const generate = document.getElementById("confirm-generation");
  const cancel = document.getElementById("cancel-selection");
  const zoomSelect = document.getElementById("zoom-select");

  let session = null;
  let page = 1;
  let zoom = 1;
  let drawing = null;
  let regions = [];
  let saved = false;
  let requiresGenerationConfirmation = false;

  const clamp = (value) => Math.min(1, Math.max(0, value));
  const endpoint = (path) => `${path}?${parameters.toString()}`;

  function setStatus(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("error", isError);
  }

  function currentRegions() { return regions.filter((region) => region.page === page); }

  function toPixels(region) {
    return { x: region.x0 * canvas.width, y: region.y0 * canvas.height, width: (region.x1 - region.x0) * canvas.width, height: (region.y1 - region.y0) * canvas.height };
  }

  function drawRegion(region, preview = false) {
    const rect = toPixels(region);
    context.fillStyle = preview ? "rgba(202, 42, 42, 0.18)" : "rgba(202, 42, 42, 0.24)";
    context.strokeStyle = "#b31b1b";
    context.lineWidth = 2;
    context.fillRect(rect.x, rect.y, rect.width, rect.height);
    context.strokeRect(rect.x, rect.y, rect.width, rect.height);
  }

  function draw() {
    context.clearRect(0, 0, canvas.width, canvas.height);
    currentRegions().forEach((region) => drawRegion(region));
    if (drawing) drawRegion(drawing, true);
  }

  function resizeCanvas() {
    const width = image.clientWidth;
    const height = image.clientHeight;
    canvas.width = Math.max(1, Math.round(width));
    canvas.height = Math.max(1, Math.round(height));
    stage.style.width = `${width}px`;
    stage.style.height = `${height}px`;
    draw();
  }

  function renderList() {
    regionsList.replaceChildren();
    regions.forEach((region, index) => {
      const item = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = `第 ${region.page} 页，区域 ${index + 1}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "删除";
      remove.addEventListener("click", () => { regions = regions.filter((candidate) => candidate !== region); markUnsaved(); renderList(); draw(); });
      item.append(text, remove);
      regionsList.append(item);
    });
    selectionCount.textContent = `${regions.length} 个区域`;
    undo.disabled = currentRegions().length === 0;
    clear.disabled = currentRegions().length === 0;
  }

  function markUnsaved() {
    if (!saved) return;
    saved = false;
    if (requiresGenerationConfirmation) generate.disabled = true;
  }

  function updateControls() {
    pageLabel.textContent = `第 ${page} / ${session.page_count} 页`;
    previous.disabled = page <= 1;
    next.disabled = page >= session.page_count;
    renderList();
  }

  function loadPage() {
    setStatus("正在加载本地预览…");
    image.onload = () => {
      image.style.width = `${image.naturalWidth * zoom}px`;
      image.style.height = `${image.naturalHeight * zoom}px`;
      resizeCanvas();
      setStatus("拖动鼠标框选需要遮盖的区域。");
    };
    image.onerror = () => setStatus("本地预览无法加载。", true);
    image.src = `${endpoint(`/page/${page}.png`)}&v=${Date.now()}`;
    updateControls();
  }

  function normalizedPoint(event) {
    const rect = canvas.getBoundingClientRect();
    return { x: clamp((event.clientX - rect.left) / rect.width), y: clamp((event.clientY - rect.top) / rect.height) };
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (!session) return;
    event.preventDefault();
    canvas.setPointerCapture(event.pointerId);
    const point = normalizedPoint(event);
    drawing = { page, x0: point.x, y0: point.y, x1: point.x, y1: point.y };
    draw();
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drawing) return;
    const point = normalizedPoint(event);
    drawing.x1 = point.x; drawing.y1 = point.y; draw();
  });
  function finishDrawing(event) {
    if (!drawing) return;
    const point = normalizedPoint(event);
    const raw = { ...drawing, x1: point.x, y1: point.y };
    drawing = null;
    const region = { page, x0: Math.min(raw.x0, raw.x1), y0: Math.min(raw.y0, raw.y1), x1: Math.max(raw.x0, raw.x1), y1: Math.max(raw.y0, raw.y1) };
    if (region.x1 - region.x0 >= 0.001 && region.y1 - region.y0 >= 0.001) { regions.push(region); markUnsaved(); renderList(); }
    draw();
  }
  canvas.addEventListener("pointerup", finishDrawing);
  canvas.addEventListener("pointercancel", finishDrawing);

  previous.addEventListener("click", () => { page -= 1; loadPage(); });
  next.addEventListener("click", () => { page += 1; loadPage(); });
  zoomSelect.addEventListener("change", () => { zoom = Number(zoomSelect.value); loadPage(); });
  undo.addEventListener("click", () => { for (let index = regions.length - 1; index >= 0; index -= 1) { if (regions[index].page === page) { regions.splice(index, 1); break; } } markUnsaved(); renderList(); draw(); });
  clear.addEventListener("click", () => { regions = regions.filter((region) => region.page !== page); markUnsaved(); renderList(); draw(); });

  async function post(path, payload = {}) {
    const response = await fetch(endpoint(path), { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), cache: "no-store" });
    if (!response.ok) throw new Error("request_failed");
  }
  save.addEventListener("click", async () => {
    if (!regions.length) { setStatus("请至少框选一个区域，或点击取消。", true); return; }
    save.disabled = true;
    try { await post("/save", { regions }); setStatus("区域已保存。请关闭此页面并返回工具，随后显式确认生成脱敏副本。"); }
    catch (_) { save.disabled = false; setStatus("保存失败，区域尚未写入。", true); return; }
    saved = true;
    if (requiresGenerationConfirmation) {
      generate.disabled = false;
      save.disabled = false;
      setStatus("区域已保存，尚未生成副本。请人工检查框选后再点击“确认生成脱敏副本”。");
    }
  });
  generate.addEventListener("click", async () => {
    if (!saved) { setStatus("请先保存区域。", true); return; }
    generate.disabled = true;
    try { await post("/generate"); setStatus("已确认生成。本机正在创建脱敏副本；请返回工具等待完成。"); }
    catch (_) { generate.disabled = false; setStatus("确认失败，尚未生成副本。", true); }
  });
  cancel.addEventListener("click", async () => {
    try { await post("/cancel"); } catch (_) { /* local cancellation is best-effort */ }
    setStatus("已取消，未保存区域。");
  });
  window.addEventListener("resize", resizeCanvas);
  fetch(endpoint("/session"), { cache: "no-store" })
    .then((response) => response.json())
    .then((data) => {
      session = data;
      requiresGenerationConfirmation = Boolean(data.generation_confirmation_required);
      generate.hidden = !requiresGenerationConfirmation;
      generate.disabled = true;
      document.getElementById("file-meta").textContent = `${data.file_id} · ${data.source_kind === "pdf" ? "PDF" : "图片"} · ${data.page_count} 页`;
      loadPage();
    })
    .catch(() => setStatus("无法连接本地选择器。", true));
})();
