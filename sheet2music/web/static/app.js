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
STAGES.splice(1, 0, ["reidentifying_region", "区域二次识别"]);
STAGES.splice(2, 0, ["finalizing_review", "应用审批决定"]);
STAGES.splice(3, 0, ["awaiting_review", "等待人工审查"]);
STAGES.splice(
  0,
  STAGES.length,
  ["running_homr", "HOMR 逐页识别"],
  ["repairing_musicxml", "修复 MusicXML"],
  ["automatic_reidentification", "自动检查小节边界"],
  ["automatic_upload_recognition", "识别补充谱表图片"],
  ["reidentifying_region", "区域二次识别"],
  ["finalizing_review", "应用审批决定"],
  ["awaiting_review", "等待人工审查"],
  ["exporting_midi", "导出 MIDI"],
  ["rendering_mp3", "渲染 MP3"],
  ["completed", "完成"],
  ["failed", "失败"]
);

const $ = (id) => document.getElementById(id);

const dropZone = $("drop-zone");
const fileInput = $("file-input");
const bpmInput = $("bpm");
const timeSigInput = $("time-signature");
const gpuInput = $("use-gpu");
const outputsFieldset = $("outputs");
const convertBtn = $("convert-btn");
const resetBtn = $("reset-btn");
const reviewPanel = $("review-panel");
const reviewFindings = $("review-findings");
const approveReviewBtn = $("approve-review-btn");
const reviewStatus = $("review-status");
const reviewPreserveAllBtn = $("review-preserve-all-btn");
const reviewApplySuggestionsBtn = $("review-apply-suggestions-btn");
const reviewSelectionStatus = $("review-selection-status");
const reviewChanges = $("review-changes");
const {
  reviewSelectionState,
  hasAutomaticRepairSuggestion,
  suggestedReviewActions,
  automaticRepairCount,
  reviewChangeSummaryText,
  actionsForFinding,
  formatBeatLocation,
  formatTimingSummary,
  preservableFindingCount,
  autoResolutionSummary,
  autoReviewReady,
  batchActions,
  candidateSummaryText,
} = window.Sheet2MusicReviewState;
const { saveJobId, loadJobId, clearJobId, initialJobId } = window.Sheet2MusicJobSession;
const JOB_SESSION_KEY = "sheet2music.current-job";

let currentJob = null;
let pollTimer = null;
let currentAutoResolution = null;

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
  clearJobId(sessionStorage, JOB_SESSION_KEY);
  stopPolling();
  regionUploadBusy = false;
  currentAutoResolution = null;
  $("preview-area").hidden = true;
  $("progress").hidden = true;
  $("results").hidden = true;
  reviewPanel.hidden = true;
  reviewFindings.innerHTML = "";
  reviewStatus.textContent = "";
  $("error-box").hidden = true;
  $("drop-zone").hidden = false;
  fileInput.value = "";
  setParamsEnabled(false);
  resetBtn.disabled = true;
}

function resetToPreview() {
  $("progress").hidden = true;
  $("results").hidden = true;
  reviewPanel.hidden = true;
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
    saveJobId(sessionStorage, JOB_SESSION_KEY, currentJob);
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
    startPolling();
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
  } else if (job.status === "awaiting_review") {
    stopPolling();
    showReview(job);
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
    if (li.dataset.stage === "automatic_reidentification") {
      const progress = job.progress || {};
      const base = "自动检查小节边界";
      li.textContent = job.stage === "automatic_reidentification" && progress.total
        ? `${base}（${progress.current}/${progress.total}，已解决 ${progress.resolved || 0}，待审 ${progress.needs_review || 0}）`
        : base;
    }
  });
}

function startPolling() {
  stopPolling();
  pollJob();
  pollTimer = setInterval(pollJob, 1000);
}

const FINDING_KIND_LABELS = {
  time_signature_change: "拍号变化",
  missing_time_signature: "可能漏识别拍号",
  conflicting_time_signature: "不同声部的拍号不一致",
  clef_change: "谱号变化",
  clef_change_at_measure_start: "小节开头谱号变化",
  clef_change_mid_measure: "小节中途谱号变化",
  clef_mismatch: "谱号与结构计划不一致",
  missing_clef: "可能漏识别谱号",
  timing_structure: "小节时值异常",
  timing_measure_overflow: "音符超出小节边界",
  timing_cursor_invalid: "小节时间结构无效",
  timing_notation_mismatch: "音符时值与记谱不一致",
};

const FINDING_FIELD_LABELS = {
  signature: "拍号",
  sign: "谱号",
  line: "谱号所在位置",
  cursor: "当前时值",
  expected_ticks: "目标时值",
  values: "识别到的拍号",
  parts: "各声部结果",
  action: "处理方式",
};

const FINDING_ACTION_LABELS = {
  review: "请人工确认",
  compress: "按明确的音符记谱自动压缩",
  reidentify: "上传放大图进行二次识别",
};

const CLEF_LABELS = {
  G: "高音谱号",
  F: "低音谱号",
  C: "中音谱号",
};

const FINDING_REASON_LABELS = {
  "unconfirmed time-signature change affects measure boundaries":
    "拍号发生变化，可能影响小节边界，请确认这是谱子本身的变化还是识别错误。",
  "different parts report different effective time signatures":
    "不同声部识别出了不同的拍号，请对照谱面确认实际拍号。",
  "one part contains multiple time signatures in one measure":
    "同一声部的小节内出现了多个拍号，请对照谱面确认实际拍号。",
  "clef differs from the reviewed structure plan":
    "识别出的谱号与结构计划不一致，可能影响音符的音高解释。",
  "unconfirmed clef change may alter staff pitch interpretation":
    "谱号发生变化，可能影响后续音符的音高解释，请确认这是谱子本身的变化还是识别错误。",
  "reviewed structure plan requires a time-signature declaration here":
    "结构计划显示这里应该出现新的拍号，但识别结果中没有明确记录，请检查该区域。",
};

function formatSignature(value) {
  const text = String(value ?? "").trim();
  if (text === "inherited") return "沿用前一小节的拍号";
  if (/^\d+\/\d+$/.test(text)) return `${text} 拍`;
  return "未明确的拍号";
}

function formatClef(sign, line) {
  const label = CLEF_LABELS[String(sign ?? "").toUpperCase()] || "未明确的谱号";
  return line == null || line === "" ? label : `${label}（第 ${line} 线）`;
}

function formatFindingField(key, value) {
  if (key === "signature") return formatSignature(value);
  if (key === "sign") return CLEF_LABELS[String(value ?? "").toUpperCase()] || "未明确的谱号";
  if (key === "action") return FINDING_ACTION_LABELS[value] || "请人工确认";
  if (key === "cursor") return `${value} 个时值单位`;
  if (key === "expected_ticks") return `${value} 个时值单位`;
  if (key === "line") return `第 ${value} 线`;
  if (key === "staff") return `第 ${value} 谱表`;
  return String(value ?? "未提供");
}

function formatFindingValue(value) {
  if (value == null) return "未提供";
  if (Array.isArray(value)) {
    return value.map((item) => (typeof item === "object" ? formatFindingValue(item) : formatSignature(item))).join("、");
  }
  if (typeof value !== "object") return String(value);

  if (Object.prototype.hasOwnProperty.call(value, "signature")) {
    return formatSignature(value.signature);
  }
  if (Object.prototype.hasOwnProperty.call(value, "sign")) {
    return formatClef(value.sign, value.line);
  }
  if (Object.prototype.hasOwnProperty.call(value, "cursor") || Object.prototype.hasOwnProperty.call(value, "expected_ticks")) {
    const current = value.cursor == null ? "未提供" : `${value.cursor} 个时值单位`;
    const expected = value.expected_ticks == null ? "未提供" : `${value.expected_ticks} 个时值单位`;
    return `当前小节为 ${current}，目标为 ${expected}`;
  }
  if (value.parts && typeof value.parts === "object") {
    return Object.entries(value.parts)
      .map(([part, signature]) => `声部 ${part}：${formatSignature(signature)}`)
      .join("；");
  }
  if (Array.isArray(value.values)) {
    return `识别到的拍号：${value.values.map(formatSignature).join("、")}`;
  }
  if (Object.prototype.hasOwnProperty.call(value, "action")) {
    return FINDING_ACTION_LABELS[value.action] || "请人工确认";
  }

  return Object.entries(value)
    .map(([key, item]) => `${FINDING_FIELD_LABELS[key] || "相关信息"}：${formatFindingField(key, item)}`)
    .join("；");
}

function formatFindingReason(reason) {
  const text = String(reason || "").trim();
  if (!text) return "该结构信息可能影响播放，请结合谱面确认。";
  if (/[一-鿿]/.test(text)) return text;
  if (FINDING_REASON_LABELS[text]) return FINDING_REASON_LABELS[text];
  const timingPrefix = "recognition timing structure requires review: ";
  if (text.startsWith(timingPrefix)) {
    const detail = text.slice(timingPrefix.length);
    if (detail.includes("one or more staff/voice lanes exceed the target boundary")) {
      return "同一小节内某个声部的时值超出了小节边界，可能是识别结果有误，请检查该区域。";
    }
    const cursorMatch = detail.match(/^cursor=(-?\d+), expected=(-?\d+)$/);
    if (cursorMatch) {
      return `小节内的音符时值与拍号不一致：当前为 ${cursorMatch[1]} 个时值单位，目标为 ${cursorMatch[2]} 个时值单位。`;
    }
    return "小节内的音符时值或声部游标与拍号不一致，可能是识别结果有误，请检查该区域。";
  }
  return "该结构信息可能影响播放，请结合谱面确认。";
}

function formatFindingKind(kind) {
  return FINDING_KIND_LABELS[kind] || "结构信息需要确认";
}

function formatMeasureRange(start, end, displayStart, displayEnd, mappingConfidence) {
  if (start == null || end == null) return "全谱小节范围未提供";
  const hasDisplay = mappingConfidence === "high"
    && displayStart != null
    && displayEnd != null;
  if (hasDisplay) {
    const printed = displayStart === displayEnd
      ? `印刷第 ${displayStart} 小节`
      : `印刷第 ${displayStart} 至第 ${displayEnd} 小节`;
    const ordinal = start === end
      ? `内部序号 ${start}`
      : `内部序号 ${start} 至 ${end}`;
    return `${printed}（${ordinal}）`;
  }
  if (mappingConfidence === "unknown") {
    return start === end
      ? `内部序号 ${start} 小节`
      : `内部序号 ${start} 至 ${end} 小节`;
  }
  return start === end ? `全谱第 ${start} 小节` : `全谱第 ${start} 至第 ${end} 小节`;
}

function formatStaffRegion(staff, observed) {
  if (staff == null || staff === "") {
    const affected = observed && observed.affected_staffs;
    if (Array.isArray(affected) && affected.length === 1) return formatStaffRegion(affected[0]);
    return "上下谱表均涉及";
  }
  const staffNumber = Number(staff);
  if (staffNumber === 1) return "右手区域（上方谱表，第 1 谱表）";
  if (staffNumber === 2) return "左手区域（下方谱表，第 2 谱表）";
  return `第 ${staff} 谱表（左右手区域需结合谱面确认）`;
}

function formatFindingLocation(finding, pages) {
  const parts = [pages];
  if (finding.measure_start === finding.measure_end && finding.offset_beats != null) {
    parts.push(formatBeatLocation(finding));
  } else {
    parts.push(formatMeasureRange(
      finding.measure_start,
      finding.measure_end,
      finding.display_measure_number,
      finding.display_measure_number,
      finding.number_mapping_confidence,
    ));
  }
  parts.push(formatStaffRegion(finding.staff, finding.observed));
  return parts.join("；");
}

function formatFindingObserved(finding) {
  if (String(finding.kind || "").startsWith("timing_")) {
    return formatTimingSummary(finding.observed);
  }
  if (String(finding.kind || "").startsWith("clef_")) {
    const observed = finding.observed || {};
    const previous = observed.previous_clef;
    const current = observed.observed_clef || observed;
    const currentText = formatClef(current.sign, current.line);
    return previous
      ? `原先为 ${formatClef(previous.sign, previous.line)}，当前位置识别为 ${currentText}`
      : `当前位置识别为 ${currentText}`;
  }
  const value = formatFindingValue(finding.observed);
  if (["time_signature_change", "missing_time_signature", "clef_change", "clef_mismatch", "missing_clef"].includes(finding.kind)) {
    return `当前识别为：${value}`;
  }
  return value;
}

function formatFindingSuggestion(finding) {
  const suggestion = finding.suggestion || {};
  if (suggestion.action === "compress") {
    return `根据 ${suggestion.corrected_note_count || 0} 个音符的类型、附点或连音标记，将小节修正为 ${suggestion.resulting_beats} 拍。`;
  }
  if (suggestion.action === "review") {
    if (finding.kind === "timing_structure") return "建议对照原始谱面检查小节内的音符时值。";
    if (finding.kind === "conflicting_time_signature") return "建议对照原始谱面确认各声部的实际拍号。";
    return "暂无自动修复结果，建议对照原始谱面人工确认。";
  }
  const value = formatFindingValue(suggestion);
  if (["time_signature_change", "missing_time_signature", "clef_change", "clef_mismatch", "missing_clef"].includes(finding.kind)) {
    return `建议改为：${value}`;
  }
  return value;
}

function showReviewChanges(report) {
  const changes = report && report.region_reidentification && report.region_reidentification.analysis_changes;
  if (!changes) {
    reviewChanges.hidden = true;
    reviewChanges.textContent = "";
    return;
  }
  const newFindings = Array.isArray(changes.new_findings) ? changes.new_findings : [];
  const resolvedFindings = Array.isArray(changes.resolved_findings) ? changes.resolved_findings : [];
  const describe = (finding) => formatMeasureRange(finding.measure_start, finding.measure_end);
  const extraLines = [];
  if (resolvedFindings.length) {
    extraLines.push(`本次已解决：${resolvedFindings.map(describe).join("、")}。`);
  }
  if (newFindings.length) {
    extraLines.push(`本次新增需要审阅：${newFindings.map(describe).join("、")}。`);
  }
  reviewChanges.textContent = [reviewChangeSummaryText(changes), ...extraLines].join(" ");
  reviewChanges.hidden = false;
}

function batchMeasureText(batch) {
  const range = Array.isArray(batch.context_range) ? batch.context_range : [];
  if (range.length !== 2) return "小节范围未明确";
  return range[0] === range[1]
    ? `第 ${range[0]} 小节`
    : `第 ${range[0]}–${range[1]} 小节`;
}

function renderAutoResolution(job, autoResolution) {
  const section = $("auto-review");
  const batches = Array.isArray(autoResolution && autoResolution.batches)
    ? autoResolution.batches
    : [];
  currentAutoResolution = autoResolution && typeof autoResolution === "object"
    ? autoResolution
    : { batches: [] };
  section.hidden = batches.length === 0;
  if (!batches.length) return;

  const summary = autoResolutionSummary(currentAutoResolution);
  $("auto-review-summary").textContent = summary.text;
  const resolved = batches.filter((batch) => batch.status === "auto_resolved");
  const audit = $("auto-resolved-audit");
  audit.hidden = resolved.length === 0;
  $("auto-resolved-list").innerHTML = resolved.map((batch) => `
    <div class="auto-audit-row">
      <strong>第 ${esc(batch.page_number)} 页 · ${esc(batchMeasureText(batch))}</strong>
      <span>已采用${batch.selected_candidate ? `“${esc(candidateSummaryText({ variant: batch.selected_candidate, validation: { accepted: true } }).split("：")[0])}”` : "可靠候选"}，并通过整谱回归检查。</span>
    </div>`).join("");

  const unresolved = batches.filter((batch) => (
    batch.status === "needs_choice" || batch.status === "needs_upload" || batch.status === "failed"
  ));
  $("auto-review-batches").innerHTML = unresolved.map((batch) => {
    const actions = batchActions(batch);
    const cropUrl = `/api/jobs/${encodeURIComponent(job.job_id)}/auto-resolution/${encodeURIComponent(batch.batch_id)}/crop`;
    const candidates = (batch.attempts || []).filter((attempt) => (
      attempt.status === "succeeded" && attempt.validation && attempt.validation.accepted
    ));
    const candidateRows = candidates.map((candidate) => `
      <div class="auto-candidate-row">
        <p>${esc(candidateSummaryText(candidate))}</p>
        ${actions.includes("select") ? `<button type="button" class="secondary auto-select-btn" data-candidate-id="${esc(candidate.variant)}">采用此结果</button>` : ""}
      </div>`).join("");
    const upload = actions.includes("upload") ? `
      <div class="auto-upload-controls">
        <label>完整谱表图片
          <input type="file" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp" data-auto-file />
        </label>
        <button type="button" class="secondary auto-upload-btn">上传并重新识别</button>
        <span class="auto-action-status" data-auto-status></span>
      </div>` : "";
    return `
      <article class="auto-batch" data-batch-id="${esc(batch.batch_id)}">
        <div class="auto-batch-heading">
          <div>
            <h3>第 ${esc(batch.page_number)} 页 · ${esc(batchMeasureText(batch))}</h3>
            <p>第 ${esc(Number(batch.system_index) + 1)} 行，左右手完整谱表区域</p>
          </div>
          <span class="auto-batch-state">${batch.status === "needs_choice" ? "请选择识别结果" : "需要补充图片"}</span>
        </div>
        <img class="auto-crop" src="${cropUrl}?t=${Date.now()}" alt="需要审核的完整谱表区域" />
        ${candidateRows || (batch.status === "needs_choice" ? '<p class="auto-empty">没有可展示的有效候选，请重新尝试。</p>' : "")}
        ${actions.includes("retry") ? '<button type="button" class="secondary auto-retry-btn">重新运行未尝试方案</button>' : ""}
        ${upload}
      </article>`;
  }).join("");

  section.querySelectorAll(".auto-select-btn").forEach((button) => {
    button.addEventListener("click", () => selectAutoCandidate(job, button));
  });
  section.querySelectorAll(".auto-retry-btn").forEach((button) => {
    button.addEventListener("click", () => retryAutoBatch(job, button));
  });
  section.querySelectorAll(".auto-upload-btn").forEach((button) => {
    button.addEventListener("click", () => uploadAutoBatch(job, button));
  });
}

function showReview(job) {
  regionUploadBusy = false;
  const analysis = job.analysis || (job.report && job.report.analysis) || {};
  const autoResolution = job.report && job.report.auto_resolution;
  const automaticTargets = new Set(
    (autoResolution && autoResolution.batches || []).flatMap((batch) => batch.target_measures || [])
  );
  const findings = (analysis.findings || []).filter((finding) => (
    finding.severity === "high"
    && !(finding.kind === "timing_measure_overflow" && automaticTargets.has(finding.measure_start))
  ));
  const suggestedActions = suggestedReviewActions(findings);
  const repairCount = automaticRepairCount(findings);
  const preserveCount = preservableFindingCount(findings);
  reviewPanel.hidden = false;
  $("progress").hidden = false;
  $("results").hidden = true;
  setParamsEnabled(false);
  resetBtn.disabled = false;
  reviewStatus.textContent = "";
  reviewSelectionStatus.textContent = "";
  renderAutoResolution(job, autoResolution);
  $("review-summary").textContent = findings.length
    ? `另有 ${findings.length} 个谱号、拍号或结构疑点需要确认。`
    : "没有其他结构疑点需要逐项确认。";
  showReviewChanges(job.report);

  reviewFindings.innerHTML = findings
    .map((finding, index) => {
      const id = esc(finding.id);
      const pageNumbers = finding.page_numbers || [];
      const pages = pageNumbers.length ? pageNumbers.map((page) => `第 ${page} 页`).join("、") : "来源页未知";
      const sourcePage = (finding.page_numbers || [])[0];
      const preview = sourcePage
        ? `/api/jobs/${encodeURIComponent(job.job_id)}/pages/${encodeURIComponent(sourcePage)}`
        : "";
      const availableActions = actionsForFinding(finding);
      const preserveAction = availableActions.includes("preserve")
        ? `<label><input type="radio" name="review-${id}" value="preserve" data-review-action /> 保留谱面中的当前变化</label>`
        : "";
      const correctAction = availableActions.includes("correct") && hasAutomaticRepairSuggestion(finding.suggestion)
        ? `<label><input type="radio" name="review-${id}" value="correct" data-review-action /> 采用修复建议</label>`
        : "";
      const reidentifyAction = availableActions.includes("reidentify")
        ? `<label><input type="radio" name="review-${id}" value="reidentify" data-review-action /> 上传放大图二次识别</label>`
        : "";
      return `
        <article class="finding" data-finding-id="${id}">
          <div class="finding-topline">
            <h3>疑点 ${index + 1} · ${esc(formatFindingKind(finding.kind))}</h3>
            <span class="finding-range">${esc(formatMeasureRange(finding.measure_start, finding.measure_end))}</span>
          </div>
          <dl class="finding-details">
            <div><dt>需要审阅的位置</dt><dd>${esc(formatFindingLocation(finding, pages))}</dd></div>
            <div><dt>当前识别结果</dt><dd>${esc(formatFindingObserved(finding))}</dd></div>
            <div><dt>修复建议</dt><dd>${esc(formatFindingSuggestion(finding))}</dd></div>
            <div><dt>原因</dt><dd>${esc(formatFindingReason(finding.reason))}</dd></div>
          </dl>
          ${preview ? `<img class="finding-preview" src="${preview}?t=${Date.now()}" alt="来源谱面预览" />` : `<p class="finding-preview-empty">该疑点对应${esc(pages)}，原始页面仍保存在任务工作区。</p>`}
          <fieldset class="finding-actions">
            <legend>审批决定</legend>
            ${preserveAction}${correctAction}${reidentifyAction}
          </fieldset>
          <div class="region-controls" hidden>
            <label>区域图片
              <input type="file" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp" data-region-file />
            </label>
            <div class="range-inputs">
              <label>起始小节 <input type="number" min="1" value="${esc(finding.measure_start)}" data-region-start /></label>
              <label>结束小节 <input type="number" min="1" value="${esc(finding.measure_end)}" data-region-end /></label>
            </div>
            <button type="button" class="secondary region-upload-btn">上传并二次识别</button>
            <span class="region-status" data-region-status></span>
          </div>
        </article>`;
    })
    .join("");

  $("review-bulk-actions").hidden = findings.length === 0;

  reviewFindings.querySelectorAll("[data-review-action]").forEach((input) => {
    input.addEventListener("change", () => {
      const finding = input.closest(".finding");
      const controls = finding.querySelector(".region-controls");
      controls.hidden = input.value !== "reidentify";
      if (input.value !== "reidentify") {
        finding.querySelector("[data-region-status]").textContent = "";
      }
      updateReviewReady();
    });
  });
  reviewFindings.querySelectorAll(".region-upload-btn").forEach((button) => {
    button.addEventListener("click", () => uploadRegion(job, button.closest(".finding")));
  });
  reviewPreserveAllBtn.onclick = () => applyReviewActions(
    findings.map((finding) => (actionsForFinding(finding).includes("preserve") ? "preserve" : null))
  );
  reviewPreserveAllBtn.disabled = preserveCount === 0;
  reviewPreserveAllBtn.textContent = preserveCount
    ? `批量保留 ${preserveCount} 项可保留变化`
    : "没有可直接保留的项目";
  reviewApplySuggestionsBtn.disabled = repairCount === 0;
  reviewApplySuggestionsBtn.textContent = repairCount
    ? `一键采用 ${repairCount} 项可用修复`
    : "没有可用的自动修复";
  reviewApplySuggestionsBtn.onclick = () => applyReviewActions(suggestedActions);
  approveReviewBtn.onclick = () => submitReview(job);
  updateReviewReady();
}

function applyReviewActions(actions) {
  Array.from(reviewFindings.querySelectorAll(".finding")).forEach((finding, index) => {
    const action = actions[index];
    const input = finding.querySelector(`[data-review-action][value="${action}"]`);
    if (!input) return;
    input.checked = true;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

async function selectAutoCandidate(job, button) {
  const batch = button.closest(".auto-batch");
  regionUploadBusy = true;
  updateReviewReady();
  button.disabled = true;
  reviewStatus.textContent = "正在重新验证所选结果…";
  try {
    const resp = await fetch(
      `/api/jobs/${encodeURIComponent(job.job_id)}/auto-resolution/${encodeURIComponent(batch.dataset.batchId)}/select`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_id: button.dataset.candidateId }),
      }
    );
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "候选验证失败");
    showReview(data);
  } catch (err) {
    regionUploadBusy = false;
    button.disabled = false;
    reviewStatus.textContent = `未能采用该结果：${err.message}`;
    updateReviewReady();
  }
}

async function retryAutoBatch(job, button) {
  const batch = button.closest(".auto-batch");
  regionUploadBusy = true;
  updateReviewReady();
  button.disabled = true;
  reviewStatus.textContent = "正在检查剩余识别方案…";
  try {
    const resp = await fetch(
      `/api/jobs/${encodeURIComponent(job.job_id)}/auto-resolution/${encodeURIComponent(batch.dataset.batchId)}/retry`,
      { method: "POST" }
    );
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "无法重新尝试");
    reviewPanel.hidden = true;
    startPolling();
  } catch (err) {
    regionUploadBusy = false;
    button.disabled = false;
    reviewStatus.textContent = `无法重新尝试：${err.message}`;
    updateReviewReady();
  }
}

async function uploadAutoBatch(job, button) {
  const batch = button.closest(".auto-batch");
  const file = batch.querySelector("[data-auto-file]").files[0];
  const status = batch.querySelector("[data-auto-status]");
  if (!file) {
    status.textContent = "请选择包含完整左右手谱表的图片";
    return;
  }
  regionUploadBusy = true;
  updateReviewReady();
  button.disabled = true;
  status.textContent = "正在提交完整谱表图片…";
  const form = new FormData();
  form.append("file", file);
  try {
    const resp = await fetch(
      `/api/jobs/${encodeURIComponent(job.job_id)}/auto-resolution/${encodeURIComponent(batch.dataset.batchId)}/upload`,
      { method: "POST", body: form }
    );
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "谱表图片提交失败");
    reviewPanel.hidden = true;
    startPolling();
  } catch (err) {
    regionUploadBusy = false;
    button.disabled = false;
    status.textContent = `提交失败：${err.message}`;
    updateReviewReady();
  }
}

function updateReviewReady() {
  const findings = Array.from(reviewFindings.querySelectorAll(".finding"));
  const actions = findings.map((finding) => finding.querySelector("[data-review-action]:checked")?.value || null);
  const state = reviewSelectionState(actions, regionUploadBusy);
  const ready = autoReviewReady(currentAutoResolution || { batches: [] }, actions, regionUploadBusy);
  const autoPending = (currentAutoResolution && currentAutoResolution.batches || [])
    .some((batch) => batch.status !== "auto_resolved");
  approveReviewBtn.disabled = !ready;
  approveReviewBtn.textContent = state.total
    ? `提交审批（${state.selected}/${state.total}）`
    : "继续生成结果";
  if (actions.includes("reidentify")) {
    reviewSelectionStatus.textContent = "请先上传所选区域的图片，完成二次识别。";
  } else if (autoPending) {
    reviewSelectionStatus.textContent = "请先处理仍待选择或补充图片的谱表区域。";
  } else if (ready) {
    reviewSelectionStatus.textContent = `已完成 ${state.selected}/${state.total} 项，可提交审批。`;
  } else {
    reviewSelectionStatus.textContent = `已完成 ${state.selected}/${state.total} 项，还需选择 ${state.pending} 项。`;
  }
}

let regionUploadBusy = false;

async function submitReview(job) {
  if (approveReviewBtn.disabled) return;
  const decisions = Array.from(reviewFindings.querySelectorAll(".finding")).map((finding) => {
    const selected = finding.querySelector("[data-review-action]:checked");
    return { id: finding.dataset.findingId, action: selected.value };
  });
  approveReviewBtn.disabled = true;
  reviewStatus.textContent = "正在应用审批决定…";
  try {
    const resp = await fetch(`/api/jobs/${encodeURIComponent(job.job_id)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "审批提交失败");
    reviewPanel.hidden = true;
    startPolling();
  } catch (err) {
    reviewStatus.textContent = `审批提交失败：${err.message}`;
    updateReviewReady();
  }
}

async function uploadRegion(job, finding) {
  const fileInput = finding.querySelector("[data-region-file]");
  const file = fileInput.files[0];
  const start = Number(finding.querySelector("[data-region-start]").value);
  const end = Number(finding.querySelector("[data-region-end]").value);
  const status = finding.querySelector("[data-region-status]");
  if (!file) {
    status.textContent = "请选择区域图片";
    return;
  }
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < start) {
    status.textContent = "请输入有效的小节范围";
    return;
  }
  regionUploadBusy = true;
  updateReviewReady();
  status.textContent = "正在排队二次识别…";
  const form = new FormData();
  form.append("file", file);
  form.append("measure_start", String(start));
  form.append("measure_end", String(end));
  try {
    const resp = await fetch(
      `/api/jobs/${encodeURIComponent(job.job_id)}/review/${encodeURIComponent(finding.dataset.findingId)}/region`,
      { method: "POST", body: form }
    );
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "区域识别提交失败");
    status.textContent = "已提交，等待新的分析结果…";
    reviewPanel.hidden = true;
    startPolling();
  } catch (err) {
    regionUploadBusy = false;
    status.textContent = `区域识别失败：${err.message}`;
    updateReviewReady();
  }
}

function failProgress(message) {
  const box = $("error-box");
  box.hidden = false;
  box.textContent = message;
  resetBtn.disabled = false;
}

function showResults(job) {
  reviewPanel.hidden = true;
  regionUploadBusy = false;
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

const restoredJobId = initialJobId(window.location.search, sessionStorage, JOB_SESSION_KEY);
if (restoredJobId) {
  currentJob = restoredJobId;
  saveJobId(sessionStorage, JOB_SESSION_KEY, currentJob);
  showProgress();
  startPolling();
} else {
  resetToEmpty();
}
loadSystemStatus();
