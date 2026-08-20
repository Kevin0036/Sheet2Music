(function attachReviewState(global) {
  function reviewSelectionState(actions, regionUploadBusy) {
    const total = actions.length;
    const selected = actions.filter(Boolean).length;
    const hasPendingRegionUpload = actions.includes("reidentify");
    return {
      selected,
      total,
      pending: total - selected,
      ready: total > 0 && selected === total && !regionUploadBusy && !hasPendingRegionUpload,
    };
  }

  function hasAutomaticRepairSuggestion(suggestion) {
    if (!suggestion || typeof suggestion !== "object" || suggestion.action === "review") return false;
    return Object.prototype.hasOwnProperty.call(suggestion, "signature")
      || Object.prototype.hasOwnProperty.call(suggestion, "sign")
      || suggestion.action === "compress"
      || suggestion.action === "repair_gap";
  }

  function actionsForFinding(finding) {
    return Array.isArray(finding && finding.available_actions)
      ? finding.available_actions.filter((action) => ["preserve", "correct", "reidentify", "ignore"].includes(action))
      : ["preserve", "correct", "reidentify", "ignore"];
  }

  function suggestedReviewActions(findings) {
    return findings.map((finding) => {
      const actions = actionsForFinding(finding);
      if (actions.includes("correct") && hasAutomaticRepairSuggestion(finding && finding.suggestion)) {
        return "correct";
      }
      return actions.includes("preserve") ? "preserve" : null;
    });
  }

  function automaticRepairCount(findings) {
    return findings.filter((finding) => (
      actionsForFinding(finding).includes("correct")
      && hasAutomaticRepairSuggestion(finding && finding.suggestion)
    )).length;
  }

  function preservableFindingCount(findings) {
    return findings.filter((finding) => actionsForFinding(finding).includes("preserve")).length;
  }

  function rationalNumber(value) {
    const text = String(value ?? "").trim();
    const match = text.match(/^(-?\d+)(?:\/(\d+))?$/);
    if (!match) return null;
    const denominator = Number(match[2] || 1);
    return denominator ? Number(match[1]) / denominator : null;
  }

  function readableNumber(value) {
    const number = rationalNumber(value);
    if (number == null) return "未明确";
    return Number.isInteger(number) ? String(number) : String(Number(number.toFixed(3)));
  }

  function formatBeatLocation(finding) {
    const measure = finding && finding.measure_start;
    const offset = rationalNumber(finding && finding.offset_beats);
    if (measure == null) return "小节位置未提供";
    if (!offset) return `第 ${measure} 小节开头`;
    return `第 ${measure} 小节，第 ${readableNumber(finding.offset_beats)} 拍后`;
  }

  function formatTimingSummary(observed) {
    if (!observed || typeof observed !== "object") return "时值信息未提供";
    const occupied = readableNumber(observed.occupied_beats);
    const expected = readableNumber(observed.expected_beats);
    const difference = readableNumber(observed.difference_beats);
    return `当前占用 ${occupied} 拍，小节容量 ${expected} 拍，超出 ${difference} 拍`;
  }

  function reviewChangeSummaryText(summary) {
    const resolved = Array.isArray(summary && summary.resolved_findings)
      ? summary.resolved_findings.length
      : 0;
    const added = Array.isArray(summary && summary.new_findings) ? summary.new_findings.length : 0;
    const unchanged = Number(summary && summary.unchanged_high_risk_count) || 0;
    const current = Number(summary && summary.after_high_risk_count) || 0;
    return `本次二次识别后：已解决 ${resolved} 个疑点，新增 ${added} 个疑点，未变化 ${unchanged} 个，当前需要确认 ${current} 个。`;
  }

  function autoResolutionSummary(autoResolution) {
    const batches = Array.isArray(autoResolution && autoResolution.batches)
      ? autoResolution.batches
      : [];
    const resolved = batches.filter((batch) => batch.status === "auto_resolved").length;
    const needsChoice = batches.filter((batch) => batch.status === "needs_choice").length;
    const needsUpload = batches.filter((batch) => (
      batch.status === "needs_upload" || batch.status === "failed"
    )).length;
    const acceptedOriginal = batches.filter((batch) => batch.status === "accepted_original").length;
    const total = batches.length;
    const acceptedText = acceptedOriginal
      ? `已允许保留原始识别 ${acceptedOriginal} 个`
      : "";
    return {
      total,
      resolved,
      needsChoice,
      needsUpload,
      acceptedOriginal,
      text: `已检查 ${total} 个谱表区域：自动解决 ${resolved} 个，需要选择 ${needsChoice} 个，仍需补充图片 ${needsUpload} 个${acceptedText ? `，${acceptedText}` : ""}。`,
    };
  }

  function autoReviewReady(autoResolution, manualActions, regionUploadBusy = false, ignoredMeasures = []) {
    const batches = Array.isArray(autoResolution && autoResolution.batches)
      ? autoResolution.batches
      : [];
    const ignored = new Set(ignoredMeasures);
    const automaticReady = batches.every((batch) => (
      batch.status === "auto_resolved"
      || batch.status === "accepted_original"
      || ((batch.status === "needs_upload" || batch.status === "failed")
        && (batch.target_measures || []).every((measure) => ignored.has(measure)))
    ));
    const manualReady = manualActions.every((action) => Boolean(action) && action !== "reidentify");
    return automaticReady && manualReady && !regionUploadBusy;
  }

  function batchActions(batch) {
    if (!batch || typeof batch !== "object") return [];
    if (batch.status === "needs_choice") return ["select", "retry"];
    if (batch.status === "needs_upload" || batch.status === "failed") return ["upload", "ignore"];
    return [];
  }

  const candidateVariants = {
    standard: "标准图像方案",
    contrast: "对比度增强方案",
    context: "扩展上下文方案",
  };

  const candidateReasons = {
    measure_overflow: "仍有音符超出小节边界",
    structure_changed: "检测到未经确认的谱号、拍号或调号变化",
    timing_cursor_invalid: "时间游标结构仍然无效",
    measure_count_mismatch: "识别出的小节数量与该谱表区域不一致",
    measure_alignment_invalid: "识别小节无法与原谱唯一对齐",
    mapping_not_high_confidence: "图像与小节的定位证据不足",
    context_anchor_mismatch: "相邻正常小节与原谱不一致",
    visual_notehead_mismatch: "音符数量与图像检测结果差异过大",
  };

  function batchFailureReasons(batch) {
    const reasons = Array.isArray(batch && batch.failure_reasons)
      ? batch.failure_reasons
      : [];
    return reasons.map((reason) => candidateReasons[reason] || "候选未通过安全检查");
  }

  function candidateSummaryText(candidate) {
    const variant = candidate && candidate.variant;
    const validation = candidate && candidate.validation && typeof candidate.validation === "object"
      ? candidate.validation
      : {};
    const title = candidateVariants[variant] || "二次识别方案";
    const before = Number(validation.target_findings_before) || 0;
    const after = Number(validation.target_findings_after) || 0;
    const parts = [`${title}：目标疑点由 ${before} 处降至 ${after} 处`];
    const reasons = Array.isArray(validation.reasons) ? validation.reasons : [];
    reasons.forEach((reason) => {
      parts.push(candidateReasons[reason] || "候选未通过安全检查");
    });
    if (validation.accepted === true && reasons.length === 0) {
      parts.push("已通过小节边界、结构和图像证据检查");
    }
    return `${parts.join("；")}。`;
  }

  const api = {
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
    batchFailureReasons,
  };
  global.Sheet2MusicReviewState = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
