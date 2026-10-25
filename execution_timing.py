"""Monotonic, JSON-safe timings for the serialized browser execution lane."""

from datetime import datetime, timezone
from collections import deque
import time


class ExecutionTiming:
    def __init__(self, event=None, market=None):
        self.started = time.perf_counter()
        now = datetime.now(timezone.utc)
        self.started_at = now.isoformat()
        self.identifiers = {"eventId": (event or {}).get("eventId"), "marketId": (market or {}).get("marketId")}
        self.stages = []
        self.marks = {}
        self.market_readiness = None
        self.submitted_at = None
        self.submission_acknowledged = False
        self.source_ages = {}
        for name, row in (("scoreAgeAtStartMs", event), ("quoteAgeAtStartMs", market)):
            try:
                received = datetime.fromisoformat((row or {})["receivedAt"])
                self.source_ages[name] = round((now - received).total_seconds() * 1000, 3)
            except (KeyError, TypeError, ValueError):
                self.source_ages[name] = None

    def call(self, name, function, *args, **kwargs):
        start = time.perf_counter()
        stage = {"name": name, "offsetMs": round((start - self.started) * 1000, 3)}
        try:
            result = function(*args, **kwargs)
            if name == 'marketReveal' and isinstance(result,dict):
                self.market_readiness = {key:result.get(key) for key in ('status','reason','missingRow')}
            return result
        except BaseException as error:
            stage["errorType"] = type(error).__name__
            raise
        finally:
            stage["durationMs"] = round((time.perf_counter() - start) * 1000, 3)
            self.stages.append(stage)

    def snapshot(self, status=None):
        command_offset = next((stage['offsetMs'] for stage in self.stages if stage['name']=='finalSubmit'), None)
        dispatched = command_offset if self.submission_acknowledged else None
        measurement = 'command-start' if dispatched is not None else None
        if self.submitted_at is not None:
            delta = (self.submitted_at - datetime.fromisoformat(self.started_at)).total_seconds()*1000
            if delta >= 0:
                dispatched, measurement = round(delta,3), 'browser-click'
        return {"startedAt": self.started_at, "elapsedMs": round((time.perf_counter() - self.started) * 1000, 3),
                "status": status, **self.identifiers, **self.source_ages,
                "marksMs": dict(self.marks), "stages": list(self.stages),
                "marketReadiness": dict(self.market_readiness) if self.market_readiness else None,
                'signalToSubmitMs':dispatched,'dispatchMeasurement':measurement,'dispatchTargetMs':1000,
                'submitCommandOffsetMs':command_offset,
                'withinDispatchTarget':dispatched <= 1000 if dispatched is not None else None,
                'receiptWaitMs':sum(stage['durationMs'] for stage in self.stages if stage['name']=='receiptWait')}

    def note_submission(self, result):
        self.submission_acknowledged = True
        try:
            stamp = datetime.fromisoformat(result['submittedAt'].replace('Z','+00:00'))
            self.submitted_at = stamp if stamp.tzinfo is not None else None
        except (KeyError,TypeError,ValueError):
            self.submitted_at = None

    def mark(self, name):
        self.marks[name] = round((time.perf_counter() - self.started) * 1000, 3)


def stage_summary(timing):
    """Keep the console bounded while the ledger retains every timed iteration."""
    totals = {}
    for stage in timing['stages']:
        row = totals.setdefault(stage['name'],{'count':0,'duration':0.0})
        row['count'] += 1
        row['duration'] += stage['durationMs']
    text = ', '.join(f"{name}={row['duration']:.1f}ms" + (f"/{row['count']} calls" if row['count']>1 else '')
                     for name,row in totals.items())
    readiness = timing.get('marketReadiness') or {}
    if readiness.get('reason'):
        text += f"; last market wait: {readiness['reason']}"
    return text


class OpportunityTimings:
    """Called under the planner's data lock; never performs browser or file IO."""

    def __init__(self):
        self.active = {}
        self.expired = deque()

    @staticmethod
    def key(item):
        selected = item["decision"]["selected"]
        info = selected["context"]
        return (item["eventId"], item["market"]["marketId"], selected["outcome"]["selectionId"],
                info["betType"], tuple(info["scoreState"]))

    def observe(self, pending):
        keys = set()
        for item in pending:
            key = self.key(item)
            keys.add(key)
            if key not in self.active:
                self.active[key] = {"timing": ExecutionTiming(item["event"], item["market"]),
                                    "decision": item["decision"], "submitting": False}
            item["_executionTiming"] = self.active[key]["timing"]
        for key, entry in list(self.active.items()):
            if key not in keys and not entry["submitting"]:
                entry["timing"].mark("qualificationEnded")
                self.expired.append(self.active.pop(key))

    def get(self, item):
        # Keep the original reference if a background scan withdraws this item
        # while its foreground navigation/reveal command is still returning.
        return item["_executionTiming"]

    def submitting(self, item):
        entry = self.active.get(self.key(item))
        if entry is None or entry["timing"] is not self.get(item):
            return False
        entry["submitting"] = True
        return True

    def finish(self, item):
        entry = self.active.get(self.key(item))
        if entry is not None and entry["timing"] is self.get(item):
            self.active.pop(self.key(item))

    def drain_expired(self):
        entries = list(self.expired)
        self.expired.clear()
        return entries
