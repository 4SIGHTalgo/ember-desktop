window.EmberBoard = (() => {
  const $ = id => document.getElementById(id);
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const identity = value => (typeof value === 'string' && value.length > 0) || (Number.isSafeInteger(value) && value >= 0);
  const pair = value => Array.isArray(value) && value.length === 2 && value.every(n => Number.isInteger(n) && n >= 0 && n <= 999);
  const players = value => Array.isArray(value) && value.length === 2 && value.every(n => typeof n === 'string' && n.length > 0) && value[0] !== value[1];
  const list = value => Array.isArray(value) ? value : [];
  const unique = (rows, key) => {
    const counts = new Map();
    for (const row of list(rows)) if (row && identity(row[key])) counts.set(String(row[key]), (counts.get(String(row[key])) || 0) + 1);
    return list(rows).filter(row => row && identity(row[key]) && counts.get(String(row[key])) === 1);
  };
  const stamp = value => typeof value === 'string' ? Date.parse(value) : NaN;
  const recent = (value, now, seconds) => now - stamp(value) >= -1000 && now - stamp(value) <= seconds * 1000;
  const price = value => finite(value) && value > 1 ? value.toFixed(2) : '—';
  const edge = value => finite(value) ? `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}%` : '—';
  const finalStatus = event => ['COMPLETED', 'FINISHED', 'FINAL', 'ENDED'].includes(String(event.status).toUpperCase());
  const liveStatus = event => ['STARTED', 'LIVE', 'IN_PROGRESS'].includes(String(event.status).toUpperCase());
  const gameDone = score => pair(score) && Math.max(...score) >= 11 && (Math.min(...score) <= 9 ? Math.max(...score) === 11 : Math.abs(score[0] - score[1]) === 2);
  function completed(event) {
    const scores = list(event.sets).filter(s => s && gameDone([s.home, s.away])).sort((a, b) => a.set - b.set);
    if (scores.some((s, i) => s.set !== i + 1)) return null;
    const tally = [scores.filter(s => s.home > s.away).length, scores.filter(s => s.away > s.home).length];
    return pair(event.set_tally) && tally.every((n, i) => n === event.set_tally[i]) ? scores : null;
  }
  function finished(event) {
    return finalStatus(event) && pair(event.set_tally) && Math.max(...event.set_tally) === 3 && Math.min(...event.set_tally) < 3 && completed(event) !== null;
  }
  function node(tag, className, text = '') {
    const element = document.createElement(tag); element.className = className; element.textContent = text; return element;
  }
  let events = new Map(), legs = new Map(), records = new Map(), upcoming = new Map();
  let board = null, schedule = null, generation = 0, slipGeneration = 0, filter = 'all', collapsed = false, lastSignature = '';
  function reset() {
    events.clear(); legs.clear(); records.clear(); upcoming.clear(); board = schedule = null; generation++; lastSignature = '';
    render(lastContext);
  }
  function scan(snapshot = {}) {
    generation++;
    const value = snapshot?.board;
    board = value?.protocol === 1 && Array.isArray(value.events) ? value : null;
    if (board) {
      if (!collapsed) { $('opportunity-diagnostics').open = false; collapsed = true; }
      // Duplicate identities cannot provide unambiguous live evidence.
      for (const event of unique(board.events, 'event_id')) {
        if (!players(event.players)) continue;
        const id = String(event.event_id);
        const old = events.get(id), markets = new Map(list(old?.markets).map(m => [String(m.market_id), m]));
        for (const market of unique(event.markets, 'market_id')) {
          const outcomes = new Map(list(markets.get(String(market.market_id))?.outcomes).map(o => [String(o.selection_id), o]));
          for (const outcome of unique(market.outcomes, 'selection_id')) {
            outcomes.set(String(outcome.selection_id), {...outcome, generation});
          }
          markets.set(String(market.market_id), {...market, outcomes: [...outcomes.values()]});
        }
        events.set(id, {...event, markets: [...markets.values()], generation});
      }
    }
    schedule = snapshot?.upcoming?.protocol === 1 ? snapshot.upcoming : null;
    for (const event of list(schedule?.events)) {
      if (event && players(event.players) && (identity(event.event_id) || identity(event.key))) {
        const key = identity(event.event_id) ? String(event.event_id) : `upcoming:${event.key}`;
        if (identity(event.event_id) && identity(event.key)) upcoming.delete(`upcoming:${event.key}`);
        upcoming.set(key, {...event, generation});
      }
    }
  }
  function slip(payload) {
    slipGeneration++;
    for (const leg of list(payload.legs)) {
      if (!leg || typeof leg.label !== 'string') continue;
      const key = JSON.stringify([leg.event_id, leg.market_id, leg.selection_id, leg.line, identity(leg.selection_id) ? null : leg.label]);
      legs.set(key, {...leg, checked_at: payload.checked_at, slipGeneration});
    }
  }
  function positions(payload) {
    for (const record of list(payload.records)) if (record && identity(record.id)) records.set(String(record.id), record);
  }
  function submitted(leg, event, positions) {
    return event && positions.some(record => {
      const snapshot = record.snapshot || {};
      return record.outcome === 'placed' && typeof record.contract === 'string' && record.contract.split(':')[0] === String(leg.event_id) &&
        String(snapshot.event_id) === String(leg.event_id) && players(snapshot.players) && snapshot.players.every((p, i) => p === event.players[i]) &&
        snapshot.market === leg.market && snapshot.selection === leg.label && (snapshot.specification?.line ?? null) === (leg.line ?? null);
    });
  }
  function track(record, event, fresh) {
    const unknown = detail => ['Unknown', detail];
    const snapshot = record.snapshot || {}, spec = snapshot.specification || record.specification || {};
    if (record.outcome !== 'placed') return unknown('Submission not verified · check My Bets');
    if (!event || !fresh) return unknown('No current verified score');
    if (!identity(snapshot.event_id) || String(snapshot.event_id) !== String(event.event_id) ||
        typeof record.contract !== 'string' || record.contract.split(':')[0] !== String(event.event_id) ||
        !players(snapshot.players) || snapshot.players.some((p, i) => p !== event.players[i])) return unknown('Identity not verified');
    if (spec.family && snapshot.bet_type && spec.family !== snapshot.bet_type) return unknown('Conflicting target');
    const family = spec.family || snapshot.bet_type;
    if (spec.player !== undefined && spec.player !== null && ![0, 1].includes(spec.player)) return unknown('Selection not verified');
    const selected = [0, 1].includes(spec.player) ? spec.player : event.players.indexOf(snapshot.selection);
    if ([0, 1].includes(spec.player) && event.players.includes(snapshot.selection) && event.players[selected] !== snapshot.selection) return unknown('Conflicting selection');
    const scores = completed(event), isFinal = finished(event);
    const compare = (difference, decided, detail) => [decided ? difference > 0 ? 'Observed win' : difference < 0 ? 'Observed loss' : 'Observed push' : difference > 0 ? 'Ahead' : difference < 0 ? 'Behind' : 'Level', detail];
    if (['split', 'complement', 'game_winner', 'set_winner'].includes(family)) {
      const targets = [spec.target, snapshot.target_game, record.target_game].filter(n => n !== undefined && n !== null);
      const target = targets[0];
      if (!Number.isInteger(target) || target < 1 || target > 5 || selected < 0 || targets.some(n => n !== target)) return unknown('Game target not verified');
      const score = scores?.find(s => s.set === target);
      if (score) return compare((score.home - score.away) * (selected === 0 ? 1 : -1), true, `Game ${target} · ${score.home} – ${score.away}`);
      if (!scores || scores.length + 1 !== target || !/^[1-5](st|nd|rd|th)?$/.test(String(event.period)) || Number.parseInt(event.period, 10) !== target || !pair(event.points)) return unknown('Target game score unavailable');
      return compare((event.points[0] - event.points[1]) * (selected === 0 ? 1 : -1), false, `Game ${target} points · ${event.points.join(' – ')}`);
    }
    if (['closeout', 'moneyline', 'match_winner'].includes(family)) {
      if (selected < 0 || !pair(event.set_tally) || scores === null || (finalStatus(event) && !isFinal)) return unknown('Match target not verified');
      return compare((event.set_tally[0] - event.set_tally[1]) * (selected === 0 ? 1 : -1), isFinal, `Match sets · ${event.set_tally.join(' – ')}`);
    }
    if (['set_handicap', 'point_handicap'].includes(family)) {
      if (selected < 0 || !finite(spec.line) || !scores || (finalStatus(event) && !isFinal)) return unknown('Handicap target not verified');
      if (family === 'point_handicap' && !isFinal && (!pair(event.points) || Number.parseInt(event.period, 10) !== scores.length + 1)) return unknown('Current points unavailable');
      const diff = family === 'set_handicap' ? event.set_tally[0] - event.set_tally[1] : scores.reduce((sum, s) => sum + s.home - s.away, 0) + (pair(event.points) && !isFinal ? event.points[0] - event.points[1] : 0);
      return compare(diff * (selected === 0 ? 1 : -1) + spec.line, isFinal, `Observed ${family === 'set_handicap' ? 'sets' : 'points'} + line ${spec.line}`);
    }
    if (family === 'match_points') {
      if (!finite(spec.line) || typeof spec.over !== 'boolean' || !scores || (finalStatus(event) && !isFinal)) return unknown('Total target not verified');
      if (!isFinal && (!pair(event.points) || Number.parseInt(event.period, 10) !== scores.length + 1)) return unknown('Current points unavailable');
      const total = scores.reduce((sum, s) => sum + s.home + s.away, 0) + (pair(event.points) && !isFinal ? event.points[0] + event.points[1] : 0);
      return compare((total - spec.line) * (spec.over ? 1 : -1), isFinal, `Observed total ${total} · line ${spec.line}`);
    }
    if (family === 'correct_score' && pair(spec.score) && Math.max(...spec.score) === 3 && Math.min(...spec.score) < 3 && isFinal) {
      return [spec.score.every((n, i) => n === event.set_tally[i]) ? 'Observed win' : 'Observed loss', `Final observed sets · ${event.set_tally.join(' – ')}`];
    }
    return unknown('Selection / target not verified');
  }
  function render(context = {}) {
    const now = Date.now() + (finite(context.serverOffset) ? context.serverOffset * 1000 : 0);
    const source = !!context.connected && !!context.healthy;
    const confidence = context.confidence === 90 ? '90' : '95';
    // These maps contain server-computed EV percentages, despite the legacy field name.
    const levelEV = (outcome, map, fallback) => outcome[map] && typeof outcome[map] === 'object' ? outcome[map][confidence] : outcome[fallback];
    const slipCurrent = leg => leg.slipGeneration === slipGeneration && recent(leg.checked_at, now, 6);
    const boardFresh = source && board && recent(board.updated_at, now, 6) && stamp(board.expires_at) > now;
    const scoreFresh = event => !!boardFresh && event?.generation === generation && recent(event.score_at, now, 5);
    for (const [key, leg] of legs) {
      const event = events.get(String(leg.event_id));
      if (event && scoreFresh(event) && finished(event)) legs.delete(key);
    }
    const groups = new Map([...events].map(([id, event]) => [id, {event, legs: [], positions: []}]));
    for (const [id, event] of upcoming) if (!groups.has(id)) groups.set(id, {upcoming: event, legs: [], positions: []});
    for (const leg of legs.values()) {
      const id = identity(leg.event_id) ? String(leg.event_id) : `slip:${JSON.stringify([leg.market_id, leg.selection_id, leg.label])}`;
      if (!groups.has(id)) groups.set(id, {legs: [], positions: []});
      groups.get(id).legs.push(leg);
    }
    for (const record of records.values()) {
      const id = identity(record.snapshot?.event_id) ? String(record.snapshot.event_id) : `position:${record.id}`;
      if (!groups.has(id)) groups.set(id, {legs: [], positions: []});
      groups.get(id).positions.push(record);
    }
    const cards = [];
    let live = 0, currentLegs = 0;
    for (const [id, group] of groups) {
      const {event, upcoming: scheduled, legs: staged, positions: mine} = group;
      const fresh = scoreFresh(event), isLive = !!event && fresh && liveStatus(event) && !finished(event);
      const physical = staged.filter(leg => slipCurrent(leg) && !submitted(leg, event, mine));
      currentLegs += physical.length;
      if (isLive) live++;
      if (filter === 'live' && !isLive || filter === 'staged' && !physical.length || filter === 'mine' && !mine.length) continue;
      const card = node('article', 'match-card'); card.dataset.eventId = id; card.dataset.stale = !fresh;
      const head = node('div', 'match-summary');
      const names = event?.players || scheduled?.players || mine.find(r => players(r.snapshot?.players))?.snapshot.players;
      head.append(node('h2', '', names ? names.join(' vs ') : `Match ${identity(event?.event_id) ? event.event_id : identity(staged[0]?.event_id) ? staged[0].event_id : 'identity unavailable'}`));
      const scheduleFresh = source && schedule && scheduled?.generation === generation && recent(schedule.updated_at, now, 10) && stamp(schedule.expires_at) > now;
      const state = !event ? scheduled ? scheduleFresh ? 'Scheduled / awaiting live mapping' : 'Stale schedule · last observation' : 'Awaiting verified match' : !fresh ? 'Stale · last observation' : finished(event) ? 'Completed · observed' : event.status || 'Status unknown';
      head.append(node('span', 'pill', state));
      const score = event ? `Sets ${pair(event.set_tally) ? event.set_tally.join(' – ') : '—'} · ${event.period ? `Period ${event.period} · ` : ''}Points ${pair(event.points) ? event.points.join(' – ') : '—'}` : scheduled ? `Scheduled ${Number.isFinite(stamp(scheduled.scheduled_at)) ? new Date(scheduled.scheduled_at).toLocaleString() : 'time unavailable'} · ${scheduled.history_count ?? 'Unknown'} H2H meetings` : 'Score unavailable';
      head.append(node('p', 'match-score', score));
      if (event) head.append(node('p', 'small', `Score ${Number.isFinite(stamp(event.score_at)) ? new Date(event.score_at).toLocaleTimeString() : 'time unavailable'}${fresh ? '' : ' · live EV hidden'}`));
      card.append(head);
      const offers = node('div', 'match-offers');
      for (const market of list(event?.markets)) for (const outcome of list(market.outcomes)) {
        const current = fresh && pair(event.set_tally) && pair(event.points) && !finalStatus(event) && outcome.generation === generation && recent(outcome.quote_at, now, 5) && finite(outcome.decimal_odds) && outcome.decimal_odds > 1;
        const row = node('div', 'board-offer'); row.dataset.selectionId = outcome.selection_id;
        const label = node('div', 'offer-label'); label.append(node('strong', '', `${outcome.label || 'Selection'}${finite(outcome.line) ? ` · ${outcome.line}` : ''}`), node('span', 'small', market.name || 'Market'));
        const ev = node('div', 'offer-ev');
        const currentEV = levelEV(outcome, 'lower_probabilities', 'conservative_ev'), entryEV = levelEV(outcome, 'entry_evs', 'entry_ev');
        ev.append(node('strong', 'current-ev', `Conservative EV ${current ? edge(currentEV) : '—'}`));
        if (finite(entryEV)) ev.append(node('span', 'small entry-ev', `Entry EV ${edge(entryEV)} · historical`));
        ev.append(node('span', 'small', !current ? 'Stale / unavailable quote or score' : outcome.entry_open === true ? 'Entry open' : 'Entry closed · midgame estimate when available'));
        row.append(label, node('span', 'offer-price', `${price(outcome.decimal_odds)}${current ? '' : ' · last'}`), ev); offers.append(row);
      }
      if (!offers.childElementCount) offers.append(node('p', 'small no-offers', 'No current market / conservative EV available.'));
      for (const leg of staged) {
        const row = node('div', 'staged-leg'); row.dataset.selectionId = leg.selection_id ?? '';
        const wasSubmitted = submitted(leg, event, mine), slipFresh = slipCurrent(leg) && !wasSubmitted;
        const state = wasSubmitted ? 'submitted' : leg.slipGeneration !== slipGeneration ? 'absent' : slipFresh ? 'present' : 'stale';
        const label = {submitted: 'Submitted', absent: 'No longer in slip · last observation', present: 'Staged', stale: 'Slip status unknown · last observation'}[state];
        row.dataset.stale = !slipFresh;
        row.dataset.slipState = state;
        row.append(node('strong', '', `${label} · ${leg.label}${finite(leg.line) ? ` · ${leg.line}` : ''}`), node('span', '', `${leg.market || 'Market'} · ${slipFresh ? 'Slip' : 'Last slip'} odds ${price(leg.decimal_odds)}${slipFresh ? ` · ${leg.status || 'Status unknown'}` : ''}`), node('span', 'small', `Last slip check ${Number.isFinite(stamp(leg.checked_at)) ? new Date(leg.checked_at).toLocaleTimeString() : 'unavailable'} · retained until verified completion`));
        offers.append(row);
      }
      for (const record of mine) {
        const [status, detail] = track(record, event, fresh), snapshot = record.snapshot || {};
        const row = node('div', 'position-row'); row.dataset.positionId = record.id; row.dataset.tracking = status;
        const reported = record.outcome === 'placed' ? 'Placed' : record.outcome === 'uncertain' ? 'Unconfirmed' : `Report: ${record.outcome || 'Unknown'}`;
        row.append(node('strong', '', `${reported} · ${snapshot.selection || 'Selection unavailable'}`), node('span', 'position-status', status), node('span', 'small', `${snapshot.market || 'Market'} · ${detail} · Entry odds ${price(snapshot.decimal_odds)}`));
        if (finite(snapshot.point_ev)) row.append(node('span', 'small', `Entry point EV ${edge(snapshot.point_ev)} · historical`));
        offers.append(row);
      }
      card.append(offers); cards.push(card);
    }
    const signature = JSON.stringify([cards.map(card => card.outerHTML), filter]);
    if (signature !== lastSignature) { $('board-events').replaceChildren(...cards); lastSignature = signature; }
    $('board-count').textContent = groups.size;
    const mapped = Number.isInteger(context.counts?.mapped) ? context.counts.mapped : null;
    const providerLive = Number.isInteger(context.counts?.dk_live) ? context.counts.dk_live : null;
    // Surface the mapping gap: live DraftKings matches without verified
    // history can never qualify, and the board alone hides that reason.
    const mappingGap = mapped !== null && providerLive !== null && mapped < providerLive
      ? ` · DraftKings live ${providerLive} · ${mapped} mapped` : '';
    $('board-status').textContent = `${live} live · ${currentLegs} staged legs · ${legs.size} retained slip observations · ${records.size} tracked positions · ${confidence}% confidence${mappingGap} · ${!board ? 'Board unavailable / legacy snapshot' : boardFresh ? 'Source current' : 'Source stale / paused'}`;
    $('board-empty').hidden = cards.length > 0;
    $('board-empty').textContent = groups.size ? 'No matches in this filter.' : 'No matches received yet. Legacy scanner details appear below.';
  }
  for (const button of document.querySelectorAll('[data-filter]')) button.onclick = () => {
    filter = button.dataset.filter;
    for (const control of document.querySelectorAll('[data-filter]')) control.setAttribute('aria-pressed', String(control === button));
    render(lastContext);
  };
  let lastContext = {};
  return {scan, slip, positions, reset, render: context => { lastContext = context; render(context); }};
})();
