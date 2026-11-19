"""Incremental logical identity for finalized typed cell values."""

import hashlib
import json
class SortedIdDigest:
    """Hash compact canonical JSON IDs in their existing sorted index pass."""

    def __init__(self):
        self._digest = hashlib.sha256(b'[')
        self._previous = None
        self.count = 0

    def add(self, value):
        if not isinstance(value, str) or self._previous is not None and value <= self._previous:
            raise ValueError("Cell identity summaries require sorted unique string IDs")
        if self.count:
            self._digest.update(b',')
        self._digest.update(json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        self._previous = value
        self.count += 1

    def hexdigest(self):
        result = self._digest.copy()
        result.update(b']')
        return result.hexdigest()


class SortedIdMappingDigest:
    """Hash the current sorted cell-ID to domain mapping without retaining it."""

    def __init__(self):
        self._digest = hashlib.sha256(b'{')
        self._previous = None
        self.count = 0

    def add(self, cell_id, domain_id):
        if not isinstance(cell_id, str) or not isinstance(domain_id, str) or self._previous is not None and cell_id <= self._previous:
            raise ValueError("Cell membership summaries require sorted unique string IDs and domains")
        if self.count:
            self._digest.update(b',')
        self._digest.update(json.dumps(cell_id, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        self._digest.update(b':')
        self._digest.update(json.dumps(domain_id, ensure_ascii=True, separators=(',', ':')).encode('utf-8'))
        self._previous = cell_id
        self.count += 1

    def hexdigest(self):
        result = self._digest.copy()
        result.update(b'}')
        return result.hexdigest()
