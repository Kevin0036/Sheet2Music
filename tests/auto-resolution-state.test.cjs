const test = require("node:test");
const assert = require("node:assert/strict");

const {
  autoResolutionSummary,
  autoReviewReady,
  batchActions,
  candidateSummaryText,
} = require("../sheet2music/web/static/review-state.js");

test("summarizes automatic batches in Chinese", () => {
  const fixture = {
    batches: [
      ...Array.from({ length: 6 }, (_, index) => ({ batch_id: `a${index}`, status: "auto_resolved" })),
      { batch_id: "c1", status: "needs_choice" },
      { batch_id: "c2", status: "needs_choice" },
      { batch_id: "u1", status: "needs_upload" },
    ],
  };

  assert.deepEqual(autoResolutionSummary(fixture), {
    total: 9,
    resolved: 6,
    needsChoice: 2,
    needsUpload: 1,
    text: "已检查 9 个谱表区域：自动解决 6 个，需要选择 2 个，仍需补充图片 1 个。",
  });
});

test("blocks submission only for unresolved batches and manual findings", () => {
  const allResolved = { batches: [{ status: "auto_resolved" }] };
  const withChoice = { batches: [{ status: "needs_choice" }] };

  assert.equal(autoReviewReady(allResolved, []), true);
  assert.equal(autoReviewReady(withChoice, []), false);
  assert.equal(autoReviewReady(allResolved, [null]), false);
  assert.equal(autoReviewReady(allResolved, ["preserve"]), true);
});

test("does not expose upload while automatic candidates remain", () => {
  assert.equal(batchActions({ status: "needs_choice" }).includes("upload"), false);
  assert.equal(batchActions({ status: "needs_upload" }).includes("upload"), true);
});

test("candidate summaries translate validation reasons into readable Chinese", () => {
  assert.equal(
    candidateSummaryText({
      variant: "contrast",
      validation: {
        accepted: false,
        reasons: ["measure_overflow", "structure_changed"],
        target_findings_before: 2,
        target_findings_after: 1,
      },
    }),
    "对比度增强方案：目标疑点由 2 处降至 1 处；仍有音符超出小节边界；检测到未经确认的谱号、拍号或调号变化。"
  );
});
