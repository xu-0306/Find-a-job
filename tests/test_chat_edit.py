"""Edit and regenerate an existing pair without changing subsequent messages."""
import json
import unittest

import httpx

from app.store import encode
import test_m2_chat as chats


class EditChecks(unittest.TestCase):
    setUp = chats.ChatChecks.setUp
    tearDown = chats.ChatChecks.tearDown
    send = chats.ChatChecks.send
    history = chats.ChatChecks.history

    def seed(self, ident='original', attachments=None):
        self.store.start_chat(ident, '原始訊息', attachments=attachments)
        self.store.interrupt_reply(ident)
        return self.history()[-2]

    def edit(self, original, request_id='edited', text='修改後的訊息', **extra):
        return self.client.post('/api/chat', json={'request_id': request_id, 'text': text,
            'edit_message_id': original['id'], 'edit_request_id': original['request_id'], **extra})

    def rows(self):
        with self.store.connect() as db:
            return [tuple(row) for row in db.execute('SELECT rowid,* FROM messages ORDER BY rowid')]

    def test_middle_edit_preserves_ids_future_messages_and_uses_only_prior_context(self):
        self.app.state.llm_transport = httpx.MockTransport(lambda request: chats.response(text='原回覆'))
        self.send('prior', '前面的提問')
        self.send('original', '原本的提問')
        self.send('future', '後面的提問')
        before, original = self.rows(), self.history()[2]
        payloads = []
        self.app.state.llm_transport = httpx.MockTransport(lambda request:
            payloads.append(json.loads(request.content)) or chats.response(text='重新生成的回覆'))
        result = self.edit(original)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(chats.events(result)[-1][1]['status'], 'completed')
        after, history = self.rows(), self.history()
        self.assertEqual(len(after), len(before))
        self.assertEqual([row[:2] for row in before], [row[:2] for row in after])
        self.assertEqual(before[:2] + before[4:], after[:2] + after[4:])
        self.assertEqual(history[2]['content']['text'], '修改後的訊息')
        self.assertEqual(history[3]['content']['text'], '重新生成的回覆')
        self.assertEqual(history[2]['created_at'], original['created_at'])
        self.assertIn('edited_at', history[2]['content'])
        self.assertEqual([item['request_id'] for item in history[2:4]], ['edited', 'edited'])
        inputs = encode(payloads[0]['input'])
        self.assertIn('前面的提問', inputs)
        self.assertIn('修改後的訊息', inputs)
        self.assertNotIn('後面的提問', inputs)
        self.assertNotIn('原本的提問', inputs)
        self.assertEqual(self.client.get('/api/chat/original').status_code, 404)
        progress = self.client.get('/api/chat/edited').json()
        self.assertEqual([item['id'] for item in progress['items']], [history[2]['id'], history[3]['id']])

    def test_failed_message_can_regenerate_in_place_and_duplicate_is_not_reexecuted(self):
        original = self.seed()
        calls = []
        self.app.state.llm_transport = httpx.MockTransport(lambda request: calls.append(request) or chats.response(text='恢復成功'))
        result = self.edit(original, text='原始訊息')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(self.edit(original).json()['error']['code'], 'duplicate_request')
        self.assertEqual(self.edit(original, request_id='stale').json()['error']['code'], 'message_changed')
        self.assertEqual(len(calls), 1)

    def test_wrong_role_conversation_and_missing_version_do_not_mutate(self):
        original = self.seed()
        before = self.rows()
        self.assertEqual(self.edit(self.history()[-1]).status_code, 404)
        other = self.client.post('/api/conversations', json={'title': '另一段'}).json()['id']
        self.assertEqual(self.edit(original, conversation_id=other).status_code, 404)
        self.assertEqual(self.client.post('/api/chat', json={'request_id': 'missing-version', 'text': 'x',
            'edit_message_id': original['id']}).status_code, 422)
        self.assertEqual(self.edit(original, request_id='blank', text='  ').status_code, 422)
        self.assertEqual(self.rows(), before)

    def test_original_attachment_must_be_reselected_and_multipart_can_replace_it(self):
        original = self.seed(attachments=[{'filename': 'original.md', 'kind': 'text', 'characters': 3}])
        before = self.rows()
        self.assertEqual(self.edit(original).json()['error']['code'], 'attachment_required')
        self.assertEqual(self.rows(), before)
        self.app.state.llm_transport = httpx.MockTransport(lambda request: chats.response(text='已重讀附件'))
        result = self.client.post('/api/chat', data={'text': '修改後的訊息', 'request_id': 'edited',
            'conversation_id': 'legacy', 'edit_message_id': original['id'], 'edit_request_id': 'original'},
            files={'resume': ('replacement.md', '重新附上的合成文檔'.encode(), 'text/markdown')})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(self.history()[0]['content']['attachments'][0]['filename'], 'replacement.md')
        self.assertNotIn('重新附上的合成文檔', encode(self.history()))

    def test_failed_pair_update_rolls_back_original_text_and_request_ids(self):
        original = self.seed()
        before = self.rows()
        self.app.state.llm_transport = httpx.MockTransport(lambda request: chats.response(text='不應執行'))
        with self.store.connect(write=True) as db:
            db.execute("CREATE TRIGGER edit_fault BEFORE UPDATE ON messages WHEN OLD.role='assistant' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        result = self.edit(original)
        self.assertEqual(result.status_code, 503)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.app.state.active_chats, {})


if __name__ == '__main__':
    unittest.main()
