const { invoke } = window.__TAURI__.core;
const $ = id => document.getElementById(id);
const lines = [];
let finishedDeviceTest = false, scannerState = null, signalView = null;
let running = false, providerReady = false, browserClosed = false, browserEvent = null, recovering = false;
function log(message) { lines.unshift(message); $('status').textContent = lines.slice(0, 30).join('\n'); }
async function run(command, args) {
  try { return {ok: true, value: await invoke(command, args)}; }
  catch (error) { log(String(error)); return {ok: false}; }
}
function render() {
  const snapshot = scannerState?.snapshot || {}, counts = snapshot.counts || {};
  const seconds = scannerState ? Math.max(0, Math.floor((Date.now() - Date.parse(scannerState.received_at)) / 1000)) : Infinity;
  const connected = !!scannerState?.connected && seconds <= 6;
  const healthy = connected && !!scannerState?.healthy;
  $('connection-state').textContent = !scannerState ? 'Not connected' : !connected ? 'Reconnecting / paused' : healthy ? 'Live source current' : 'Waiting for source data';
  $('connection-state').dataset.state = healthy ? 'connected' : 'waiting';
  $('session-state').textContent = browserClosed ? 'Browser closed' : running ? recovering ? 'Recovering browser' : providerReady ? 'Monitoring' : 'Waiting for DraftKings' : 'Stopped';
  $('session-state').dataset.tone = browserClosed ? 'negative' : running && providerReady ? 'positive' : '';
  for (const id of ['conservative-level', 'kelly', 'cap']) $(id).disabled = running;
  for (const [id, key] of [['stat-live', 'dk_live'], ['stat-ready', 'ready'], ['stat-qualified', 'qualified']]) $(id).textContent = Number.isInteger(counts[key]) ? counts[key] : '—';
  $('scanner-summary').textContent = snapshot.detail || 'Sign in to receive scanner updates.';
  if (scannerState) {
    const count = name => Number.isInteger(counts[name]) ? counts[name] : '—';
    $('scanner').textContent = [!connected ? 'Reconnecting / paused' : healthy ? 'Connected — source current' : 'Connected — waiting for verified source data',
      `Last connection heartbeat: ${seconds}s ago`, `Provider stream: ${snapshot.connection || 'unknown'}`,
      `Observed events: ${count('events')} | Markets: ${count('markets')} | Live DK: ${count('dk_live')}`,
      `History rows: ${count('histories')} | Live history: ${count('ttt_live')} | Matched: ${count('mapped')}`,
      `Monitoring candidates: ${count('ready')} | Qualified signals: ${count('qualified')}`,
      `Last score update: ${snapshot.last_score_at || 'not received yet'}`, `Last market update: ${snapshot.last_market_at || 'not received yet'}`,
      `Last history update: ${snapshot.last_history_at || 'not received yet'}`, snapshot.detail || 'Waiting for scanner telemetry.'].join('\n');
  }
  const offset = scannerState?.server_offset || 0;
  if (signalView && Date.parse(signalView.expires_at) <= Date.now() + offset * 1000) signalView = null;
  window.EmberOpportunity.render(signalView || snapshot.opportunity, {connected, healthy, heartbeatAge: seconds,
    serverOffset: offset, running, providerReady, browserClosed, browserEvent});
}
const savedApi = localStorage.getItem('ember-api');
if (savedApi) $('api').value = savedApi;
$('login').onclick = async () => {
  $('login').disabled = true;
  $('sync-missed').disabled = true;
  window.EmberHistory.update({records: [], counts: {}, queued: 0, sync_state: 'idle'});
  localStorage.setItem('ember-api', $('api').value.trim());
  const result = await run('login', {api: $('api').value.trim()});
  if (!result.ok) $('login').disabled = false;
};
for (const id of ['conservative-level', 'kelly', 'cap']) {
  const saved = localStorage.getItem(`ember-${id}`);
  if (saved !== null && (id === 'cap' ? Number(saved) > 0 && Number(saved) <= 100 : [...$(id).options].some(option => option.value === saved))) $(id).value = saved;
  $(id).onchange = () => localStorage.setItem(`ember-${id}`, $(id).value);
}
$('start').onclick = () => {
  if (!$('cap').reportValidity()) return;
  return run('start', {maxBankrollRiskPercent: Number($('cap').value), conservativeLevel: $('conservative-level').value, kellyMultiplier: Number($('kelly').value)});
};
$('stop').onclick = () => run('stop');
$('check').onclick = async () => {
  const result = await run('check_update');
  if (result.ok && result.value) {
    $('install').disabled = !result.value.available;
    $('update').textContent = result.value.available ? `Version ${result.value.version} is available. ${result.value.notes || ''}` : 'This installation is up to date.';
  }
};
$('install').onclick = () => run('install_update');
$('history-filter').onchange = () => { window.EmberHistory.render(); run('missed_history', {filter: $('history-filter').value, offset: 0}); };
$('history-newer').onclick = () => run('missed_history', {filter: $('history-filter').value, offset: window.EmberHistory.page('newer')});
$('history-older').onclick = () => run('missed_history', {filter: $('history-filter').value, offset: window.EmberHistory.page('older')});
$('sync-missed').onclick = () => run('sync_missed_history');
$('dismiss-alert').onclick = () => { $('browser-alert').hidden = true; };
$('test-sound').onclick = async () => {
  const result = await run('test_alert_sound');
  $('test-sound').textContent = result.ok && result.value ? 'Sound played · test again' : 'Sound unavailable · check your system audio';
};
window.__TAURI__.event.listen('worker', ({payload}) => {
  if (payload.kind === 'scanner') scannerState = payload;
  if (payload.kind === 'missed_history') window.EmberHistory.update(payload);
  if (payload.kind === 'opportunity') signalView = payload.snapshot;
  if (payload.kind === 'session') {
    running = payload.running; providerReady = false; browserClosed = false; recovering = false;
    $('browser-alert').hidden = true;
  }
  if (payload.kind === 'provider') { providerReady = payload.ready; if (providerReady) recovering = false; }
  if (payload.kind === 'browser_recovery') {
    recovering = payload.state === 'recovering'; providerReady = false; signalView = null; browserEvent = null;
    $('browser').textContent = payload.message;
  }
  if (payload.kind === 'browser') {
    browserEvent = payload.event_id || null;
    $('browser').textContent = payload.text + (payload.checked_at ? ` Browser status time: ${new Date(payload.checked_at).toLocaleTimeString()}.` : '');
  }
  if (payload.kind === 'alert') {
    browserClosed = true; providerReady = false; running = false; signalView = null; recovering = false;
    $('browser-alert').hidden = false; $('alert-message').textContent = payload.message;
    $('alert-sound').textContent = 'Playing the browser-close notification sound.';
    $('browser').textContent = payload.message;
  }
  if (payload.kind === 'alert_sound') $('alert-sound').textContent = payload.played ? 'Audible notification played.' : 'The sound could not play. Check your system audio output.';
  if (payload.kind === 'signed_in' || payload.kind === 'error') $('login').disabled = false;
  if (payload.kind === 'signed_in') { $('check').disabled = false; $('sync-missed').disabled = false; }
  if (payload.kind === 'signed_in' || payload.kind === 'entitlement') $('start').disabled = finishedDeviceTest || !payload.enabled || !payload.supported;
  if (payload.kind === 'signed_in') $('check').click();
  if (payload.message) log(payload.message);
  if (payload.kind === 'ready') { $('version').textContent = `SPORTS SIGNALS · ${payload.version}`; log(`Ember ${payload.version} ready. Sign in to connect.`); run('missed_history'); }
  if (payload.kind === 'stopped') {
    if (payload.recoverable === true) {
      providerReady = false; recovering = true; signalView = null; browserEvent = null;
      $('browser').textContent = 'Browser control connection interrupted. Reconnecting the owned browser; wagering is paused.';
      log('Browser control connection interrupted; the owned Chrome is preserved for recovery.');
      render();
      return;
    }
    running = false; providerReady = false; recovering = false; browserClosed = browserClosed || payload.browser_closed === true;
    if (!browserClosed) $('browser').textContent = 'Execution stopped. Press Start to open DraftKings and the TT Elite match list.';
    if (payload.device_test_complete) { finishedDeviceTest = true; $('start').disabled = true; log(`Device test finished: ${payload.outcome}. Restart the test application before another attempt.`); }
    else log('Stopped. Press Start to begin another session.');
  }
  render();
});
setInterval(render, 1000);
render();
run('worker_status').then(result => { if (result.ok && result.value) { log('Ember ready. Sign in to connect.'); run('missed_history'); } });
