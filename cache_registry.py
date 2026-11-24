"""Merge cache metadata without replacing other workers' published entries."""
import json
from pathlib import Path
from shared_history import _file_lock, atomic_json


def load(path: Path):
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise ValueError('Invalid cache registry')
    return {key:entry for key,entry in value.items() if isinstance(entry,dict) and entry.get('name')}


def save(path: Path,current: dict,baseline: dict):
    changes={key:value for key,value in current.items() if baseline.get(key)!=value}
    removed=set(baseline)-set(current)
    with _file_lock(path.with_suffix('.registry.lock')):
        try:latest=load(path)
        except FileNotFoundError:latest={}
        latest.update(changes)
        for key in removed:
            if latest.get(key)==baseline.get(key):latest.pop(key,None)
        atomic_json(path,latest)
        return latest
