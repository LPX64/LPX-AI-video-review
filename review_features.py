"""Pure validation for review edits and chronological export."""
from pathlib import Path
from datetime import datetime, timezone
import math


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Data musi zawierać strefę czasową, np. +02:00.')
    return parsed.timestamp()


def ordered(state, candidates):
    files = {f['id']: f for f in state['files']}
    times = state.get('recording_times', {})
    def key(c):
        f = files[c['file_id']]
        value = times.get(f['id']) or f.get('recorded_at')
        return (0, timestamp(value) + c['start'], f['path']) if value else (1, f['path'], c['start'])
    return sorted(candidates, key=key)


def validate_ranges(state, candidates):
    files = {f['id']: f for f in state['files']}
    grouped = {}
    for c in candidates:
        f = files[c['file_id']]
        if not all(math.isfinite(c[k]) for k in ('start', 'end')) or not 0 <= c['start'] < c['end'] <= f['duration'] + .001:
            raise ValueError('Nieprawidłowe granice klipu: ' + c['name'])
        grouped.setdefault(str(Path(f['path']).resolve()).casefold(), []).append(c)
    for group in grouped.values():
        group.sort(key=lambda c: c['start'])
        for a, b in zip(group, group[1:]):
            if a['end'] > b['start'] + .000001:
                raise ValueError('Propozycje zachodzą na siebie. Granica klipu nie może wejść w inne ujęcie.')


def changed_range(state, candidate_id, edge, delta):
    if edge not in ('start', 'end') or delta not in (-.5, .5):
        raise ValueError('Dozwolona korekta to 0,5 s początku lub końca.')
    candidates = [dict(c) for c in state['candidates']]
    candidate = next(c for c in candidates if c['id'] == candidate_id)
    candidate[edge] = round(candidate[edge] + delta, 3)
    if candidate['end'] - candidate['start'] < .5:
        raise ValueError('Klip musi mieć co najmniej 0,5 s.')
    validate_ranges(state, candidates)
    return candidate
