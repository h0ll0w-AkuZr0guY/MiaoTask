"""Persistent aggregation preserves daily counting, identity and read-only data."""
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from backend.core.service.panel import Controller, make_handler
from backend.core.storage.daily_state import DailyState, local_day
from backend.core.storage.namespace import StorageNamespace
from backend.core.storage.rankings import RankingStore


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RankingStore(self.root)

    def record(self, serial='synthetic-one', app='sample', activity='daily', day=None, instance='main', **stats):
        ledger = DailyState(self.root, serial, 'uiautomator2', day=day, instance_id=instance,
                            namespace=StorageNamespace(app, activity))
        ledger.progress('synthetic-run', stats)
        return ledger

    def test_devices_instances_dates_and_activities_aggregate_without_recounting(self):
        yesterday = (date.fromisoformat(local_day()) - timedelta(days=1)).isoformat()
        a = self.record(completed=2, elapsed=100)
        self.record(day=yesterday, completed=3, elapsed=200)
        self.record(instance='clone', completed=4, elapsed=300)
        self.record(activity='weekly', completed=5, elapsed=400)
        self.record(serial='synthetic-two', completed=1, elapsed=50)
        result = self.store.snapshot()
        row = next(item for item in result['rows'] if item['device_key'] == a.device_key)
        self.assertEqual(row['stats']['completed'], 14)
        self.assertEqual(row['stats']['elapsed'], 1000)
        self.assertEqual(row['days'], 2)
        self.assertEqual(result['records'], 5)
        self.assertEqual(self.store.snapshot()['rows'], result['rows'])
        today = self.store.snapshot(period='today')['rows'][0]
        self.assertEqual(today['stats']['completed'], 11)
        activities = self.store.snapshot(group='activity', metric='completed')['rows']
        self.assertEqual(len(activities), 3)
        self.assertEqual(activities[0]['activity'], 'daily')
        self.assertEqual(activities[0]['stats']['completed'], 9)

    def test_reopen_reads_persistent_updates_and_does_not_write_counters(self):
        ledger = self.record(completed=1, elapsed=5)
        before = ledger.stats_path.read_bytes()
        self.store.snapshot()
        self.assertEqual(before, ledger.stats_path.read_bytes())
        ledger.progress('synthetic-run', {'completed': 4, 'elapsed': 9})
        reopened = RankingStore(self.root).snapshot()
        self.assertEqual(reopened['rows'][0]['stats']['completed'], 4)
        self.assertEqual(reopened['rows'][0]['stats']['elapsed'], 9)
        self.assertEqual(reopened['rows'][0]['stats']['failed'], 0)

    def test_skip_and_failure_use_ledger_counts_not_diagnostic_count(self):
        ledger = self.record(failed=2)
        ledger.progress('synthetic-run', {'failed': 2}, tasks=[{'key': 'demo', 'status': 'disabled'}])
        ledger.failure({'reason': 'synthetic security verification'})
        row = self.store.snapshot()['rows'][0]
        self.assertEqual(row['stats']['skipped'], 1)
        self.assertEqual(row['stats']['failed'], 2)

    def test_malformed_and_mismatched_records_are_retained_and_ignored(self):
        ledger = self.record()
        for content in ('{', '[]', json.dumps(dict(ledger.data, device_key='different'))):
            ledger.stats_path.write_text(content, encoding='utf-8')
            result = self.store.snapshot()
            self.assertEqual(result['rows'], [])
            self.assertEqual(result['ignored'], 1)
            self.assertEqual(ledger.stats_path.read_text(encoding='utf-8'), content)

    def test_migrated_legacy_copy_is_not_counted_twice(self):
        ledger = self.record(completed=2)
        old = self.root / 'daily' / local_day() / ledger.device_key
        old.mkdir(parents=True)
        (old / 'state.json').write_bytes(ledger.stats_path.read_bytes())
        self.assertEqual(self.store.snapshot()['rows'][0]['stats']['completed'], 2)

    def test_labels_and_anonymous_identity_without_raw_device_data(self):
        self.record()
        result = self.store.snapshot(group='activity', catalog=[dict(id='sample', label='示例应用', activities=[dict(id='daily', label='每日任务')])])
        self.assertEqual(result['rows'][0]['app_label'], '示例应用')
        self.assertEqual(result['rows'][0]['activity_label'], '每日任务')
        self.assertNotIn('synthetic-one', json.dumps(result))
        self.assertTrue(result['local_only'])

    def test_query_values_cannot_select_arbitrary_files(self):
        for kwargs in ({'group': '../daily'}, {'period': 'yesterday'}, {'metric': 'raw'}):
            with self.assertRaises(ValueError):
                self.store.snapshot(**kwargs)

    def test_apps_remain_separate_and_bad_numeric_values_do_not_poison_totals(self):
        first = self.record(app='first', completed=2, elapsed=10)
        second = self.record(app='second', completed=3, elapsed=20)
        value = json.loads(first.stats_path.read_text(encoding='utf-8'))
        value['stats'].update(elapsed=float('inf'), failed=-4, claims=True)
        first.stats_path.write_text(json.dumps(value), encoding='utf-8')
        rows = self.store.snapshot(metric='completed')['rows']
        self.assertEqual([row['app'] for row in rows], ['second', 'first'])
        self.assertEqual(rows[1]['stats']['elapsed'], 0)
        self.assertEqual(rows[1]['stats']['failed'], 0)
        self.assertEqual(rows[1]['stats']['claims'], 0)
        self.assertEqual(rows[0]['device_key'], second.device_key)

    def test_http_uses_default_private_root_and_validates_queries(self):
        self.record(completed=2)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Controller(), 'test-token'))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch('backend.core.service.panel.data_dir', return_value=self.root):
                base = f'http://127.0.0.1:{server.server_port}'
                with urlopen(base + '/api/rankings?group=activity&period=today&metric=completed', timeout=5) as response:
                    result = json.load(response)
                self.assertEqual(result['rows'][0]['stats']['completed'], 2)
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(base + '/api/rankings?group=invalid', timeout=5)
                self.assertEqual(invalid.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)
