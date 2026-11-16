import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from delivery import DeliveryError, deliver_weekly, load_state, week_key


class WeeklyDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.json'
        self.now = datetime(2026, 11, 16, 6, tzinfo=timezone.utc)

    def sender(self, messages, on_delivered, on_uncertain):
        for index, _ in enumerate(messages):
            on_delivered(index)
        return len(messages)

    def test_backup_does_not_regenerate_or_resend_completed_week(self):
        factory = Mock(return_value=['header', 'paper 1', 'paper 2'])
        sender = Mock(side_effect=self.sender)
        self.assertEqual(deliver_weekly(factory, sender, self.path, self.now), 3)
        self.assertEqual(deliver_weekly(factory, sender, self.path, self.now), 0)
        factory.assert_called_once()
        sender.assert_called_once()
        self.assertTrue(load_state(self.path)['completed'])

    def test_failed_run_resumes_only_unconfirmed_messages(self):
        factory = Mock(return_value=['header', 'paper 1', 'paper 2'])
        def fail(messages, on_delivered, on_uncertain):
            on_delivered(0)
            raise RuntimeError('known rejected message')
        with self.assertRaises(RuntimeError):
            deliver_weekly(factory, fail, self.path, self.now)
        sender = Mock(side_effect=self.sender)
        self.assertEqual(deliver_weekly(factory, sender, self.path, self.now), 2)
        self.assertEqual(sender.call_args.args[0], ['paper 1', 'paper 2'])
        factory.assert_called_once()

    def test_uncertain_delivery_is_not_automatically_duplicated(self):
        def timeout(messages, on_delivered, on_uncertain):
            on_uncertain(0)
            raise RuntimeError('delivery uncertain')
        factory = Mock(return_value=['paper'])
        with self.assertRaises(RuntimeError):
            deliver_weekly(factory, timeout, self.path, self.now)
        sender = Mock()
        with self.assertRaisesRegex(DeliveryError, 'uncertain'):
            deliver_weekly(factory, sender, self.path, self.now)
        sender.assert_not_called()
        factory.assert_called_once()

    def test_next_week_creates_new_digest(self):
        factory = Mock(return_value=['paper'])
        deliver_weekly(factory, self.sender, self.path, self.now)
        following = datetime(2026, 11, 23, 6, tzinfo=timezone.utc)
        self.assertEqual(deliver_weekly(factory, self.sender, self.path, following), 1)
        self.assertEqual(factory.call_count, 2)

    def test_moscow_week_boundary(self):
        self.assertEqual(week_key(datetime(2026, 11, 15, 21, tzinfo=timezone.utc)), '2026-11-16')
        self.assertEqual(week_key(datetime(2026, 11, 15, 20, 59, tzinfo=timezone.utc)), '2026-11-09')

    def test_all_receipts_saved_before_crash_avoids_resend(self):
        def interrupted(messages, on_delivered, on_uncertain):
            on_delivered(0)
            raise RuntimeError('crash after confirmed delivery')
        with self.assertRaises(RuntimeError):
            deliver_weekly(lambda: ['paper'], interrupted, self.path, self.now)
        sender = Mock()
        self.assertEqual(deliver_weekly(Mock(), sender, self.path, self.now), 0)
        sender.assert_not_called()
        self.assertTrue(load_state(self.path)['completed'])

    def test_corrupt_state_fails_without_sending(self):
        self.path.write_text('{broken')
        factory = Mock()
        sender = Mock()
        with self.assertRaises(DeliveryError):
            deliver_weekly(factory, sender, self.path, self.now)
        factory.assert_not_called()
        sender.assert_not_called()

    def test_explicit_force_allows_intentional_repeat(self):
        factory = Mock(return_value=['paper'])
        deliver_weekly(factory, self.sender, self.path, self.now)
        self.assertEqual(deliver_weekly(factory, self.sender, self.path, self.now, force=True), 1)
        self.assertEqual(factory.call_count, 2)
