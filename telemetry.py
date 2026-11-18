from __future__ import annotations

import copy
import json
import math
import os
import re
import sys
import threading
import time
from collections import deque

PREFIX = '@quinn:event '
SOURCES = ('quinn', 'sniper')
STATUSES = {'start', 'complete', 'error', 'skipped', 'info', 'stopped'}
COUNTERS = {'collected', 'unconfirmed', 'grab_seconds', 'drop_seconds'}
LIVE = {'start', 'info'}
HISTORY_STAGE = {'quinn': 'inference', 'sniper': 'ocr'}
_write_lock = threading.Lock()
_controls = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')


def clean(value, limit=2000):
    return _controls.sub('', str(value))[:limit]


def normalize(event):
    if not isinstance(event, dict) or event.get('source') not in SOURCES:
        return None
    if not isinstance(event.get('status'), str) or event['status'] not in STATUSES:
        return None
    stage = event.get('stage', '')
    if not isinstance(stage, str) or not re.fullmatch(r'[a-z_]{1,32}', stage):
        return None
    stamp = event.get('time', time.time())
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or abs(stamp) > 1e12 or not math.isfinite(stamp):
        return None
    metrics = event.get('metrics', {})
    if not isinstance(metrics, dict):
        return None
    values = {}
    for key, value in list(metrics.items())[:40]:
        if not isinstance(key, str) or not re.fullmatch(r'[a-z_]{1,48}', key):
            continue
        if isinstance(value, str):
            values[key] = clean(value)
        elif value is None or isinstance(value, bool):
            values[key] = value
        elif isinstance(value, (int, float)) and abs(value) <= 1e100 and math.isfinite(value):
            values[key] = value
    return dict(source=event['source'], stage=stage, status=event['status'],
                detail=clean(event.get('detail', '')), time=float(stamp),
                trace=clean(event.get('trace') or '', 120), metrics=values)


def emit(source, stage, status='info', detail='', trace=None, **metrics):
    if os.environ.get('QUINN_TELEMETRY') != '1':
        return
    try:
        event = normalize(dict(source=source, stage=stage, status=status, detail=detail,
                               trace=trace, metrics=metrics, time=time.time()))
        if event:
            line = PREFIX + json.dumps(event, ensure_ascii=True, allow_nan=False) + '\n'
            with _write_lock:
                sys.stdout.write(line)
                sys.stdout.flush()
    except Exception:
        pass


def _pipeline():
    return dict(events=deque(maxlen=240), samples=deque(maxlen=240), stages={}, metrics={}, globals={}, updated=0,
                revision=0, trace='', traces={}, stopped=False)


class TelemetryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._sources = {name: _pipeline() for name in SOURCES}
        self._revision = 0

    def ingest(self, line):
        if not isinstance(line, str) or not line.startswith(PREFIX):
            return False
        try:
            if len(line) <= 100_000:
                self.record(json.loads(line[len(PREFIX):]))
        except (ValueError, TypeError, RecursionError):
            pass
        return True

    def record(self, event):
        event = normalize(event)
        if event is None:
            return False
        with self._lock:
            data = self._sources[event['source']]
            self._revision += 1
            event['sequence'] = self._revision
            data['revision'] = self._revision
            data['updated'] = event['time']
            data['events'].append(event)
            counters = {key: value for key, value in event['metrics'].items() if key in COUNTERS}
            data['globals'].update(counters)
            data['metrics'].update(counters)
            trace = event['trace']
            stage = event['stage']
            if trace:
                if trace not in data['traces']:
                    data['traces'][trace] = dict(stages={}, metrics={}, samples=deque(maxlen=240))
                    if len(data['traces']) > 24:
                        history_stage = HISTORY_STAGE[event['source']]
                        candidates = [(key, run) for key, run in data['traces'].items() if key not in (trace, data['trace'])]
                        oldest, _ = min(candidates, key=lambda pair: history_stage in pair[1]['stages'])
                        del data['traces'][oldest]
                run = data['traces'][trace]
                previous = run['stages'].get(stage)
                history = stage == HISTORY_STAGE[event['source']]
                if previous and 'started' in previous and (history or event['status'] != 'start'):
                    event['started'] = previous['started']
                elif event['status'] == 'start':
                    event['started'] = event['time']
                if previous and history:
                    if previous['status'] == 'stopped' or (previous['status'] not in LIVE and event['status'] in LIVE):
                        return True
                run['stages'][stage] = event
                run['metrics'].update({key: value for key, value in event['metrics'].items() if key not in COUNTERS})
                if (stage == 'inference' and 'output_chars' in event['metrics']) or (stage == 'ocr' and 'pass_ms' in event['metrics']):
                    run['samples'].append(event)
                focus = (event['source'] == 'quinn' and stage == 'context' and event['status'] == 'start') or (
                    event['source'] == 'sniper' and stage in ('intake', 'download') and event['status'] == 'start')
                if not data['trace'] or focus:
                    data['trace'] = trace
                    data['stopped'] = False
                if data['trace'] == trace:
                    data['stages'] = run['stages']
                    data['samples'] = run['samples']
                    data['metrics'] = {**data['globals'], **run['metrics']}
            else:
                data['globals'].update(event['metrics'])
                data['metrics'].update(event['metrics'])
                if stage not in ('cooldown', 'connection', 'backend'):
                    data['stages'][stage] = event
        return True

    def snapshot(self, source=None, *, since=None):
        with self._lock:
            if since == self._revision:
                return None
            result = copy.deepcopy(self._sources)
        for data in result.values():
            data['events'] = list(data['events'])
            data['samples'] = list(data['samples'])
        return result[source] if source else result

    def reset(self, source=None):
        with self._lock:
            for name in (source,) if source else SOURCES:
                self._revision += 1
                self._sources[name] = _pipeline()
                self._sources[name]['revision'] = self._revision

    def mark_stopped(self, source, detail='Stopped'):
        with self._lock:
            data = self._sources[source]
            self._revision += 1
            data['revision'] = self._revision
            data['stopped'] = True
            stopped_at = time.time()
            stages_by_run = [run['stages'] for run in data['traces'].values()]
            for stages in [data['stages'], *stages_by_run]:
                for stage, event in list(stages.items()):
                    if event['status'] in LIVE:
                        stages[stage] = {**event, 'status': 'stopped', 'detail': detail, 'stopped_at': stopped_at}
