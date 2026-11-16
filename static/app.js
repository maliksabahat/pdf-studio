"use strict";

/* ------------------------------------------------------------------ utils */
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  pages: [],
  image: null,
  mixed: [],
  seq: 0,
  reordering: false,
  landedKey: null,
};

function nextKey() {
  state.seq += 1;
  return "k" + state.seq;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function icon(id) {
  return `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return value + " B";
  const units = ["KB", "MB", "GB"];
  let size = value / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return size.toFixed(size >= 10 ? 0 : 1) + " " + units[index];
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 300) };
    }
  }
  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

function toast(message, type = "info") {
  const host = $("#toasts");
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  const glyph = type === "success" ? "i-check" : type === "error" ? "i-x" : "i-file";
  node.innerHTML = `${icon(glyph)}<div>${esc(message)}</div>`;
  host.appendChild(node);
  window.setTimeout(() => {
    node.style.opacity = "0";
    node.style.transform = "translateY(6px)";
    node.style.transition = "opacity .25s, transform .25s";
    window.setTimeout(() => node.remove(), 260);
  }, 5200);
}

function setBusy(section, busy, text) {
  const panel = document.querySelector(`.panel[data-section="${section}"]`);
  if (!panel) return;
  const overlay = $(".busy", panel);
  if (!overlay) return;
  overlay.hidden = !busy;
  if (text) $("p", overlay).textContent = text;
}

function autoDownload(url, name) {
  const link = document.createElement("a");
  link.href = url;
  link.download = name || "";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function showResult(selector, data, title, detail) {
  const box = $(selector);
  if (!box) return;
  box.hidden = false;
  box.innerHTML = `
    <span class="result-icon">${icon("i-check")}</span>
    <div>
      <strong>${esc(title)}</strong>
      <p>${esc(detail || "")}</p>
    </div>
    <a class="btn primary" href="${esc(data.url)}" download="${esc(data.name)}">
      ${icon("i-download")} Download
    </a>`;
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  autoDownload(data.url, data.name);
}

/* ---------------------------------------------------------------- lightbox */
const lightbox = {
  items: [],
  index: 0,
  zoom: 1,
  fitW: 0,
  fitH: 0,
  lastFocus: null,
};

function pageLightboxItems() {
  return state.pages.map((page, index) => ({
    src: `${page.thumb}?rot=${page.rotation}&full=1`,
    title: page.name,
    meta: `${page.kind === "pdf" ? "PDF" : "Image"} · Page ${page.page + 1} · ${index + 1} of ${state.pages.length}`,
    alt: `${page.name} page ${page.page + 1}`,
  }));
}

function imageLightboxItems() {
  if (!state.image) return [];
  const count = Math.max(1, Number(state.image.pageCount) || state.image.pages.length || 1);
  return Array.from({ length: count }, (_, index) => ({
    src: `/api/thumb/${state.image.id}/${index}?full=1`,
    title: state.image.name,
    meta: `Page ${index + 1} of ${count}`,
    alt: `${state.image.name} page ${index + 1}`,
  }));
}

function mixedLightboxItems() {
  return state.mixed.map((item, index) => ({
    src: `/api/mixed/thumb/${item.id}?full=1`,
    title: item.name,
    meta: `${fileVisual(item.kind, item.ext).label} · ${index + 1} of ${state.mixed.length}`,
    alt: `${item.name} first page`,
  }));
}

function openLightbox(items, index = 0) {
  if (!items || !items.length) return;
  lightbox.items = items;
  lightbox.lastFocus = document.activeElement;
  lightbox.index = Math.max(0, Math.min(index, items.length - 1));
  $("#lightbox").hidden = false;
  document.body.classList.add("lightbox-open");
  showLightboxItem(lightbox.index);
  $("#lightboxClose").focus({ preventScroll: true });
}

function closeLightbox() {
  const box = $("#lightbox");
  if (!box || box.hidden) return;
  box.hidden = true;
  document.body.classList.remove("lightbox-open");
  const img = $("#lightboxImage");
  img.onload = null;
  img.onerror = null;
  img.removeAttribute("src");
  img.style.width = "";
  img.style.height = "";
  img.classList.remove("ready", "loading");
  lightbox.items = [];
  lightbox.zoom = 1;
  lightbox.fitW = 0;
  lightbox.fitH = 0;
  const last = lightbox.lastFocus;
  lightbox.lastFocus = null;
  if (last && document.contains(last)) last.focus({ preventScroll: true });
}

function showLightboxItem(index) {
  if (!lightbox.items.length) return;
  const count = lightbox.items.length;
  lightbox.index = (index + count) % count;
  const item = lightbox.items[lightbox.index];
  const img = $("#lightboxImage");
  lightbox.zoom = 1;
  lightbox.fitW = 0;
  lightbox.fitH = 0;
  img.classList.remove("ready");
  img.classList.add("loading");
  img.style.width = "";
  img.style.height = "";
  img.alt = item.alt || item.title || "Preview";
  img.onload = () => {
    img.classList.remove("loading");
    fitLightboxImage();
  };
  img.onerror = () => {
    img.classList.remove("loading");
    toast("Could not load this preview", "error");
  };
  img.src = item.src;
  $("#lightboxTitle").textContent = item.title || "Preview";
  $("#lightboxMeta").textContent = item.meta || "";
  $("#lightboxCounter").textContent = count > 1 ? `${lightbox.index + 1} / ${count}` : "";
  $("#lightboxPrev").disabled = count <= 1;
  $("#lightboxNext").disabled = count <= 1;
  applyLightboxZoom();
  const stage = $("#lightboxStage");
  stage.scrollTop = 0;
  stage.scrollLeft = 0;
  if (img.complete && img.naturalWidth) {
    img.classList.remove("loading");
    fitLightboxImage();
  }
}

function fitLightboxImage() {
  const img = $("#lightboxImage");
  const stage = $("#lightboxStage");
  if (!img.naturalWidth || !img.naturalHeight || !stage.clientWidth || !stage.clientHeight) return;
  const pad = 36;
  const availW = Math.max(60, stage.clientWidth - pad);
  const availH = Math.max(60, stage.clientHeight - pad);
  const scale = Math.min(availW / img.naturalWidth, availH / img.naturalHeight, 1);
  lightbox.fitW = Math.max(1, Math.round(img.naturalWidth * scale));
  lightbox.fitH = Math.max(1, Math.round(img.naturalHeight * scale));
  img.classList.add("ready");
  applyLightboxZoom();
}

function applyLightboxZoom() {
  const img = $("#lightboxImage");
  if (lightbox.fitW && lightbox.fitH) {
    img.style.width = Math.round(lightbox.fitW * lightbox.zoom) + "px";
    img.style.height = Math.round(lightbox.fitH * lightbox.zoom) + "px";
  }
  $("#lightboxZoomLabel").textContent = lightbox.zoom <= 1 ? "Fit" : Math.round(lightbox.zoom * 100) + "%";
  $("#lightboxZoomOut").disabled = lightbox.zoom <= 1;
  $("#lightboxZoomIn").disabled = lightbox.zoom >= 4;
}

function setLightboxZoom(value) {
  const next = Math.max(1, Math.min(4, Math.round(value * 4) / 4));
  if (next === lightbox.zoom) return;
  lightbox.zoom = next;
  applyLightboxZoom();
  const stage = $("#lightboxStage");
  if (next === 1) {
    stage.scrollTop = 0;
    stage.scrollLeft = 0;
  } else {
    requestAnimationFrame(() => {
      stage.scrollLeft = Math.max(0, (stage.scrollWidth - stage.clientWidth) / 2);
      stage.scrollTop = Math.max(0, (stage.scrollHeight - stage.clientHeight) / 2);
    });
  }
}

function wireLightbox() {
  const box = $("#lightbox");
  if (!box) return;
  $("#lightboxBackdrop").addEventListener("click", closeLightbox);
  $("#lightboxClose").addEventListener("click", closeLightbox);
  $("#lightboxPrev").addEventListener("click", () => showLightboxItem(lightbox.index - 1));
  $("#lightboxNext").addEventListener("click", () => showLightboxItem(lightbox.index + 1));
  $("#lightboxZoomOut").addEventListener("click", () => setLightboxZoom(lightbox.zoom - 0.25));
  $("#lightboxZoomIn").addEventListener("click", () => setLightboxZoom(lightbox.zoom + 0.25));
  $("#lightboxReset").addEventListener("click", () => setLightboxZoom(1));

  document.addEventListener("keydown", (event) => {
    if (box.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeLightbox();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      showLightboxItem(lightbox.index - 1);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      showLightboxItem(lightbox.index + 1);
    } else if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      setLightboxZoom(lightbox.zoom + 0.25);
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      setLightboxZoom(lightbox.zoom - 0.25);
    } else if (event.key === "0") {
      event.preventDefault();
      setLightboxZoom(1);
    }
  });

  window.addEventListener("resize", () => {
    if (!box.hidden) fitLightboxImage();
  });
}

/* --------------------------------------------------------------- dropzones */
function setupDropzone(zone, input, onFiles) {
  if (!zone || !input) return;

  const openPicker = () => input.click();

  zone.addEventListener("click", (event) => {
    if (event.target.closest("button, a, input")) return;
    openPicker();
  });

  zone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openPicker();
    }
  });

  $$("[data-browse]", zone).forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openPicker();
    });
  });

  input.addEventListener("change", () => {
    if (input.files && input.files.length) onFiles(input.files);
    input.value = "";
  });

  ["dragenter", "dragover"].forEach((type) => {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      event.stopPropagation();
      zone.classList.add("over");
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    });
  });

  ["dragleave", "dragend"].forEach((type) => {
    zone.addEventListener(type, (event) => {
      if (event.relatedTarget && zone.contains(event.relatedTarget)) return;
      zone.classList.remove("over");
    });
  });

  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    event.stopPropagation();
    zone.classList.remove("over");
    if (event.dataTransfer && event.dataTransfer.files.length) {
      onFiles(event.dataTransfer.files);
    }
  });
}

function dropInsertIndex(event, axis) {
  const target = event.target.closest("[data-key]");
  if (!target || !target.parentElement) return null;
  const nodes = $$("[data-key]", target.parentElement);
  const index = nodes.indexOf(target);
  if (index < 0) return null;
  const rect = target.getBoundingClientRect();
  const after = axis === "y"
    ? (event.clientY - rect.top) > rect.height / 2
    : (event.clientX - rect.left) > rect.width / 2;
  return index + (after ? 1 : 0);
}

function setupPanelDrop(panel, onFiles, axis) {
  if (!panel) return;

  panel.addEventListener("dragover", (event) => {
    if (state.reordering) return;
    const types = event.dataTransfer ? Array.from(event.dataTransfer.types || []) : [];
    if (!types.includes("Files")) return;
    event.preventDefault();
    panel.classList.add("file-over");
  });

  panel.addEventListener("dragleave", (event) => {
    if (event.relatedTarget && panel.contains(event.relatedTarget)) return;
    panel.classList.remove("file-over");
  });

  panel.addEventListener("drop", (event) => {
    if (state.reordering) return;
    if (!event.dataTransfer || !event.dataTransfer.files || !event.dataTransfer.files.length) return;
    if (event.target.closest(".dropzone")) return;
    event.preventDefault();
    panel.classList.remove("file-over");
    onFiles(event.dataTransfer.files, dropInsertIndex(event, axis));
  });
}

/* ------------------------------------------------------------ page studio */
function renderPages() {
  const grid = $("#pageGrid");
  if (!grid) return;
  const fragment = document.createDocumentFragment();
  state.pages.forEach((page, index) => {
    const card = document.createElement("article");
    card.className = "page-card";
    if (page.key === state.landedKey) card.classList.add("landed");
    card.draggable = true;
    card.dataset.key = page.key;
    card.setAttribute("role", "listitem");
    card.setAttribute("aria-label", `${page.name}, page ${page.page + 1}`);
    const badge = page.kind === "pdf" ? `PDF · p.${page.page + 1}` : "Image";
    card.innerHTML = `
      <div class="thumb-wrap">
        <img loading="lazy" draggable="false" src="${esc(page.thumb)}?rot=${page.rotation}" alt="${esc(page.name)} page ${page.page + 1}">
        <span class="page-badge">${esc(badge)}</span>
        <div class="card-actions">
          <button class="icon-btn" type="button" data-act="view" title="View full size" aria-label="View page full size">${icon("i-expand")}</button>
          <button class="icon-btn" type="button" data-act="rotate" title="Rotate 90°" aria-label="Rotate page">${icon("i-rotate")}</button>
          <button class="icon-btn" type="button" data-act="duplicate" title="Duplicate" aria-label="Duplicate page">${icon("i-copy")}</button>
          <button class="icon-btn danger" type="button" data-act="delete" title="Remove" aria-label="Remove page">${icon("i-trash")}</button>
        </div>
      </div>
      <div class="card-foot">
        <span class="card-name" title="${esc(page.name)}">${esc(page.name)}</span>
        <div class="move-btns">
          <button class="icon-btn small" type="button" data-act="left" title="Move left" aria-label="Move left" ${index === 0 ? "disabled" : ""}>${icon("i-left")}</button>
          <span class="idx">${index + 1}</span>
          <button class="icon-btn small" type="button" data-act="right" title="Move right" aria-label="Move right" ${index === state.pages.length - 1 ? "disabled" : ""}>${icon("i-right")}</button>
        </div>
      </div>`;
    fragment.appendChild(card);
  });

  grid.replaceChildren(fragment);
  state.landedKey = null;
  $("#pageStat").textContent = `${state.pages.length} page${state.pages.length === 1 ? "" : "s"}`;
  $("#pageWorkspace").hidden = state.pages.length === 0;
  if (!state.pages.length) $("#pageResult").hidden = true;
  updateDropVisibility();
}

function updateDropVisibility() {
  $("#pageDrop").hidden = state.pages.length > 0;
  $("#mixedDrop").hidden = state.mixed.length > 0;
  $("#imageDrop").hidden = Boolean(state.image);
}

async function uploadPages(fileList, insertAt) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  setBusy("pages", true, "Reading pages…");
  try {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const data = await api("/api/pages/upload", { method: "POST", body: form });

    const addedPages = [];
    (data.files || []).forEach((file) => {
      (file.pages || []).forEach((page) => {
        addedPages.push({
          key: nextKey(),
          uploadId: file.id,
          page: page.index,
          rotation: 0,
          name: file.name,
          kind: file.kind,
          thumb: page.thumb,
          width: page.w,
          height: page.h,
        });
      });
    });
    const at = Number.isInteger(insertAt)
      ? Math.max(0, Math.min(insertAt, state.pages.length))
      : state.pages.length;
    state.pages.splice(at, 0, ...addedPages);

    renderPages();
    if (data.errors && data.errors.length) {
      data.errors.forEach((message) => toast(message, "error"));
    } else {
      const added = (data.files || []).reduce((sum, file) => sum + (file.pageCount || 0), 0);
      toast(`${added} page${added === 1 ? "" : "s"} added`, "success");
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy("pages", false);
  }
}

async function buildPages() {
  if (!state.pages.length) return;
  setBusy("pages", true, "Building your PDF…");
  try {
    const options = {
      pageSize: $("#pageSize").value,
      imagePageSize: $("#pageSize").value,
      fit: $("#pageFit").value,
      margin: Number($("#pageMargin").value),
      uniform: $("#uniform").checked,
      name: $("#pageName").value.trim() || "organized",
    };
    const payload = {
      items: state.pages.map((page) => ({
        uploadId: page.uploadId,
        page: page.page,
        rotation: page.rotation,
      })),
      options,
    };
    const data = await api("/api/pages/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showResult("#pageResult", data, "Your PDF is ready",
      `${data.pages} page${data.pages === 1 ? "" : "s"} · ${data.name}`);
    toast("PDF created", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy("pages", false);
  }
}

/* ------------------------------------------------------------- PDF -> images */
async function uploadImagePdf(fileList) {
  const file = Array.from(fileList || [])[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    toast("Please choose a PDF file for this section.", "error");
    return;
  }
  setBusy("images", true, "Reading the PDF…");
  try {
    const form = new FormData();
    form.append("file", file);
    const data = await api("/api/images/upload", { method: "POST", body: form });
    state.image = data;
    $("#imageFile").hidden = false;
    $("#imageName").textContent = data.name;
    $("#imageMeta").textContent =
      `${data.pageCount} page${data.pageCount === 1 ? "" : "s"} · ${formatBytes(data.size)}`;
    const pages = data.pages && data.pages.length
      ? data.pages
      : Array.from({ length: data.pageCount }, (_, index) => `/api/thumb/${data.id}/${index}`);
    $("#imagePreview").innerHTML = pages.map((src, index) => `
      <div class="preview-page" data-index="${index}">
        <img src="${esc(src)}" alt="Page ${index + 1}" loading="lazy" data-index="${index}" tabindex="0" role="button" title="View page ${index + 1} full size">
        <div class="preview-actions">
          <button class="preview-btn" type="button" data-act="zoom" title="View page ${index + 1} full size" aria-label="View page ${index + 1} full size">${icon("i-expand")}</button>
          <button class="preview-btn" type="button" data-act="download" title="Download page ${index + 1}" aria-label="Download page ${index + 1}">${icon("i-download")}</button>
        </div>
      </div>`).join("");
    $("#imageConvert").disabled = false;
    $("#imageResult").hidden = true;
    updateDropVisibility();
    toast("PDF loaded", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy("images", false);
  }
}

function resetImagePdf() {
  state.image = null;
  $("#imageFile").hidden = true;
  $("#imagePreview").innerHTML = "";
  $("#imageResult").hidden = true;
  $("#imageConvert").disabled = true;
  $("#imageInput").value = "";
  updateDropVisibility();
}

async function convertImages() {
  if (!state.image) return;
  setBusy("images", true, "Rendering pages…");
  try {
    const format = $("#imageFormat").value;
    const data = await api("/api/images/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uploadId: state.image.id,
        format,
        dpi: Number($("#imageDpi").value),
        quality: Number($("#imageQuality").value),
        range: $("#imageRange").value.trim(),
      }),
    });
    showResult("#imageResult", data, data.single ? "Image ready" : "Images ready",
      `${data.count} ${format.toUpperCase()} image${data.count === 1 ? "" : "s"} · ${formatBytes(data.size)} · ${data.name}`);
    toast(data.single ? "Image exported" : "Images exported as a ZIP", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy("images", false);
  }
}

/* --------------------------------------------------------------- mixed PDF */
function fileVisual(kind, ext) {
  const extension = (ext || "").toLowerCase();
  if (kind === "pdf") return { chip: "PDF", cls: "pdf", label: "PDF document" };
  if (kind === "image") return { chip: "IMG", cls: "image", label: "Image" };
  if (kind === "office") {
    if (extension.startsWith(".doc")) return { chip: "DOC", cls: "word", label: "Word document" };
    if (extension.startsWith(".xls")) return { chip: "XLS", cls: "excel", label: "Excel workbook" };
    if (extension.startsWith(".ppt")) return { chip: "PPT", cls: "powerpoint", label: "PowerPoint slides" };
  }
  if (kind === "text") {
    const chip = extension.replace(".", "").slice(0, 4).toUpperCase() || "TXT";
    return { chip, cls: "text", label: "Text / data" };
  }
  return { chip: "FILE", cls: "text", label: "File" };
}

function renderMixed() {
  const list = $("#mixedList");
  if (!list) return;
  const fragment = document.createDocumentFragment();

  state.mixed.forEach((item, index) => {
    const visual = fileVisual(item.kind, item.ext);
    const row = document.createElement("div");
    row.className = "mixed-item";
    if (item.key === state.landedKey) row.classList.add("landed");
    row.draggable = true;
    row.dataset.key = item.key;
    row.setAttribute("role", "listitem");
    row.setAttribute("aria-label", item.name);
    row.innerHTML = `
      <span class="grip" title="Drag to reorder">${icon("i-grip")}</span>
      <button class="mixed-thumb" type="button" data-act="preview" title="View full size" aria-label="View ${esc(item.name)} full size">
        <span class="file-type ${visual.cls}">${esc(visual.chip)}</span>
        <img src="/api/mixed/thumb/${esc(item.id)}" alt="" loading="lazy" draggable="false" onerror="this.remove()">
        <span class="mixed-thumb-zoom">${icon("i-expand")}</span>
      </button>
      <div class="mixed-info">
        <strong title="${esc(item.name)}">${esc(item.name)}</strong>
        <span>${esc(visual.label)} · ${formatBytes(item.size)}</span>
      </div>
      <div class="mixed-actions">
        <button class="icon-btn small" type="button" data-act="up" title="Move up" aria-label="Move up" ${index === 0 ? "disabled" : ""}>${icon("i-up")}</button>
        <button class="icon-btn small" type="button" data-act="down" title="Move down" aria-label="Move down" ${index === state.mixed.length - 1 ? "disabled" : ""}>${icon("i-down")}</button>
        <button class="icon-btn small" type="button" data-act="remove" title="Remove" aria-label="Remove" >${icon("i-trash")}</button>
      </div>`;
    fragment.appendChild(row);
  });

  list.replaceChildren(fragment);
  state.landedKey = null;
  $("#mixedWorkspace").hidden = state.mixed.length === 0;
  if (!state.mixed.length) $("#mixedResult").hidden = true;
  updateDropVisibility();
}

async function uploadMixed(fileList, insertAt) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  setBusy("mixed", true, "Reading files…");
  try {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const data = await api("/api/mixed/upload", { method: "POST", body: form });
    const addedItems = (data.items || []).map((item) => ({ ...item, key: nextKey() }));
    const at = Number.isInteger(insertAt)
      ? Math.max(0, Math.min(insertAt, state.mixed.length))
      : state.mixed.length;
    state.mixed.splice(at, 0, ...addedItems);
    renderMixed();
    if (data.errors && data.errors.length) {
      data.errors.forEach((message) => toast(message, "error"));
    } else {
      toast(`${(data.items || []).length} file${(data.items || []).length === 1 ? "" : "s"} added`, "success");
    }
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy("mixed", false);
  }
}

async function buildMixed() {
  if (!state.mixed.length) return;
  setBusy("mixed", true, "Converting documents…");
  try {
    const options = {
      pageSize: $("#mixedSize").value,
      orientation: $("#mixedOrientation").value,
      margin: Number($("#mixedMargin").value),
      name: $("#mixedName").value.trim() || "mixed-document",
    };
    const data = await api("/api/mixed/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        items: state.mixed.map((item) => ({ uploadId: item.id })),
        options,
      }),
    });
    showResult("#mixedResult", data, "Combined PDF is ready",
      `${data.pages} page${data.pages === 1 ? "" : "s"} · ${data.name}`);
    toast("Combined PDF created", "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy("mixed", false);
  }
}

/* ------------------------------------------------------------------ reorder */
function enableReorder(container, getItems, render, axis) {
  if (!container) return;
  let dragKey = null;
  let dropTargetKey = null;
  let dropAfter = false;
  let placeholder = null;

  const removePlaceholder = () => {
    if (placeholder) {
      placeholder.remove();
      placeholder = null;
    }
  };

  const clearMarkers = () => {
    $$(".dragging", container).forEach((node) => node.classList.remove("dragging"));
    $$(".drop-before, .drop-after", container).forEach((node) =>
      node.classList.remove("drop-before", "drop-after"));
    removePlaceholder();
  };

  const makePlaceholder = () => {
    const el = document.createElement("div");
    if (container.id === "pageGrid") {
      el.className = "page-placeholder";
      el.innerHTML = `<span class="placeholder-page"></span><span class="placeholder-label">Drop here</span>`;
    } else {
      el.className = "mixed-placeholder";
      el.innerHTML = `<span class="placeholder-label">Drop here</span>`;
    }
    el.setAttribute("aria-hidden", "true");
    return el;
  };

  const showPlaceholder = (item, after) => {
    if (!placeholder) placeholder = makePlaceholder();
    if (after) item.after(placeholder);
    else item.before(placeholder);
  };

  container.addEventListener("dragstart", (event) => {
    const item = event.target.closest("[data-key]");
    if (!item || event.target.closest("button")) {
      event.preventDefault();
      return;
    }
    dragKey = item.dataset.key;
    dropTargetKey = null;
    dropAfter = false;
    state.reordering = true;
    item.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    try {
      event.dataTransfer.setData("text/plain", dragKey);
    } catch {
      /* ignore */
    }
  });

  container.addEventListener("dragend", () => {
    clearMarkers();
    dragKey = null;
    dropTargetKey = null;
    dropAfter = false;
    state.reordering = false;
  });

  container.addEventListener("dragover", (event) => {
    if (!dragKey) return;
    event.preventDefault();
    const item = event.target.closest("[data-key]");
    $$(".drop-before, .drop-after", container).forEach((node) =>
      node.classList.remove("drop-before", "drop-after"));
    if (!item) return; // pointer is over the ghost slot; keep it in place
    if (item.dataset.key === dragKey) {
      dropTargetKey = null;
      dropAfter = false;
      removePlaceholder();
      return;
    }
    const rect = item.getBoundingClientRect();
    const after = axis === "y"
      ? (event.clientY - rect.top) > rect.height / 2
      : (event.clientX - rect.left) > rect.width / 2;
    item.classList.add(after ? "drop-after" : "drop-before");
    dropTargetKey = item.dataset.key;
    dropAfter = after;
    showPlaceholder(item, after);
  });

  container.addEventListener("drop", (event) => {
    if (!dragKey) return;
    // This was a card/row move, not a file drop. Stop here so the panel-level
    // file handler cannot treat the dragged thumbnail as a new upload.
    event.preventDefault();
    event.stopPropagation();
    const items = getItems();
    const from = items.findIndex((item) => item.key === dragKey);
    if (from < 0) {
      clearMarkers();
      dragKey = null;
      dropTargetKey = null;
      state.reordering = false;
      return;
    }
    let to;
    let after;
    if (dropTargetKey) {
      to = items.findIndex((item) => item.key === dropTargetKey);
      after = dropAfter;
      if (to < 0) {
        to = items.length - 1;
        after = true;
      }
    } else {
      to = items.length - 1;
      after = true;
    }
    const [moved] = items.splice(from, 1);
    if (from < to) to -= 1;
    if (after) to += 1;
    items.splice(Math.max(0, Math.min(to, items.length)), 0, moved);
    state.landedKey = moved.key;
    clearMarkers();
    dragKey = null;
    dropTargetKey = null;
    dropAfter = false;
    state.reordering = false;
    render();
  });
}

/* -------------------------------------------------------------------- wiring */
function wireUp() {
  wireLightbox();

  // Page studio
  setupDropzone($("#pageDrop"), $("#pageInput"), uploadPages);
  setupPanelDrop($("#pages"), uploadPages, "x");
  $("#pageAdd").addEventListener("click", () => $("#pageInput").click());
  $("#pageClear").addEventListener("click", () => {
    if (!state.pages.length) return;
    if (window.confirm("Remove all pages from the workspace?")) {
      state.pages = [];
      renderPages();
      toast("Workspace cleared", "info");
    }
  });
  $("#pageBuild").addEventListener("click", buildPages);

  $("#pageGrid").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-act]");
    const card = event.target.closest(".page-card");
    if (!card) return;
    const index = state.pages.findIndex((page) => page.key === card.dataset.key);
    if (index < 0) return;
    const action = button
      ? button.dataset.act
      : event.target.closest(".thumb-wrap") ? "view" : null;
    if (!action) return;

    if (action === "view") {
      openLightbox(pageLightboxItems(), index);
    } else if (action === "rotate") {
      state.pages[index].rotation = (state.pages[index].rotation + 90) % 360;
      renderPages();
    } else if (action === "duplicate") {
      const copy = { ...state.pages[index], key: nextKey() };
      state.pages.splice(index + 1, 0, copy);
      renderPages();
      toast("Page duplicated", "info");
    } else if (action === "delete") {
      state.pages.splice(index, 1);
      renderPages();
    } else if (action === "left" && index > 0) {
      [state.pages[index - 1], state.pages[index]] = [state.pages[index], state.pages[index - 1]];
      renderPages();
    } else if (action === "right" && index < state.pages.length - 1) {
      [state.pages[index + 1], state.pages[index]] = [state.pages[index], state.pages[index + 1]];
      renderPages();
    }
  });

  enableReorder($("#pageGrid"), () => state.pages, renderPages, "x");

  // PDF -> images
  setupDropzone($("#imageDrop"), $("#imageInput"), uploadImagePdf);
  setupPanelDrop($("#images"), uploadImagePdf);
  $("#imageRemove").addEventListener("click", resetImagePdf);
  $("#imageConvert").addEventListener("click", convertImages);

  const qualityField = $("#qualityField");
  const syncQuality = () => {
    const needsQuality = $("#imageFormat").value !== "png";
    qualityField.hidden = !needsQuality;
  };
  $("#imageFormat").addEventListener("change", syncQuality);
  $("#imageQuality").addEventListener("input", () => {
    $("#qualityValue").textContent = $("#imageQuality").value;
  });
  syncQuality();

  const previewStrip = $("#imagePreview");
  const previewPage = (target) => target.closest(".preview-page");
  const openPreview = (target) => {
    const page = previewPage(target);
    if (!page || !state.image) return;
    openLightbox(imageLightboxItems(), Number(page.dataset.index) || 0);
  };
  const downloadPreviewPage = (target) => {
    const page = previewPage(target);
    if (!page || !state.image) return;
    const index = Number(page.dataset.index) || 0;
    const params = new URLSearchParams({
      format: $("#imageFormat").value,
      dpi: $("#imageDpi").value,
      quality: $("#imageQuality").value,
    });
    const link = document.createElement("a");
    link.href = `/api/images/page/${state.image.id}/${index}?${params.toString()}`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast(`Downloading page ${index + 1} as ${$("#imageFormat").value.toUpperCase()}…`, "info");
  };
  previewStrip.addEventListener("click", (event) => {
    if (event.target.closest('button[data-act="download"]')) {
      event.preventDefault();
      downloadPreviewPage(event.target);
      return;
    }
    if (event.target.closest('button[data-act="zoom"]') || event.target.closest("img[data-index]")) {
      event.preventDefault();
      openPreview(event.target);
    }
  });
  previewStrip.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (!event.target.closest("img[data-index]")) return;
    event.preventDefault();
    openPreview(event.target);
  });

  // Mixed content
  setupDropzone($("#mixedDrop"), $("#mixedInput"), uploadMixed);
  setupPanelDrop($("#mixed"), uploadMixed, "y");
  $("#mixedAdd").addEventListener("click", () => $("#mixedInput").click());
  $("#mixedClear").addEventListener("click", () => {
    if (!state.mixed.length) return;
    if (window.confirm("Remove all files from this list?")) {
      state.mixed = [];
      renderMixed();
      toast("List cleared", "info");
    }
  });
  $("#mixedBuild").addEventListener("click", buildMixed);

  const betaBtn = $("#mixedBeta");
  if (betaBtn) {
    betaBtn.addEventListener("click", () => {
      const note = $("#mixedBetaNote");
      const open = note.hidden;
      note.hidden = !open;
      betaBtn.setAttribute("aria-expanded", String(open));
    });
  }

  $("#mixedList").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-act]");
    if (!button) return;
    const row = button.closest(".mixed-item");
    const index = state.mixed.findIndex((item) => item.key === row.dataset.key);
    if (index < 0) return;
    const action = button.dataset.act;
    if (action === "preview") {
      openLightbox(mixedLightboxItems(), index);
    } else if (action === "up" && index > 0) {
      [state.mixed[index - 1], state.mixed[index]] = [state.mixed[index], state.mixed[index - 1]];
      renderMixed();
    } else if (action === "down" && index < state.mixed.length - 1) {
      [state.mixed[index + 1], state.mixed[index]] = [state.mixed[index], state.mixed[index + 1]];
      renderMixed();
    } else if (action === "remove") {
      state.mixed.splice(index, 1);
      renderMixed();
    }
  });

  enableReorder($("#mixedList"), () => state.mixed, renderMixed, "y");

  // Navigation highlight
  const links = $$(".nav a");
  const sections = ["pages", "images", "mixed"]
    .map((id) => document.getElementById(id))
    .filter(Boolean);
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        links.forEach((link) => link.classList.toggle(
          "active", link.getAttribute("href") === "#" + entry.target.id));
      });
    }, { rootMargin: "-42% 0px -52% 0px", threshold: 0 });
    sections.forEach((section) => observer.observe(section));
  }

  // Stop the local server
  $("#shutdownBtn").addEventListener("click", async () => {
    if (!window.confirm("Stop the PDF Studio server? You can close this tab afterwards.")) return;
    try {
      await api("/api/shutdown", { method: "POST" });
    } catch {
      /* server may exit before answering */
    }
    document.body.innerHTML = `
      <div style="min-height:100vh;display:grid;place-items:center;text-align:center;padding:30px">
        <div>
          <h1 style="margin:0 0 8px;font-size:26px">PDF Studio has stopped</h1>
          <p style="margin:0;color:#98a3c0">You can close this tab.</p>
        </div>
      </div>`;
  });

  // Never let the browser navigate away when a file is dropped outside a zone
  ["dragover", "drop"].forEach((type) => {
    document.addEventListener(type, (event) => {
      if (!event.target.closest(".dropzone")) event.preventDefault();
    });
  });
}

document.addEventListener("DOMContentLoaded", wireUp);
