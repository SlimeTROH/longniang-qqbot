"""Keep bounded per-client fingerprints for differential game responses."""
from collections import OrderedDict
from hashlib import blake2b
import json
import secrets

ROW_KEYS = {"warehouse":"stock_id", "equipment":"equip_id", "supplies":"stock_id"}


def _fingerprint(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return blake2b(raw, digest_size=16).digest()


class SnapshotCache:
    def __init__(self, capacity=192, row_budget=32000):
        self.capacity = capacity
        self.row_budget = row_budget
        self.rows_cached = 0
        self.baselines = OrderedDict()

    def pack(self, owner, state, base="", revision=""):
        previous = self.baselines.get((owner, base))
        signatures = {}
        cost = 0
        for name, value in state.items():
            key = ROW_KEYS.get(name)
            if key and isinstance(value,list) and len(value) <= 4096 and all(key in row for row in value):
                signatures[name] = tuple((row[key],_fingerprint(row)) for row in value)
                cost += len(value)
            elif name in ("player", "run") and isinstance(value, dict):
                signatures[name] = {key: _fingerprint(item) for key, item in value.items()}
            else:
                signatures[name] = _fingerprint(value)
        token = secrets.token_urlsafe(16)
        self.baselines[owner, token] = (state["revision"], signatures, cost)
        self.rows_cached += cost
        while len(self.baselines) > self.capacity or (self.rows_cached > self.row_budget and len(self.baselines)>1):
            _, evicted = self.baselines.popitem(last=False)
            self.rows_cached -= evicted[2]
        if not previous or previous[0] != revision or previous[1].keys() != signatures.keys():
            return {"state": state, "sync_token": token}
        old = previous[1]
        fields, sections, lists = {}, {}, {}
        for name, new in signatures.items():
            if old[name] == new:
                continue
            if isinstance(new, tuple) and isinstance(old[name], tuple):
                prior = dict(old[name])
                changed = [row for row,(identifier,digest) in zip(state[name],new) if prior.get(identifier) != digest]
                if not new or len(changed)*2 > len(new):
                    fields[name] = state[name]
                else:
                    lists[name] = {"key":ROW_KEYS[name],"upsert":changed}
                    order = [identifier for identifier,_ in new]
                    if list(prior) != order:
                        lists[name]["order"] = order
            elif isinstance(new, dict) and isinstance(old[name], dict) and new.keys() == old[name].keys():
                sections[name] = {key: state[name][key] for key in new if old[name][key] != new[key]}
            else:
                fields[name] = state[name]
        return {"delta": {"base": base, "base_revision": revision, "fields": fields, "sections": sections, "lists": lists},
                "sync_token": token}
