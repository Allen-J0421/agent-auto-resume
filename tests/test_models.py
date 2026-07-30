import time
import unittest

from agent_resume.models import (
    QuotaWindow,
    STATUS_REJECTED,
    WINDOW_FIVE_HOUR,
    WINDOW_UNKNOWN,
    WINDOW_WEEKLY,
    classify_duration,
    latest_reset,
    merge_windows,
)


class ModelTests(unittest.TestCase):
    def test_duration_classification(self):
        self.assertEqual(classify_duration(300), WINDOW_FIVE_HOUR)
        self.assertEqual(classify_duration(10080), WINDOW_WEEKLY)
        self.assertEqual(classify_duration(90), WINDOW_UNKNOWN)
        self.assertEqual(classify_duration(None), WINDOW_UNKNOWN)

    def test_threshold_and_rejected_windows(self):
        now = 1_800_000_000
        warning = QuotaWindow("weekly", 98, now + 10)
        self.assertTrue(warning.blocks(98, now))
        self.assertFalse(warning.blocks(99, now))
        rejected = QuotaWindow("weekly", 1, now + 10, STATUS_REJECTED)
        self.assertTrue(rejected.blocks(99, now))
        stale = QuotaWindow("weekly", 100, now - 1, STATUS_REJECTED)
        self.assertFalse(stale.blocks(1, now))

    def test_merge_keeps_distinct_unknown_durations(self):
        windows = merge_windows(
            [],
            [
                QuotaWindow(
                    "unknown", 10, 20, limit_id="shared", duration_minutes=60
                ),
                QuotaWindow(
                    "unknown", 20, 30, limit_id="shared", duration_minutes=120
                ),
            ],
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(latest_reset(windows), 30)

    def test_merge_preserves_sparse_reset(self):
        old = QuotaWindow("weekly", 20, 300, limit_id="x")
        new = QuotaWindow("weekly", 30, None, limit_id="x")
        self.assertEqual(merge_windows([old], [new])[0].resets_at, 300)

    def test_stale_observation_cannot_replace_newer_usage(self):
        new = QuotaWindow("weekly", 80, 300, observed_at=200, limit_id="x")
        stale = QuotaWindow("weekly", 10, 400, observed_at=100, limit_id="x")
        merged = merge_windows([new], [stale])
        self.assertEqual(merged[0].used_percent, 80)
        self.assertEqual(merged[0].resets_at, 300)

    def test_observation_timestamp_is_current(self):
        window = QuotaWindow("unknown", 0, None)
        self.assertLessEqual(abs(window.observed_at - int(time.time())), 1)
