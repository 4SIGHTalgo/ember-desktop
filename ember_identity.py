"""Stdlib wire identity shared by scanner, server, and public executor."""
import re
import unicodedata


def provider_id(value, kind):
    patterns = {"event": r"[0-9]{1,32}", "market": r"[A-Za-z0-9_#:.+\-]{1,64}",
                "selection": r"[A-Za-z0-9_#:.+\-]{1,200}"}
    return isinstance(value, str) and re.fullmatch(patterns[kind], value) is not None


def name_key(name):
    name = unicodedata.normalize("NFKD", name.casefold().translate(str.maketrans("łøđ", "lod")))
    return " ".join("".join(c if c.isalnum() else " " for c in name
                            if not unicodedata.combining(c)).split())


def observation_identity(value):
    if isinstance(value, dict):
        return {key: observation_identity(item) for key, item in value.items()
                if key not in {"receivedAt", "publishedAt", "type"}}
    if isinstance(value, list):
        rows = [observation_identity(item) for item in value]
        if rows and all(isinstance(row, dict) and "selectionId" in row for row in rows):
            return sorted(rows, key=lambda row: row["selectionId"])
        return rows
    return value


def signal_identity(signal):
    if signal.get('protocol')==3:
        payload=signal['payload'];event,market,plan=(payload[key] for key in ('event','market','plan'))
        return [signal['protocol'],signal['id'],signal['contract'],
                [event.get(key) for key in ('eventId','player1','player2','startTimestamp')],
                [market.get(key) for key in ('eventId','marketId','name')],
                {key:plan[key] for key in ('betType','selection','selectionId','scoreState','nextSet','specification','window')}]
    value = observation_identity({key: signal[key] for key in ("protocol", "id", "contract", "payload")})
    if "plan" in value["payload"]:
        value["payload"]["plan"].pop("deadline", None)
    return value
