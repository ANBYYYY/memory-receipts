"""Migration and durable-worker regressions using isolated fixtures."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import test_memory
from memory import atomic, encoded, read
from organizer import Organizer


class AutomationTests(test_memory.Tests):
    def user(self, text="执行正常待办"):
        self.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})
        self.mem.sync()

    def test_increment_consumed_once_and_no_decision_changed(self):
        self.user()
        before = self.mem.head()
        result = Organizer(self.mem).run()
        self.assertEqual(result['status'], 'consumed_into_review_queue')
        self.assertEqual(Organizer(self.mem).run()['status'], 'no_increment')
        self.assertEqual(self.mem.head(), before)
        self.assertEqual(result['model_calls'], 0)
        draft = read(result['review_path'])
        self.assertEqual(draft['task_candidates'][0]['instruction_text'], '执行正常待办')
        self.assertEqual(self.mem.check()['pending_public_count'], 1)

    def test_restart_retries_saved_draft_without_duplicate_artifact(self):
        self.user()
        original = atomic
        def fail(path, data):
            if Path(path).parent.name == 'review':
                raise OSError('queue write interrupted')
            original(path, data)
        with patch('organizer.atomic', fail):
            with self.assertRaises(OSError):
                Organizer(self.mem).run()
        result = Organizer(self.mem).run()
        self.assertEqual(result['status'], 'consumed_into_review_queue')
        self.assertEqual(len(list((self.mem.base / 'organization/jobs').glob('*.json'))), 1)
        self.assertEqual(len(list((self.mem.base / 'organization/review').glob('*.json'))), 1)

    def test_late_saved_draft_stays_stale(self):
        self.user()
        def fail(path, data):
            if Path(path).parent.name == 'review':
                raise OSError('pause after draft')
            atomic(path, data)
        with patch('organizer.atomic', fail):
            with self.assertRaises(OSError):
                Organizer(self.mem).run()
        state = self.mem.head()[1]
        state['decisions'].append({'id':'D2','text':'主会话的新决定','source':'new-user'})
        self.mem.publish(state, 1, test_memory.PUBLISHER)
        self.assertEqual(Organizer(self.mem).run()['status'], 'stale_candidate_kept')
        self.assertEqual(self.mem.head()[1]['decisions'][-1]['id'], 'D2')
        self.assertEqual(len(list((self.mem.base / 'organization/review').glob('*.json'))), 0)

    def test_disable_stops_receipt_and_short_view_declares_incomplete(self):
        self.user()
        atomic(self.mem.base / 'automation.json', encoded({'enabled':False}))
        before = read(self.mem.base / 'last-sync.json')
        self.assertEqual(self.mem.hook({'session_id':'fixture','cwd':str(self.root),'hook_event_name':'SessionStart','source':'compact'}), {})
        self.assertEqual(before, read(self.mem.base / 'last-sync.json'))
        self.assertIn('内容不完整', self.mem.short_view())
        self.assertIn('baseline=', self.mem.short_view())

    def test_explicit_alias_mapping_preserves_source_identity(self):
        old = self.root / 'retired-alias'
        atomic(self.mem.base / 'path-resolution.json', encoded([{'old_prefix':str(old),'physical_prefix':str(self.root)}]))
        config = self.mem.config()
        config['sources'][0]['cwd'] = str(old)
        atomic(self.mem.base / 'sources.json', encoded(config))
        self.user()
        self.assertEqual(self.mem.sync()['sources'][0]['events'], 0)
        self.assertEqual(len(self.mem.events()), 1)


if __name__ == '__main__':
    import unittest
    # print which branch of scratch_root() was taken, so a run is checkable
    print('fixture scratch dir = %s' % test_memory.scratch_root())
    unittest.main()
