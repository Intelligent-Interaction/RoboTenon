"""Connection-local, bounded content-addressed history transfer.

Cache serialized frames, never model-owned arrays. Each inference reconstructs
fresh observations, so in-place model preprocessing cannot poison later requests.
"""
from collections import OrderedDict
from hashlib import sha256

MAX_FRAMES = 32
MAX_BYTES = 64 * 1024 * 1024


class HistoryCache:
    def __init__(self):
        self.frames = OrderedDict()

    def clear(self):
        self.frames.clear()

    def _commit(self, updates, refs):
        candidate = self.frames.copy()
        candidate.update(updates)
        for key in refs:
            candidate.move_to_end(key)
        total = sum(len(v) for v in candidate.values())
        while len(candidate) > MAX_FRAMES or total > MAX_BYTES:
            _, data = candidate.popitem(last=False)
            total -= len(data)
        self.frames = candidate

    def prepare(self, payload):
        from .batch_transport import pack_message
        if not isinstance(payload, dict) or not isinstance(payload.get('history'), list):
            return None
        history = payload['history']
        if not history or len(history) > MAX_FRAMES:
            return None
        refs, updates = [], {}
        for observation in history:
            data = pack_message(observation)
            key = sha256(data).hexdigest()
            refs.append(key)
            if key not in self.frames:
                updates[key] = data
        current = {key: updates.get(key, self.frames.get(key)) for key in refs}
        if sum(len(data) for data in current.values()) > MAX_BYTES:
            return None
        return {'payload': {k:v for k,v in payload.items() if k != 'history'},
                'refs': refs, 'frames': updates}

    def acknowledge(self, message):
        self._commit(message['frames'], message['refs'])

    def restore(self, message):
        from .batch_transport import unpack_message
        refs, updates, payload = message['refs'], message['frames'], message['payload']
        if (not isinstance(refs, list) or not 0 < len(refs) <= MAX_FRAMES
                or not all(isinstance(k, str) for k in refs)
                or not isinstance(updates, dict) or not isinstance(payload, dict)
                or 'history' in payload or not set(updates).issubset(refs)):
            raise ValueError('invalid history cache message')
        if any(not isinstance(v, bytes) for v in updates.values()):
            raise ValueError('history cache frames must be bytes')
        if sum(map(len, updates.values())) > MAX_BYTES:
            raise ValueError('history cache exceeds byte limit')
        if any(sha256(data).hexdigest() != key for key,data in updates.items()):
            raise ValueError('history cache frame identity mismatch')
        current = {k:updates.get(k, self.frames.get(k)) for k in refs}
        if any(v is None for v in current.values()):
            raise ValueError('history cache miss; reconnect with a new session')
        if sum(map(len,current.values())) > MAX_BYTES:
            raise ValueError('history cache exceeds byte limit')
        restored = {**payload, 'history':[unpack_message(current[k]) for k in refs]}
        self._commit(updates, refs)
        return restored
