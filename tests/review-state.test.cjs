const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

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
} = require("../sheet2music/web/static/review-state.js");

test("requires a decision for every visible review finding", () => {
  assert.deepEqual(
    reviewSelectionState(["preserve", null, "correct"], false),
    { selected: 2, total: 3, pending: 1, ready: false }
  );
});

test("enables submission when every finding has a non-region decision", () => {
  assert.deepEqual(
    reviewSelectionState(["preserve", "correct", "preserve"], false),
    { selected: 3, total: 3, pending: 0, ready: true }
  );
});

test("keeps submission disabled while a region reidentification is pending", () => {
  assert.deepEqual(
    reviewSelectionState(["preserve", "reidentify"], false),
    { selected: 2, total: 2, pending: 0, ready: false }
  );
});

test("identifies whether a finding has an automatic repair to apply", () => {
  assert.equal(hasAutomaticRepairSuggestion({ signature: "2/4" }), true);
  assert.equal(hasAutomaticRepairSuggestion({ sign: "G", line: 2 }), true);
  assert.equal(hasAutomaticRepairSuggestion({ action: "review" }), false);
  assert.equal(hasAutomaticRepairSuggestion({ action: "compress", corrected_note_count: 2 }), true);
  assert.equal(hasAutomaticRepairSuggestion({ action: "repair_gap", resulting_beats: "4" }), true);
});

test("uses only review actions explicitly allowed by the finding", () => {
  assert.deepEqual(actionsForFinding({ available_actions: ["reidentify"] }), ["reidentify"]);
  assert.deepEqual(actionsForFinding({}), ["preserve", "correct", "reidentify", "ignore"]);
});

test("formats beat positions and timing values as readable Chinese", () => {
  assert.equal(
    formatBeatLocation({ measure_start: 15, offset_beats: "3/2" }),
    "第 15 小节，第 1.5 拍后"
  );
  assert.equal(
    formatTimingSummary({ occupied_beats: "19/4", expected_beats: "4", difference_beats: "3/4" }),
    "当前占用 4.75 拍，小节容量 4 拍，超出 0.75 拍"
  );
});

test("bulk suggestions leave reidentification-only findings unselected", () => {
  assert.deepEqual(
    suggestedReviewActions([
      { available_actions: ["correct", "reidentify"], suggestion: { action: "compress" } },
      { available_actions: ["reidentify"], suggestion: { action: "reidentify" } },
    ]),
    ["correct", null]
  );
});

test("counts only findings whose current result may be preserved", () => {
  assert.equal(
    preservableFindingCount([
      { available_actions: ["preserve", "reidentify"] },
      { available_actions: ["reidentify"] },
      { available_actions: ["preserve", "correct", "reidentify"] },
    ]),
    2
  );
});

test("applies available repairs and preserves findings without an automatic repair", () => {
  assert.deepEqual(
    suggestedReviewActions([
      { suggestion: { signature: "2/4" } },
      { suggestion: { action: "review" } },
      { suggestion: { sign: "G", line: 2 } },
    ]),
    ["correct", "preserve", "correct"]
  );
});

test("counts only findings that have a concrete automatic repair", () => {
  assert.equal(
    automaticRepairCount([
      { suggestion: { signature: "2/4" } },
      { suggestion: { action: "review" } },
      { suggestion: { sign: "G", line: 2 } },
    ]),
    2
  );
});

test("summarizes reidentification review changes in plain Chinese", () => {
  assert.equal(
    reviewChangeSummaryText({
      before_high_risk_count: 34,
      after_high_risk_count: 33,
      unchanged_high_risk_count: 33,
      new_findings: [],
      resolved_findings: [{ id: "timing_structure:P1:-:76:76" }],
    }),
    "本次二次识别后：已解决 1 个疑点，新增 0 个疑点，未变化 33 个，当前需要确认 33 个。"
  );
});

test("loads the review state script with a cache version", () => {
  const html = fs.readFileSync(path.join(__dirname, "../sheet2music/web/static/index.html"), "utf8");
  assert.match(html, /review-state\.js\?v=\d+/);
});

test("resets region upload state when rendering a fresh review", () => {
  const app = fs.readFileSync(path.join(__dirname, "../sheet2music/web/static/app.js"), "utf8");
  assert.match(app, /function showReview\(job\) \{\s+regionUploadBusy = false;/);
});
