const test = require("node:test");
const assert = require("node:assert/strict");

const {
  saveJobId,
  loadJobId,
  clearJobId,
  initialJobId,
} = require("../sheet2music/web/static/job-session.js");

function memoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
}

test("restores the most recently saved job id", () => {
  const storage = memoryStorage();
  saveJobId(storage, "sheet2music.current-job", "31b828cc8284");
  assert.equal(loadJobId(storage, "sheet2music.current-job"), "31b828cc8284");
});

test("clearing a job id prevents a reset task from reappearing", () => {
  const storage = memoryStorage();
  saveJobId(storage, "sheet2music.current-job", "31b828cc8284");
  clearJobId(storage, "sheet2music.current-job");
  assert.equal(loadJobId(storage, "sheet2music.current-job"), null);
});

test("uses a job id from the URL before the browser session", () => {
  const storage = memoryStorage();
  saveJobId(storage, "sheet2music.current-job", "older-job");
  assert.equal(
    initialJobId("?job_id=31b828cc8284", storage, "sheet2music.current-job"),
    "31b828cc8284"
  );
});
