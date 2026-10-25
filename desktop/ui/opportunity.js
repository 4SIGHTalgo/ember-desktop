window.EmberOpportunity = (() => {
  const $ = id => document.getElementById(id);
  const known = value => typeof value === 'number' && Number.isFinite(value);
  const text = (id, value) => { const node = $(id); if (node.textContent !== String(value)) node.textContent = value; };
  function percent(value, threshold = null) {
    if (!known(value)) return '—';
    const near = known(threshold) && value !== threshold && Math.abs(value - threshold) < 0.001;
    return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(near ? 6 : 2)}%`;
  }
  const probability = value => known(value) ? `${(100 * value).toFixed(1)}%` : '—';
  const selection = candidate => candidate.player + (known(candidate.line) ? ` ${['Over', 'Under'].includes(candidate.player) ? '' : candidate.line >= 0 ? '+' : ''}${candidate.line}` : '');
  const age = (value, now) => value && Number.isFinite(Date.parse(value)) ? Math.max(0, Math.floor((now - Date.parse(value)) / 1000)) : null;
  function odds(candidate) {
    if (!known(candidate.decimal_odds)) return 'Odds unavailable';
    const american = candidate.american_odds || (candidate.decimal_odds >= 2 ? `+${Math.round(100 * (candidate.decimal_odds - 1))}` : `−${Math.round(100 / (candidate.decimal_odds - 1))}`);
    return `${american} · ${candidate.decimal_odds.toFixed(2)}`;
  }
  function candidateCard(candidate, confidence) {
    const card = document.createElement('article'); card.className = 'candidate';
    const head = document.createElement('div'); head.className = 'candidate-head';
    const name = document.createElement('h3'); name.textContent = selection(candidate);
    const price = document.createElement('span'); price.className = 'candidate-odds'; price.textContent = odds(candidate);
    head.append(name, price);
    const list = document.createElement('dl');
    for (const [label, value] of [
      ['Point-estimate EV', percent(candidate.estimated_ev)], ['Conservative EV', percent(candidate.conservative_ev)],
      ['Historical win probability', probability(candidate.probability)], [confidence === 95 ? 'Very conservative probability' : 'Conservative probability', probability(candidate.lower_probability)],
      ['Sportsbook implied', probability(candidate.implied_probability)]]) {
      const term = document.createElement('dt'); term.textContent = label;
      const detail = document.createElement('dd'); detail.textContent = value;
      list.append(term, detail);
    }
    const sample = document.createElement('p'); sample.className = 'small';
    sample.textContent = `Conditional history: ${candidate.success_count} successes / ${candidate.sample_size} matching cases.`;
    card.append(head, list, sample);
    return card;
  }
  function render(view, context = {}) {
    const panel = document.querySelector('.opportunity');
    $('opportunity-content').hidden = !view;
    $('opportunity-empty').hidden = !!view;
    if (!view) {
      text('opportunity-heading', 'Waiting for a verified match');
      text('opportunity-subtitle', context.connected ? 'The scanner is running. Match details and EV appear here when a candidate can be evaluated.' : 'Sign in to see the match, live score, odds, and expected value being evaluated.');
      text('opportunity-stage', context.browserClosed ? 'Browser closed' : context.connected ? 'Scanning' : 'Standby');
      $('opportunity-stage').dataset.tone = context.browserClosed ? 'negative' : '';
      return;
    }
    const now = Date.now() + (context.serverOffset || 0) * 1000;
    const fresh = !!context.connected && !!context.healthy && context.heartbeatAge <= 6 && Date.parse(view.expires_at) > now;
    const quoteAge = age(view.market_at, now), scoreAge = age(view.score_at, now);
    const pricesCurrent = quoteAge !== null && quoteAge <= 5 && scoreAge !== null && scoreAge <= 5;
    const candidates = view.candidates || [];
    const best = candidates.reduce((best, row) => !best || (known(row.gate_ev) && (!known(best.gate_ev) || row.gate_ev > best.gate_ev)) ? row : best, null);
    const gatePassed = !!best?.ev_passed;
    const active = fresh && pricesCurrent && !context.browserClosed;
    panel.dataset.stale = !fresh || (known(best?.gate_ev) && !pricesCurrent) || !!context.browserClosed;
    text('opportunity-heading', `${view.players[0]} vs ${view.players[1]}`);
    text('opportunity-subtitle', `${view.bet_type === 'split' ? `${view.market || 'Next game'} conditional split` : view.bet_type === 'complement' ? `${view.market || 'Next game'} split complement` : view.bet_type === 'closeout' ? 'Match close-out' : view.market || 'Match evaluation'} · ${view.history_count ?? 'Unknown'} prior head-to-head meetings${context.browserEvent === view.event_id && view.event_id ? ' · Browser target' : ''}`);
    text('player-one', view.players[0]); text('player-two', view.players[1]);
    text('sets-one', view.set_tally?.[0] ?? '—'); text('sets-two', view.set_tally?.[1] ?? '—');
    text('point-score', `${view.period ? `${view.period} set · ` : ''}Current points ${view.points ? view.points.join(' – ') : 'unavailable'}`);
    const mode = view.ev_mode === 'point' ? 'POINT-ESTIMATE NET EV' : view.confidence === 95 ? 'VERY CONSERVATIVE NET EV' : 'CONSERVATIVE NET EV';
    const shownEV = active && view.entry_open ? best?.gate_ev : null;
    text('ev-label', mode); text('ev-value', percent(shownEV, view.minimum_ev));
    const tone = known(shownEV) ? shownEV > 0 ? 'positive' : 'negative' : '';
    $('ev-value').dataset.tone = tone;
    document.querySelector('.ev-card').dataset.tone = tone;
    text('ev-comparison', `Gate: strictly above ${percent(view.minimum_ev)}`);
    const position = value => Math.max(0, Math.min(300, (value + 50) / 150 * 300));
    $('ev-meter-fill').setAttribute('width', known(shownEV) ? position(shownEV) : 0);
    for (const attribute of ['x1', 'x2']) $('ev-meter-threshold').setAttribute(attribute, position(view.minimum_ev));
    text('ev-profit', known(shownEV) ? `Expected net per $1: ${shownEV >= 0 ? '+' : '−'}$${Math.abs(shownEV / 100).toFixed(3)} at the feed quote. Final slip is rechecked.` : 'EV is unavailable until a valid start-state sample and matching current quote are present.');
    text('selection-name', best ? selection(best) : 'Waiting for a strategy selection');
    text('market-name', `${view.market || (view.bet_type === 'closeout' ? 'Moneyline' : 'Next game winner')}${best ? ` · ${odds(best)}` : ''}`);
    let stage = 'Evaluating', stageTone = '', reason = view.reason;
    if (context.browserClosed) { stage = 'Browser closed'; stageTone = 'negative'; reason = 'DraftKings browser closed. Execution is stopped; scanner estimates are read-only.'; }
    else if (!fresh) { stage = 'Data paused'; stageTone = 'warning'; reason = 'Waiting for a fresh scanner snapshot. Values shown are the last observation.'; }
    else if (!view.mapping_verified) { stage = 'Checking matchup'; stageTone = 'warning'; }
    else if (!view.entry_open) { stage = candidates.length ? 'Entry closed' : 'Watching match'; }
    else if (!known(best?.gate_ev)) { stage = 'Waiting for data'; }
    else if (!pricesCurrent) { stage = 'Quote stale'; stageTone = 'warning'; reason = 'Waiting for current score and odds updates. Live EV is hidden.'; }
    else if (view.qualifies) { stage = context.running && context.providerReady ? 'Signal qualified' : 'Qualified · execution stopped'; stageTone = 'positive'; reason = context.running && context.providerReady ? 'Model signal qualified. Local authentication, price, sizing, and entry conditions are checked before submission.' : 'The model signal qualifies. Execution is stopped or awaiting DraftKings readiness.'; }
    else if (!gatePassed) { stage = 'Below EV gate'; reason = view.reason; }
    else { stage = 'Waiting for entry checks'; stageTone = 'warning'; }
    text('opportunity-stage', stage); $('opportunity-stage').dataset.tone = stageTone;
    text('decision-reason', reason);
    const checks = {mapping: view.mapping_verified, history: view.history_count >= 30, window: view.entry_open, ev: gatePassed && active};
    for (const [key, passed] of Object.entries(checks)) $(`check-${key}`).dataset.passed = passed && fresh;
    text('check-history', `${view.history_count ?? 'Unknown'} prior meetings · min 30`);
    text('check-ev', `${view.ev_mode === 'point' ? 'Point' : 'Conservative'} EV > ${view.minimum_ev}%`);
    text('opportunity-age', `Quote: ${quoteAge === null ? 'not received' : `${quoteAge}s ago`} · Score: ${scoreAge === null ? 'not received' : `${scoreAge}s ago`} · Snapshot: ${age(view.updated_at, now) ?? '—'}s ago`);
    const shownCandidates = active && view.entry_open ? candidates : candidates.map(row => ({...row, estimated_ev: null, conservative_ev: null}));
    const signature = JSON.stringify([shownCandidates, view.confidence]);
    if ($('candidate-cards').dataset.signature !== signature) {
      $('candidate-cards').replaceChildren(...shownCandidates.map(candidate => candidateCard(candidate, view.confidence)));
      $('candidate-cards').dataset.signature = signature;
    }
  }
  return {render, percent};
})();
