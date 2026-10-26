"""Execution-layer, zero-stake round-robin staging of validated scanner hints."""
import time

from ember_protocol import timestamp
from tt_market_exec import read_plan_slip, remove_closed_picks


def stage_key(stage):
    return (stage["event_id"], stage["market_id"], stage["selection_id"])


class Staging:
    def __init__(self):
        self.cursor = 0
        self.pending = None
        self.inventory = []
        self.read_snapshot = None
        self.read_at = float("-inf")

    def recent_snapshot(self, max_age=1.0, clock=time.monotonic):
        """The slip read from this tick; a publisher never needs a second one."""
        if self.read_snapshot is not None and clock()-self.read_at <= max_age:
            return self.read_snapshot
        return None

    def add(self, driver, core, event, market, outcome, valid, timeout,*,min_decimal=None,observe_price=None):
        snapshot = read_plan_slip(driver)
        if snapshot.get('stagingSafe') is not True or not valid() or timeout <= 0:
            return {'status': 'not_ready'}
        target = (event['eventId'], market['marketId'], outcome['selectionId'])
        if any((leg['eventId'], leg['marketId'], leg['selectionId']) == target for leg in snapshot.get('legs', [])):
            return {'status': 'selected'}
        return core.click_selection(driver, event, outcome, market, validate=valid, timeout=timeout,
                                    **({'min_decimal':min_decimal} if min_decimal is not None else {}),
                                    **({'observe_price':observe_price} if observe_price is not None else {}))

    def step(self, driver, core, readiness, stages, valid, server_now, max_wait=.3, focus=None):
        """At most one absent leg per tick; expiry never removes a physical leg."""
        if not valid() or any(getattr(driver, flag, False) is True for flag in
                              ("_tt_plan_uncertain", "_tt_plan_stopped")):
            return None
        started = time.monotonic()
        snapshot = read_plan_slip(driver)
        if snapshot.get('reason') == 'bet slip is hidden or ambiguous':
            core.ensure_slip_open(driver, timeout=min(.05, max_wait))
            snapshot = read_plan_slip(driver)
        if snapshot.get('stagingSafe') is True and any(leg.get('closed') is True for leg in snapshot.get('legs', [])):
            if not valid() or remove_closed_picks(driver).get('status') != 'cleared':
                return None
            snapshot = read_plan_slip(driver)
        self.inventory = snapshot.get("legs", [])
        if snapshot.get("status") not in ("empty", "present") or snapshot.get("stagingSafe") is not True:
            return None
        self.read_snapshot, self.read_at = snapshot, started
        fresh = [s for s in stages[:128] if s.get("event") and timestamp(s["expires_at"]) > server_now]
        if focus is not None:
            # The navigation arbiter owns every page change; staging only
            # clicks on the page the arbiter already put the browser on.
            fresh = [s for s in fresh if str(s["event_id"]) == focus]
            if fresh and (not hasattr(core, "correct_event") or not core.correct_event(driver, fresh[0]["event"])):
                return None
        present = {(leg["eventId"], leg["marketId"], leg["selectionId"]) for leg in self.inventory}
        absent = [s for s in fresh if stage_key(s) not in present]
        if not absent:
            return None
        stage = next((s for s in absent if stage_key(s) == self.pending), None)
        if stage is None:
            stage = absent[self.cursor % len(absent)]
            self.cursor += 1
            # A slow reveal may spend a turn; give its page one follow-up before rotating.
            self.pending = stage_key(stage)
        else:
            self.pending = None
        event = stage["event"]
        siblings = [s for s in fresh if (s["event_id"], s["market_id"]) ==
                    (stage["event_id"], stage["market_id"]) and stage_key(s) not in present]
        market = {"eventId": stage["event_id"], "marketId": stage["market_id"],
                  "name": stage["market_name"], "betType": stage["bet_type"],
                  "receivedAt": stage["updated_at"],
                  "outcomes": [{"selectionId": s["selection_id"], "label": s["label"],
                                "points": s["line"]} for s in siblings]}
        if market["name"] == "Moneyline":
            market["scoreState"] = core.score_state(event)
        current = lambda: valid() and timestamp(stage["expires_at"]) > server_now+time.monotonic()-started
        remaining = lambda: max_wait+.6-(time.monotonic()-started)
        if readiness.prepare(driver, event, market, current, remaining,
                             max_wait=max(0, max_wait-(time.monotonic()-started)), staging=True).get("status") != "revealed":
            return None
        left = max(.001, max_wait-(time.monotonic()-started))
        outcome = next(row for row in market["outcomes"] if row["selectionId"] == stage["selection_id"])
        clicked = self.add(driver, core, event, market, outcome, current, left)
        if clicked.get('status') in ('clicked', 'selected'):
            self.pending = None
        return stage if clicked.get("status") in ("clicked", "selected") else None
