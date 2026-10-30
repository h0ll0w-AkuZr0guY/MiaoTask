"""Optional informational text never changes activity execution or registration."""
import json
from pathlib import Path
import tempfile
import unittest

from backend.core.registry import Registry
from test_activity_pipeline import write_registry


class ActivityDescriptionTests(unittest.TestCase):
    def test_missing_blank_and_non_text_descriptions_are_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);registry=write_registry(root)
            path=root/'alpha/activities/first/config.json'
            for value in (None,'', '  ', {'html':'<b>unsupported</b>'}):
                config=json.loads(path.read_text(encoding='utf-8'))
                config['description']=value
                path.write_text(json.dumps(config),encoding='utf-8')
                row=registry.catalog()[0]['activities'][0]
                self.assertNotIn('description',row)

    def test_multiline_description_is_plain_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);registry=write_registry(root)
            path=root/'alpha/activities/first/config.json'
            config=json.loads(path.read_text(encoding='utf-8'))
            description='🎁 活动介绍\n\n使用方法：\n<script>plain text</script>'
            config['description']=description
            path.write_text(json.dumps(config),encoding='utf-8')
            row=registry.catalog()[0]['activities'][0]
            self.assertEqual(row['description'],description)

    def test_only_requested_activity_has_provided_description(self):
        registry=Registry(Path(__file__).resolve().parents[1]/'backend/app').discover()
        described=[(app['id'],activity['id'],activity['description']) for app in registry.catalog()
                   for activity in app['activities'] if activity.get('description')]
        self.assertEqual([(a,b) for a,b,_ in described],[('zfb','yueyuezhuan')])
        self.assertIn('这是一款转账红包（非支付减免红包）',described[0][2])
        self.assertIn('积少成多',described[0][2])
