// Reveal one exact market; selection buttons are never clicked.
const config = arguments[0];
const watcherVersion = 7;
let state = window.__ttNextSetWatch;
if (state && (!config || state.key !== config.key || state.version !== watcherVersion)) {
    state.observer.disconnect();
    clearTimeout(state.expiryTimer);
    window.__ttNextSetWatch = state = null;
}
if (!config) return null;
if (!state) {
    const normalize = text => text.toLowerCase().replace(/[łøđ]/g, c => ({ł:'l',ø:'o',đ:'d'})[c])
        .normalize('NFKD').replace(/\p{M}/gu, '').replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
    const visible = element => {
        if (!element?.getClientRects().length) return false;
        for (let node = element; node instanceof Element; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
        }
        return true;
    };
    state = {key: config.key, version: watcherVersion, status: 'waiting', config, expanding: false};
    window.__ttNextSetWatch = state;
    const check = () => {
        // Revalidate on foreground calls as prices/availability can change after
        // the first reveal. The observer itself still disconnects on success.
        state.status = 'waiting';
        state.reason = null;
        state.missingRow = false;
        if (location.href.split('?')[0] !== state.config.pageUrl || Date.now() >= state.config.validUntil) {
            state.reason = 'page changed or data expired';state.observer.disconnect();return;
        }
        if (loadNotice()) { state.reason = 'provider load error; preparation paused'; return; }
        const scoreboards = [...document.querySelectorAll('.cms-event-scoreboard-container')];
        if (!scoreboards.some(scoreboard => scoreboard.getClientRects().length && state.config.players.every(p =>
            ` ${normalize(scoreboard.innerText)} `.includes(` ${normalize(p)} `)))) { state.reason = 'scoreboard identity'; return; }
        if (state.config.displayText) {
            let panel=document.getElementById('tt-conditional-split-info');
            if(!panel){panel=document.createElement('div');panel.id='tt-conditional-split-info';panel.setAttribute('role','status');
                panel.setAttribute('aria-live','polite');scoreboards[0].insertAdjacentElement('afterend',panel);}
            if(panel.textContent!==state.config.displayText)panel.textContent=state.config.displayText;
            const style='padding:14px;margin:12px 0;color:#fff;background:#222;font:16px/1.5 sans-serif;border:2px solid '+
                (state.config.displayEligible?'#53d337':'#aaa');
            if(panel.dataset.ttEligible!==String(!!state.config.displayEligible)){
                panel.style.cssText=style;panel.dataset.ttEligible=String(!!state.config.displayEligible);}
        }
        const tabLabels = state.config.tabLabels || ['Player Props', 'Game Props'];
        if (![...document.querySelectorAll('[role="tab"][aria-selected="true"]')]
            .some(t => tabLabels.includes(t.textContent.trim()))) { state.reason = 'market tab not selected'; return; }
        const moneyline = state.config.betType === 'closeout' && state.config.sectionTitle === 'Moneyline';
        const sectionTitle = state.config.sectionTitle || (moneyline ? 'Moneyline' : 'Winner - Listed Game');
        const sectionTitles = (state.config.sectionTitles || [sectionTitle]).map(normalize);
        const groups = [...document.querySelectorAll('[data-testid="collapsible-wrapper"]')]
            .filter(g => visible(g) && sectionTitles.includes(normalize(g.querySelector('[data-testid="collapsible-header"]')?.textContent || '')));
        const kind = state.config.distribution ? 'market' : 'winner';
        if (!groups.length) { state.missingRow = true; state.reason = kind + ' section missing'; return; }
        if (groups.length !== 1) { state.reason = kind + ' section ambiguous'; return; }
        const group = groups[0];
        if (!state.positioned) {
            state.positioned = true;
            group.scrollIntoView({block: 'center', behavior: 'instant'});
        }
        const trigger = group.querySelector('[data-testid="collapsible-trigger"]');
        if (trigger?.getAttribute('aria-expanded') === 'false') {
            if (!state.expanding) { state.expanding = true; trigger.click(); }
            state.reason = 'expanding ' + kind + ' section'; return;
        }
        state.expanding = false;
        const indexed = new Map();
        for(const b of document.querySelectorAll('button[data-testid]')){
            const id=b.getAttribute('data-testid');
            if(state.config.testIds.includes(id)&&visible(b))indexed.set(id,[...(indexed.get(id)||[]),b]);
        }
        const buttons = state.config.testIds.map(id => indexed.get(id)||[]);
        if (buttons.some(matches => matches.length !== 1)) { state.missingRow = true; state.reason = 'selection IDs missing or ambiguous'; return; }
        const choices = buttons.map(matches => matches[0]);
        const matchMarket = moneyline || state.config.distribution;
        const row = choices[0].closest('[data-testid="market-template"]') || (matchMarket ? group : null);
        // DraftKings can render the exact market row beside its collapsible
        // header, rather than inside that wrapper (including Moneyline).
        if (!row || !choices.every(b => (b.closest('[data-testid="market-template"]') || (matchMarket ? group : null)) === row)) { state.reason = 'selections are not in one market row'; return; }
        const rowName = row.querySelector('[data-testid="market-label"]')?.textContent.trim();
        if (matchMarket ? (rowName && !(state.config.rowNames || ['Moneyline']).includes(rowName)) : rowName !== state.config.name) { state.reason = 'game number mismatch'; return; }
        if (choices.some((b, i) => b.disabled || b.getAttribute('aria-disabled') === 'true'
            || b.getAttribute('aria-pressed') === 'true' || !b.getClientRects().length
            || getComputedStyle(b).visibility === 'hidden'
            || normalize(b.querySelector('[data-testid="button-title-market-board"]')?.textContent || '') !== normalize(state.config.labels[i])
            || !/^[+\-\u2212]?\d+$/.test(b.querySelector('[data-testid="button-odds-market-board"]')?.textContent.trim() || ''))) { state.reason = 'selections disabled, hidden, already selected, or names/prices invalid'; return; }
        state.status = 'revealed';
        state.reason = null;
        state.revealedAt = Date.now();
        state.observer.disconnect();
        row.scrollIntoView({block: 'center', behavior: 'instant'});
        row.style.outline = '3px solid #53d337';
    };
    state.observer = new MutationObserver(()=>{
        if(state.scheduled)return;
        state.scheduled=true;
        setTimeout(()=>{state.scheduled=false;if(window.__ttNextSetWatch===state)check();},50);
    });
    state.observer.observe(document.body, {subtree: true, childList: true, attributes: true, characterData: true,
        attributeFilter:['aria-expanded','aria-selected','aria-disabled','aria-pressed','disabled','hidden','class','style']});
    state.check = check;
}
state.config = config;
clearTimeout(state.expiryTimer);
state.expiryTimer=setTimeout(()=>{state.observer.disconnect();state.status='waiting';state.reason='page changed or data expired';},
    Math.min(2147483647,Math.max(0,config.validUntil-Date.now())));
state.check();
return {status: state.status, reason: state.reason || null, missingRow: !!state.missingRow, revealedAt: state.revealedAt || null};
