from __future__ import annotations

import unittest

from envserver.semantic.data_handles import DataHandleStore
from envserver.semantic.protocol import ErrorCode, ProtocolError


class DataHandleStoreTests(unittest.TestCase):
    def test_handles_are_opaque_bounded_and_expire(self) -> None:
        now = [0.0]
        store = DataHandleStore(
            ttl_seconds=10,
            max_handles=2,
            max_records_per_handle=3,
            clock=lambda: now[0],
            token_factory=iter(["one", "two", "three"]).__next__,
        )
        record = store.create("research.results", [{"value": 1}])
        self.assertEqual(record.handle, "data_one")
        self.assertEqual(store.get(record.handle).records[0]["value"], 1)
        now[0] = 11
        with self.assertRaises(ProtocolError) as expired:
            store.get(record.handle)
        self.assertEqual(expired.exception.code, ErrorCode.NOT_FOUND)
        with self.assertRaises(ProtocolError) as over:
            store.create("x", [{}, {}, {}, {}])
        self.assertEqual(over.exception.code, ErrorCode.BUDGET_EXHAUSTED)


if __name__ == "__main__":
    unittest.main()
