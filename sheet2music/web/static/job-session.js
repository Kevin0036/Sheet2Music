(function attachJobSession(global) {
  function saveJobId(storage, key, jobId) {
    storage.setItem(key, jobId);
  }

  function loadJobId(storage, key) {
    return storage.getItem(key);
  }

  function clearJobId(storage, key) {
    storage.removeItem(key);
  }

  function initialJobId(search, storage, key) {
    return new URLSearchParams(search).get("job_id") || loadJobId(storage, key);
  }

  const api = { saveJobId, loadJobId, clearJobId, initialJobId };
  global.Sheet2MusicJobSession = api;
  if (typeof module !== "undefined") module.exports = api;
})(globalThis);
