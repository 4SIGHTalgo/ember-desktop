"""Exercise the plan quote, arming and submission comparisons in a local Chrome fixture."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selenium.webdriver.chrome.options import Options

import tt_market_exec as execution
from driver_lifecycle import create_owned_chrome


def bet_event(identifier="10", **overrides):
    return {"type": "score", "eventId": identifier, "player1": "Piotr Zemla", "player2": "Tomasz Lojtek",
            "startTimestamp": datetime.now(timezone.utc).timestamp() - 100, "status": "STARTED",
            "period": "2nd", "setTally": [1, 0],
            "sets": [{"set": 1, "home": 11, "away": 5}, {"set": 2, "home": 0, "away": 0}],
            "receivedAt": datetime.now(timezone.utc).isoformat(), **overrides}


HTML = '''<button id="standardized-header-account-menu">Account</button>
<span id="standardized-header-balance">$100.00</span>
<div class="cms-event-scoreboard-container">Piotr Zemla Tomasz Lojtek</div>
<button data-testid="component-builder-market-button-10-selection2" aria-pressed="false">
  <span data-testid="button-title-market-board">Tomasz Lojtek</span>
  <span data-testid="button-odds-market-board">+110</span>
</button>
<div data-testid="betslip-content-wrapper">
  <div data-testid="betslip-empty-state-title">Your picks will show up here.</div>
</div>'''

SETUP = r"""
window.nativeClicks = 0;
window.syntheticClicks = 0;
window.submitClicks = 0;
window.ignoreNativeClicks = 0;
window.failSelection = false;
window.marketName = 'Game 2';
window.slipPrice = '+110';
window.selectionDelay = 60;
const selection = document.querySelector('button[data-testid^="component-builder-market-button-"]');
selection.addEventListener('click', event => {
    if (!event.isTrusted) { window.syntheticClicks++; return; }
    window.nativeClicks++;
    if (window.failSelection || window.nativeClicks <= window.ignoreNativeClicks) return;
    selection.setAttribute('aria-pressed', 'true');
    setTimeout(() => {
        const slip = document.querySelector('[data-testid="betslip-content-wrapper"]');
        slip.innerHTML = `<div>Tomasz Lojtek</div><div>${window.marketName}</div>
            <span data-testid="betslip-odds-standard">${window.slipPrice}</span>
            <input data-testid="betslip-wager-box-input" value="">
            <button data-testid="betslip-place-bet-button" disabled>Place Bet $1.00</button>`;
        const input = slip.querySelector('input');
        const submit = slip.querySelector('button');
        input.addEventListener('input', () => {
            setTimeout(() => { submit.disabled = !(Number(input.value) > 0); }, 40);
        });
        submit.addEventListener('click', () => {
            window.submitClicks++;
            const text = slip.querySelector('[data-testid="betslip-odds-standard"]').textContent.replace(/\u2212/g, '-');
            const american = Number(text);
            const decimal = 1 + (american < 0 ? 100 / -american : american / 100);
            const stake = Number(input.value);
            slip.textContent = `Bet placed. $${stake.toFixed(2)} to win $${(stake * decimal).toFixed(2)}.`;
        });
    }, window.selectionDelay);
});
"""


PLAN_SETUP = SETUP.replace(
    'slip.innerHTML = `<div>',
    '''slip.innerHTML = `<section><button data-testid="betslip-selection-card-ex-button"
        data-tracking='{"selectionId":"selection2","eventId":"10","marketId":"m2"}'>Remove</button><div>'''
).replace('<button data-testid="betslip-place-bet-button" disabled>',
          '</section><button data-testid="betslip-place-bet-button" disabled>').replace(
              "const submit = slip.querySelector('button');",
              "const submit = slip.querySelector('[data-testid=betslip-place-bet-button]');")
PLAN_SETUP += "window.__isLoggedIn=window.__canBet=true;window.__playSpan={playSpan:{clientKey:'fixture-grant',grantType:'PlaySportsbook'}};"


class PlanExecutionBrowserTests(unittest.TestCase):
    """Protocol 2: quote, arm and confirm compare authorized values only."""

    @classmethod
    def setUpClass(cls):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1280,900")
        cls.driver = create_owned_chrome(options)

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for setting in (patch.object(execution, "SLIP_CAPTURE", Path(directory.name) / "slip.json"),
                        patch.object(execution, "SLIP_ARM_TIMEOUT", .8),
                        patch.object(execution, "SLIP_ARM_INTERVAL", .05)):
            setting.start()
            self.addCleanup(setting.stop)
        self.driver.get("about:blank")
        self.driver.execute_script("document.body.innerHTML=arguments[0];" + PLAN_SETUP, HTML)

    def market(self):
        return {"marketId": "m2", "eventId": "10", "name": "Game 2", "isSuspended": False}

    def outcome(self):
        return {"selectionId": "selection2", "label": "Tomasz Lojtek", "decimal": 2.1}

    def plan(self, **overrides):
        return {"betType": "split", "selection": "Tomasz Lojtek", "selectionId": "selection2",
                "scoreState": [1, 0], "nextSet": 2,
                "specification": {"family": "split", "player": 1, "target": 2},
                "min_decimal": 2.0, "stake_cents": 261,
                "deadline": (datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat(),
                "display": {}, **overrides}

    def open_slip(self):
        click = execution.click_selection(self.driver, bet_event(), self.outcome(), self.market())
        self.assertIn(click.get("status"), ("clicked", "selected"))
        self.assertTrue(execution.ensure_slip_open(self.driver, allow_empty=False))

    def identity(self):
        return execution.plan_slip_config(bet_event(), self.market(), self.plan())

    def test_plan_quote_reads_the_balance_and_price_without_writing_a_stake(self):
        self.open_slip()
        quote = self.driver.execute_async_script(execution.WAIT_FOR_PLAN_ARM_SCRIPT,
                                                 {**self.identity(), "readOnly": True, "timeoutMs": 2000, "fallbackMs": 50}) or {}
        self.assertEqual(quote["status"], "armed")
        self.assertEqual(quote["balance"]["availableBalance"], 100)
        self.assertAlmostEqual(quote["decimalOdds"], 2.1)
        self.assertFalse(quote["stakeValue"])
        armed = self.driver.execute_async_script(execution.WAIT_FOR_PLAN_ARM_SCRIPT,
                                                 {**self.identity(), "stake": 2.61, "timeoutMs": 2000, "fallbackMs": 50}) or {}
        self.assertEqual(armed["status"], "armed")
        self.assertEqual(armed["stakeValue"], "2.61")
        self.assertFalse(armed["placeDisabled"])

    def test_plan_confirm_submits_only_at_or_above_the_authorized_floor(self):
        for floor, expected in ((2.0, "submitted"), (2.2, "aborted")):
            with self.subTest(floor=floor):
                self.setUp()
                self.open_slip()
                self.driver.execute_async_script(execution.WAIT_FOR_PLAN_ARM_SCRIPT,
                                                 {**self.identity(), "stake": 2.61, "timeoutMs": 2000, "fallbackMs": 50})
                result = self.driver.execute_script(execution.CONFIRM_PLAN_SCRIPT,
                        {**self.identity(), "stake": 2.61, "minDecimal": floor, "echo": "e1",
                         "selectionLabel": "Tomasz Lojtek", "marketName": "Game 2"}) or {}
                self.assertEqual(result["status"], expected)
                if expected == "submitted":
                    self.assertEqual(result["echo"], "e1")
                    self.assertEqual(result["minDecimal"], floor)
                    self.assertEqual(result["stake"], 2.61)
                    self.assertEqual(self.driver.execute_script("return window.submitClicks"), 1)
                else:
                    self.assertTrue(result["edgeRejected"])
                    self.assertEqual(result["decimalOdds"], 2.1)
                    self.assertEqual(self.driver.execute_script("return window.submitClicks"), 0)

    def test_plan_confirm_treats_a_changed_stake_as_resize_never_a_submit(self):
        self.open_slip()
        self.driver.execute_async_script(execution.WAIT_FOR_PLAN_ARM_SCRIPT,
                                         {**self.identity(), "stake": 2.61, "timeoutMs": 2000, "fallbackMs": 50})
        self.driver.execute_script("document.querySelector('input[data-testid=betslip-wager-box-input]').value='5.00'")
        result = self.driver.execute_script(execution.CONFIRM_PLAN_SCRIPT,
                {**self.identity(), "stake": 2.61, "minDecimal": 2.0, "selectionLabel": "Tomasz Lojtek",
                 "marketName": "Game 2"}) or {}
        self.assertEqual(result["status"], "resize")
        self.assertEqual(self.driver.execute_script("return window.submitClicks"), 0)

    def test_place_plan_executes_end_to_end_by_comparison(self):
        authorized = []

        def authorize_values(quote):
            authorized.append((quote.get("balance", {}).get("availableBalance"), quote.get("decimalOdds")))
            return {"stake": 2.61, "min_decimal": 2.0, "echo": "e1"}

        result = execution.place_plan(self.driver, bet_event(), self.market(), self.plan(),
                                      self.outcome(), authorize_values=authorize_values)
        self.assertEqual(result["status"], "placed")
        self.assertEqual(result["stake"], 2.61)
        self.assertEqual(result["echo"], "e1")
        self.assertEqual(result["sizingMode"], "server_plan")
        self.assertEqual(authorized, [(100, 2.1)])
        self.assertEqual(self.driver.execute_script("return window.nativeClicks"), 1)
        self.assertEqual(self.driver.execute_script("return window.submitClicks"), 1)

    def test_place_plan_never_submits_when_the_server_withholds_the_stake(self):
        result = execution.place_plan(self.driver, bet_event(), self.market(), self.plan(),
                                      self.outcome(), authorize_values=lambda quote: {"denied": "signal_expired"})
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["denied"], "signal_expired")
        self.assertEqual(self.driver.execute_script("return window.submitClicks"), 0)
        self.assertEqual(self.driver.execute_script("return window.nativeClicks"), 1)


if __name__ == "__main__":
    unittest.main()
