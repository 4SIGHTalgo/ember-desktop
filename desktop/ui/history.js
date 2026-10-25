window.EmberHistory = (() => {
  let state = {records: [], counts: {}, queued: 0, sync_state: 'idle'};
  const $ = id => document.getElementById(id);
  const money = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '—';
  const edge = value => typeof value === 'number' && Number.isFinite(value) ? `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}%` : '—';
  function node(tag, className, text) {
    const element = document.createElement(tag); element.className = className; element.textContent = text; return element;
  }
  function render() {
    $('missed-count').textContent = state.counts.missed || 0;
    const statuses = {idle: 'History is saved locally', synced: 'Server sync is up to date', retrying: 'Server sync interrupted; retrying automatically', local_error: 'Local history storage needs attention'};
    if (state.queued && state.sync_state === 'synced') statuses.synced = 'Server received the last batch';
    $('history-sync').textContent = `${statuses[state.sync_state] || 'History is saved locally'}${state.queued ? ` · ${state.queued} record${state.queued === 1 ? '' : 's'} queued for server sync` : ''}.`;
    const filter = $('history-filter').value;
    const rows = state.records.filter(row => filter === 'all' || row.outcome === filter);
    $('history-newer').disabled = !(state.offset > 0);
    $('history-older').disabled = state.next_offset === null || state.next_offset === undefined;
    $('history-page').textContent = state.total ? `${(state.offset || 0) + 1}–${(state.offset || 0) + rows.length} of ${state.total}` : '';
    $('missed-list').replaceChildren();
    if (!rows.length) $('missed-list').append(node('p', 'history-empty', filter === 'uncertain' ? 'No unconfirmed submissions recorded.' : filter === 'all' ? 'No qualifying opportunities have been observed yet.' : 'No missed opportunities recorded yet.'));
    for (const row of rows) {
      const snapshot = row.snapshot || {}, card = node('article', 'missed-record', '');
      const header = node('div', 'missed-record-header', '');
      header.append(node('strong', '', (snapshot.players || []).join(' vs ')), node('span', 'pill', row.outcome === 'uncertain' ? 'Unconfirmed · check My Bets' : row.outcome.replaceAll('_', ' ')));
      const details = node('p', 'missed-offer', `${snapshot.market || 'Market'} · ${snapshot.selection || 'Selection'} · Point EV ${edge(snapshot.point_ev)} · Odds ${money(snapshot.decimal_odds)}`);
      const reason = node('p', 'missed-reason', row.reason_label || row.reason.replaceAll('_', ' '));
      const when = new Date(row.observed_at).toLocaleString();
      const sync = ['pending', 'processing', 'submitting'].includes(row.outcome) ? 'Awaiting outcome' : row.synced ? 'Synced to server' : 'Queued for server sync';
      const footer = node('p', 'small', `${when} · ${sync}`);
      card.append(header, details, reason);
      const readiness = row.readiness || snapshot.readiness;
      if (readiness && Object.keys(readiness).length) card.append(node('p', 'small', `Market readiness: ${readiness.reason} · ${readiness.attempts} checks · ${Math.round(readiness.wait_ms)} ms`));
      if (row.final_point_ev !== null && row.final_point_ev !== undefined) card.append(node('p', 'small', `Final observed point EV: ${edge(row.final_point_ev)}${row.final_decimal_odds ? ` · final odds ${money(row.final_decimal_odds)}` : ''}`));
      card.append(footer); $('missed-list').append(card);
    }
  }
  return {update: value => { state = value; render(); }, render,
    page: direction => direction === 'older' ? state.next_offset : Math.max(0, (state.offset || 0) - 50)};
})();
