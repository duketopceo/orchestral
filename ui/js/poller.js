let pollTimer = null;
export function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

export function poll(fn, ms) {
  stopPolling();
  pollTimer = setInterval(fn, ms);
}
