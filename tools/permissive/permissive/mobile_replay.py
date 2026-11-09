"""Lossless text coalescing for Android's initial history replay."""
import bisect
import json


def compact_events(rows):
    events, deltas, turns, timing = [], {}, {}, {}
    for row in rows:
        event = {'id': row['id'], 'taskId': row['task_id'], 'kind': row['kind'],
                 'data': json.loads(row['data']), 'createdAt': row['created_at']}
        data = event['data']
        if event['kind'] == 'inference' and isinstance(data, dict) and data.get('phase') == 'output':
            key = (event['taskId'], data.get('requestId'), int(data.get('timestamp', 0) / 10))
            previous = timing.get(key)
            if previous is not None:
                previous['data']['estimatedTokens'] += data.get('estimatedTokens', 0)
                continue
            data['estimatedTokens'] = data.get('estimatedTokens', 0)
            timing[key] = event
        if event['kind'] == 'agent' and isinstance(data, dict):
            method, params = data.get('method', ''), data.get('params', {})
            if method == 'turn/started':
                turns[event['taskId']] = params.get('turn', {}).get('id', '')
            if method.startswith('item/') and isinstance(params.get('delta'), str):
                key = (event['taskId'], params.get('turnId') or turns.get(event['taskId'], ''),
                       params.get('itemId'), method, params.get('contentIndex'), params.get('summaryIndex'))
                if key in deltas:
                    deltas[key][1].append(params['delta'])
                    continue
                deltas[key] = (event, [params['delta']])
        events.append(event)
    for event, parts in deltas.values():
        event['data']['params']['delta'] = ''.join(parts)
    return events


class MobileReplay:
    def __init__(self):
        self.cache = {}

    def clear(self):
        self.cache.clear()

    def page(self, store, ident, after, through, limit=200, segment_id=None):
        root = store.db.execute('SELECT id FROM tasks WHERE conversation_id=? ORDER BY rowid LIMIT 1', (ident,)).fetchone()
        if root is None:
            raise KeyError(ident)
        info = store.segments.info(ident, segment_id)
        upper = store.segments.cursor(info) if info else store.db.execute('SELECT coalesce(max(e.id),0) FROM events e JOIN tasks t ON t.id=e.task_id WHERE t.conversation_id=?', (ident,)).fetchone()[0]
        through = min(upper, through) if through is not None else upper
        if after < 0 or through < after:
            raise ValueError('Invalid replay cursor')
        key = (ident, info['id'] if info else root[0], through)
        if key not in self.cache:
            rows = store.event_rows(ident, through=through, segment_id=segment_id)
            events = compact_events(rows)
            if len(self.cache) >= 2:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = ([event['id'] for event in events], events)
        ids, events = self.cache[key]
        start = bisect.bisect_right(ids, after)
        page = events[start:start + limit]
        cursor = page[-1]['id'] if start + len(page) < len(events) else through
        return {'data': page, 'nextCursor': cursor, 'view': 'mobile'}
