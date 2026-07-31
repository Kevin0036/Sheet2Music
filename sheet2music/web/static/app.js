"use strict";

// 阶段显示顺序（key 与后端 stage 对应）
const STAGES = [
  ["running_homr", "HOMR 逐页识别"],
  ["repairing_musicxml", "修复 MusicXML 拍号/时值"],
  ["exporting_midi", "导出并归一化 MIDI"],
  ["rendering_mp3", "渲染 MP3"],
  ["completed", "完成"],
  ["failed", "失败"],
];

const $ = (id) => document.getElementById(id);

const dropZone = $("drop-zone");
const fileInput = $("file-input");
const bpmInput = $("bpm");
const timeSigInput = $("time-signature");
const gpuInput = $("use-gpu");
const outputsFieldset = $("outputs");
const convertBtn = $("convert-btn");
const resetBtn = $("reset-btn");

let currentJob = null;
let pollTimer = null;

function setParamsEnabled(enabled) {
  bpmInput.disabled = !enabled;
  timeSigInput.disabled = !enabled;
  gpuInput.disabled = !enabled;
  outputsFieldset.disabled = !enabled;
  convertBtn.disabled = !enabled || !isParamsValid();
}

function isParamsValid() {
  const bpm = Number(bpmInput.value);
  return Number.isInteger(bpm) && bpm > 0;
}

function selectedOutputs() {
  return Array.from(outputsFieldset.querySelectorAll("input:checked")).map((c) => c.value);
}

function resetToEmpty() {
  currentJob = null;
  stopPolling();
  $("preview-area").hidden = true;
  $("progress").hidden = true;
  $("results").hidden = true;
  $("error-box").hidden = true;
  $("drop-zone").hidden = false;
  fileInput.value = "";
  setParamsEnabled(false);
  resetBtn.disabled = true;
}

function resetToPreview() {
  $("progress").hidden = true;
  $("results").hidden = true;
  setParamsEnabled(true);
  resetBtn.disabled = false;
}

async function handleFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    alert("请选择 PDF 文件");
    return;
  }
  setParamsEnabled(false);
  const form = new FormData();
  form.append("file", file);
  try {
    const resp = await fetch("/api/preview", { method: "POST", body: form });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "上传失败");
    currentJob = data.job_id;
    $("preview-img").src = data.preview_url + "?t=" + Date.now();
    $("file-meta").textContent = data.filename + " · 已上传，等待转换";
    $("preview-area").hidden = false;
    $("drop-zone").hidden = true;
    resetToPreview();
  } catch (err) {
    alert("上传/预览失败：" + err.message);
    resetToEmpty();
  }
}

async function startConvert() {
  if (!currentJob || !isParamsValid()) return;
  setParamsEnabled(false);
  resetBtn.disabled = true;
  showProgress();
  try {
    const resp = await fetch("/api/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        job_id: currentJob,
        bpm: Number(bpmInput.value),
        time_signature: timeSigInput.value.trim() || "4/4",
        outputs: selectedOutputs(),
        use_gpu: gpuInput.checked,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "启动失败");
    pollTimer = setInterval(pollJob, 1000);
  } catch (err) {
    failProgress("启动失败：" + err.message);
  }
}

async function pollJob() {
  if (!currentJob) return;
  const resp = await fetch(`/api/jobs/${currentJob}`);
  const job = await resp.json();
  if (!resp.ok) {
    failProgress("查询任务状态失败");
    return;
  }
  renderStages(job);
  if (job.status === "completed") {
    stopPolling();
    showResults(job);
  } else if (job.status === "failed") {
    stopPolling();
    failProgress(job.error || "转换失败");
  }
}

function showProgress() {
  $("progress").hidden = false;
  $("results").hidden = true;
  $("error-box").hidden = true;
  $("stages").innerHTML = STAGES.map(
    ([key, label]) => `<li data-stage="${key}">${label}</li>`
  ).join("");
}

function renderStages(job) {
  const activeIndex = STAGES.findIndex(([key]) => key === job.stage);
  document.querySelectorAll("#stages li").forEach((li) => {
    const index = STAGES.findIndex(([key]) => key === li.dataset.stage);
    li.className = index < activeIndex ? "done" : index === activeIndex ? "active" : "";
    if (li.dataset.stage === "running_homr") {
      const base = "HOMR 逐页识别";
      li.textContent =
        job.progress && job.status === "running"
          ? `${base}（${job.progress.current}/${job.progress.total}）`
          : base;
    }
  });
}

function failProgress(message) {
  const box = $("error-box");
  box.hidden = false;
  box.textContent = message;
  resetBtn.disabled = false;
}

function showResults(job) {
  $("results").hidden = false;
  $("error-box").hidden = true;

  const downloads = $("downloads");
  downloads.innerHTML = "";
  const wanted = new Set(["musicxml", "midi", "mp3", "zip"]);
  job.artifacts
    .filter((a) => wanted.has(a.kind))
    .forEach((a) => {
      const link = document.createElement("a");
      link.href = `/api/jobs/${job.job_id}/artifacts/${a.name}`;
      link.download = a.name;
      link.textContent = `下载 ${a.name}（${formatSize(a.size)}）`;
      downloads.appendChild(link);
    });

  const report = job.report || {};
  const parts = [];
  if (report.num_measures != null) parts.push(`共 ${report.num_measures} 小节`);
  if (report.num_parts != null) parts.push(`${report.num_parts} 个声部`);
  if (report.page_count != null) parts.push(`识别 ${report.page_count_recognized ?? report.page_count} 页`);
  if ((report.skipped_pages || []).length > 0) parts.push(`跳过 ${report.skipped_pages.length} 页`);
  $("summary").textContent = parts.join(" · ");

  const skipped = report.skipped_pages || [];
  const warnings = $("warnings");
  if (skipped.length > 0) {
    warnings.hidden = false;
    warnings.textContent =
      `已跳过 ${skipped.length} 页无法识别的内容（可能是歌词/文本页）：\n` +
      skipped.map((s) => `· ${s.page}: ${s.error}`).join("\n");
  } else {
    warnings.hidden = true;
  }

  resetBtn.disabled = false;
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function resetJob() {
  if (currentJob) {
    try {
      await fetch(`/api/jobs/${currentJob}/reset`, { method: "POST" });
    } catch (_) {
      /* 忽略：清空本地状态即可 */
    }
  }
  resetToEmpty();
}

// ---- 事件绑定 ----
$("pick-file").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  if (fileInput.files[0]) handleFile(fileInput.files[0]);
});

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});
["dragenter", "dragover"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.add("dragging");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropZone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragging");
  })
);
dropZone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) handleFile(file);
});

bpmInput.addEventListener("input", () => {
  convertBtn.disabled = !isParamsValid();
});
convertBtn.addEventListener("click", startConvert);
resetBtn.addEventListener("click", resetJob);

// ---- 环境检查面板 ----
let weightsPollTimer = null;

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[c]);
}

function statusItem(ok, label, detail, hint) {
  const hintHtml = hint ? `<span class="hint">${esc(hint)}</span>` : "";
  return (
    `<li class="${ok ? "ok" : "bad"}"><span class="mark">${ok ? "✓" : "✗"}</span> ` +
    `${esc(label)}<span class="detail">${esc(detail)}</span>${hintHtml}</li>`
  );
}

async function loadSystemStatus() {
  try {
    const resp = await fetch("/api/system/status");
    if (!resp.ok) return;
    renderSystemStatus(await resp.json());
  } catch (_) {
    /* 状态面板加载失败不阻塞主流程 */
  }
}

function renderSystemStatus(status) {
  const items = [];
  if (status.homr_root.ok) {
    items.push(statusItem(true, "HOMR 源码", status.homr_root.path, null));
  } else {
    items.push(statusItem(false, "HOMR 源码", status.homr_root.error, null));
  }
  if (status.weights.ok) {
    items.push(statusItem(true, "模型权重", "已就绪", null));
  } else if (!status.homr_root.ok) {
    items.push(statusItem(false, "模型权重", "未检查（HOMR 源码缺失）", null));
  } else {
    items.push(
      statusItem(
        false,
        "模型权重",
        `缺失 ${status.weights.missing.length} 个文件（首次使用需下载，约 150MB）`,
        null
      )
    );
  }
  if (status.gpu) {
    const providers = (status.gpu.providers || []).join(", ") || "未检测到 ONNX Runtime GPU provider";
    const detail = status.gpu.fp16_weights_ok
      ? providers + " · FP16 已就绪"
      : providers + ` · 缺失 FP16 ${status.gpu.missing_fp16.length} 个文件`;
    items.push(statusItem(status.gpu.ok, "GPU 加速", detail, status.gpu.hint));
  }
  status.python_deps.forEach((dep) =>
    items.push(statusItem(dep.ok, dep.name, dep.ok ? "已安装" : "未安装", dep.ok ? null : `pip install ${dep.name}`))
  );
  status.binaries.forEach((bin) =>
    items.push(statusItem(bin.ok, bin.name, bin.ok ? bin.path : "未找到", bin.hint))
  );
  $("system-status").innerHTML = items.join("");

  const badge = $("system-badge");
  badge.className = "badge " + (status.all_ok ? "ok" : "warn");
  badge.textContent = status.all_ok ? "正常" : "需处理";

  $("weights-section").hidden = !status.weights.download_needed;
}

async function startWeightDownload() {
  const btn = $("weights-download-btn");
  const statusEl = $("weights-status");
  btn.disabled = true;
  statusEl.className = "weights-status";
  statusEl.textContent = "正在启动下载…";
  $("weights-progress").hidden = false;
  try {
    const resp = await fetch("/api/system/weights/download", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok || !data.started) {
      statusEl.textContent = data.state && data.state.running ? "已在下载中…" : "启动失败";
      btn.disabled = false;
    }
    pollWeights();
  } catch (err) {
    statusEl.textContent = "启动失败：" + err.message;
    btn.disabled = false;
  }
}

function pollWeights() {
  if (weightsPollTimer) clearInterval(weightsPollTimer);
  weightsPollTimer = setInterval(async () => {
    try {
      const resp = await fetch("/api/system/weights/download");
      if (!resp.ok) return;
      const state = await resp.json();
      const bar = $("weights-progress-bar");
      const statusEl = $("weights-status");
      if (state.running) {
        bar.style.width = (state.percent || 0) + "%";
        const mb = (n) => (n / 1024 / 1024).toFixed(1);
        const size =
          state.total_bytes
            ? `${mb(state.downloaded_bytes)}/${mb(state.total_bytes)} MB `
            : "";
        statusEl.textContent = `${state.current_file || ""} ${size}${state.percent}%`;
      } else {
        clearInterval(weightsPollTimer);
        weightsPollTimer = null;
        $("weights-progress").hidden = true;
        if (state.error) {
          statusEl.textContent = "下载失败：" + state.error;
          statusEl.className = "weights-status error";
        } else {
          statusEl.textContent = "下载完成 ✓";
        }
        $("weights-download-btn").disabled = false;
        loadSystemStatus();
      }
    } catch (_) {
      /* 轮询失败等下一拍 */
    }
  }, 1000);
}

$("system-toggle").addEventListener("click", () => {
  const panel = $("system-panel");
  panel.classList.toggle("collapsed");
  $("system-toggle").setAttribute("aria-expanded", String(!panel.classList.contains("collapsed")));
});
$("weights-download-btn").addEventListener("click", startWeightDownload);

resetToEmpty();
loadSystemStatus();
