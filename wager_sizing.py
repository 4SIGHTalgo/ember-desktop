"""Browser-side cash balance reading.

The caller supplies visibleUI. Only explicit account balance controls are read;
arbitrary dollar amounts in markets, receipts and bonus balances are not bankroll.
The public client only reads; every stake arrives server-authorized.
"""

BALANCE_READER_JS = r"""
const readAvailableBalance = () => {
    const selectors = [
        '#standardized-header-balance', '#standardized-header-balance-button',
        '#standardized-header-account-balance',
        '[data-testid="standardized-header-balance"]',
        '[data-testid="standardized-header-combined-balance-cash"]',
        '[data-testid="account-balance"]', '[data-testid="available-balance"]'
    ];
    const controls = [...document.querySelectorAll(selectors.join(','))].filter(visibleUI);
    const amounts = controls.map(element => {
        const text = (element.innerText || '').replace(/\s+/g, ' ').trim();
        // One USD amount only. A control combining cash with bonus/crown cash
        // is ambiguous and must not be silently treated as spendable cash.
        if (/bonus|crown|free\s*bet|total\s*balance|\bDK\s*\$/i.test(text)) return null;
        const matches = [...text.matchAll(/\$\s*((?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2})(?!\d)/g)];
        if (matches.length !== 1 || /[-(]\s*\$/.test(text)) return null;
        return Number(matches[0][1].replace(/,/g, ''));
    });
    if (!amounts.length || amounts.some(value => value === null || !Number.isFinite(value) || value < 0)
        || amounts.some(value => value !== amounts[0])) {
        return {reason: 'available account cash balance is missing, unreadable or ambiguous'};
    }
    return {availableBalance: amounts[0], balanceSource: 'visible-account-balance',
            balanceReadAt: new Date().toISOString()};
};
const slipDecimal = slip => {
    const prices = [...slip.querySelectorAll('[data-testid="betslip-odds-standard"]')]
        .filter(visibleUI).map(element => (element.innerText || '').trim().replace(/\u2212|\u2013/g, '-'));
    const decimals = prices.map(text => /^[+\-]?\d+$/.test(text) && Number(text) !== 0
        ? 1 + (Number(text) < 0 ? 100 / -Number(text) : Number(text) / 100) : NaN);
    return decimals.length && decimals.every(d => Number.isFinite(d) && d > 1 && Math.abs(d - decimals[0]) < 1e-9)
        ? decimals[0] : null;
};
"""
