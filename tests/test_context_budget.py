"""Context budget and compaction with synthetic providers and temporary databases only."""
import json
import unittest

import httpx

from app import context_budget
from app.chat import SUMMARY_PREFIX, SUMMARY_SYSTEM
from app.llm_wire import decode_event
from app.store import encode
import test_m2_chat as chats

SMALL_WINDOW = 32_000
SUMMARY_TEXT = '使用者要找台南的軟體職缺，已排除工研院。'


def tool_round(index, ids):
    call = {'id': f'call-{index}', 'name': 'search_jobs', 'arguments': '{}'}
    return [{'role': 'assistant', 'content': '', 'tool_calls': [call]},
            {'role': 'tool', 'tool_call_id': call['id'], 'content': encode({'items': [{'id': ident, 'title': '職缺' * 500} for ident in ids]})}]


class BudgetUnits(unittest.TestCase):
    def test_estimate_counts_wide_characters_and_images(self):
        self.assertEqual(context_budget.text_tokens('職缺'), 2)
        self.assertEqual(context_budget.text_tokens('abcdefgh'), 2)
        image = {'role': 'user', 'content': [{'type': 'image', 'media_type': 'image/png', 'data': 'x' * 100000}, {'type': 'text', 'text': '圖'}]}
        self.assertEqual(context_budget.message_tokens(image), context_budget.IMAGE_TOKENS + 1 + context_budget.MESSAGE_OVERHEAD)

    def test_budget_uses_configured_window_or_default(self):
        self.assertEqual(context_budget.window({}), context_budget.DEFAULT_WINDOW)
        self.assertEqual(context_budget.window({'llm_context_window': 0}), context_budget.DEFAULT_WINDOW)
        self.assertLess(context_budget.budget({'llm_context_window': SMALL_WINDOW}), SMALL_WINDOW)
        self.assertLess(context_budget.budget({}, context_budget.TARGET_RATIO), context_budget.budget({}))

    def test_masking_keeps_recent_rounds_reasoning_and_job_ids(self):
        messages = [{'role': 'system', 'content': '系統'}, {'role': 'user', 'content': '找工作'}]
        for index in range(4):
            messages += tool_round(index, [f'job-{index}a', f'job-{index}b'])
        before = context_budget.estimate(messages)
        self.assertEqual(context_budget.mask_tool_results(messages), 2)
        tools = [message for message in messages if message['role'] == 'tool']
        self.assertTrue(all(message['content'].startswith(context_budget.MASKED_PREFIX) for message in tools[:2]))
        self.assertIn('job-0a、job-0b', tools[0]['content'])
        self.assertIn('search_jobs', tools[0]['content'])
        self.assertEqual([json.loads(message['content'])['items'][0]['id'] for message in tools[2:]], ['job-2a', 'job-3a'])
        self.assertTrue(all(message['tool_calls'] for message in messages if message['role'] == 'assistant'))
        self.assertLess(context_budget.estimate(messages), before)
        self.assertEqual(context_budget.mask_tool_results(messages), 0, 'masking is idempotent')
        self.assertEqual(context_budget.mask_tool_results(messages, keep_rounds=0), 2)

    def test_trimming_drops_oldest_history_only(self):
        messages = [{'role': 'system', 'content': '系統'}]
        for index in range(10):
            messages += [{'role': 'user', 'content': f'Q{index} ' + '問' * 2000}, {'role': 'assistant', 'content': f'A{index} ' + '答' * 2000}]
        messages.append({'role': 'user', 'content': '現在的問題'})
        dropped = context_budget.trim_history(messages, 1, 21, 9_000)
        self.assertEqual(dropped[0]['content'][:2], 'Q0')
        self.assertEqual(messages[0]['content'], '系統'); self.assertEqual(messages[-1]['content'], '現在的問題')
        self.assertEqual(messages[1]['role'], 'user', 'history never starts with an orphaned assistant reply')
        self.assertLessEqual(context_budget.estimate(messages), 9_000)

    def test_overflow_is_a_distinct_stream_error(self):
        anthropic = decode_event('anthropic_messages', {'type': 'message_delta', 'delta': {'stop_reason': 'model_context_window_exceeded'}}, {})
        self.assertEqual(anthropic[0]['code'], 'context_overflow')
        failed = decode_event('responses', {'type': 'response.failed', 'response': {'error': {'code': 'context_length_exceeded'}}}, {})
        self.assertEqual(failed[0]['code'], 'context_overflow')
        other = decode_event('responses', {'type': 'response.failed', 'response': {'error': {'code': 'server_error'}}}, {})
        self.assertEqual(other[0]['code'], 'incomplete')


class CompactionChecks(unittest.TestCase):
    setUp = chats.ChatChecks.setUp
    tearDown = chats.ChatChecks.tearDown

    def configure(self, window=SMALL_WINDOW):
        with self.store.connect(write=True) as db:
            self.store.put(db, 'services', chats.SETTINGS | {'llm_context_window': window})

    def seed(self, pairs, size=300):
        for index in range(pairs):
            request = f'seed-{index}'
            self.store.start_chat(request, f'Q{index}：' + '問' * size)
            self.store.save_reply(request, {'text': f'A{index}：' + '答' * size, 'events': [], 'status': 'completed'})

    def provider(self, reply=lambda payload: chats.response(text='好的')):
        self.summary_calls, self.reply_calls = [], []

        def handle(request):
            payload = json.loads(request.content)
            if payload.get('instructions') == SUMMARY_SYSTEM:
                self.summary_calls.append(payload)
                return chats.response(text=SUMMARY_TEXT)
            self.reply_calls.append(payload)
            return reply(payload)

        self.app.state.llm_transport = httpx.MockTransport(handle)

    @staticmethod
    def texts(payload):
        return [item['content'] for item in payload['input'] if isinstance(item.get('content'), str)]

    def test_long_history_is_summarised_once_and_reused(self):
        self.configure()
        self.seed(40)
        self.provider()
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'turn-1', 'text': '繼續幫我找'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.summary_calls), 1)
        summarised = json.loads(self.summary_calls[0]['input'][0]['content'])['messages']
        texts = self.texts(self.reply_calls[0])
        self.assertEqual(texts[0], SUMMARY_PREFIX + SUMMARY_TEXT, 'the summary replaces the dropped history at the front')
        # The summary covers exactly the rows just before the first history message still sent verbatim.
        newest = int(summarised[-1]['text'][1:summarised[-1]['text'].index('：')])
        self.assertTrue(summarised[-1]['text'].startswith(f'A{newest}：'))
        self.assertTrue(texts[1].startswith(f'Q{newest + 1}：'), texts[1][:10])
        self.assertFalse(any(text.startswith('Q0：') for text in texts), 'the oldest history is no longer sent')
        self.assertTrue(any(text.startswith('A39：') for text in texts), 'the newest history is kept verbatim')
        self.assertEqual(texts[-1], '繼續幫我找')
        sent = [{'role': item.get('role', 'user'), 'content': item['content']} for item in self.reply_calls[0]['input']]
        self.assertLessEqual(context_budget.estimate(sent), context_budget.budget({'llm_context_window': SMALL_WINDOW}))
        stored = next(item for item in self.client.get('/api/messages').json()['items'] if item['request_id'] == 'turn-1')
        self.assertEqual(stored['content']['context_summary']['text'], SUMMARY_TEXT)
        # The next turn starts from the saved summary instead of asking the model again.
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'turn-2', 'text': '再一個問題'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.summary_calls), 1)
        self.assertEqual(self.texts(self.reply_calls[1])[0], SUMMARY_PREFIX + SUMMARY_TEXT)

    def test_summary_is_still_used_after_its_rows_leave_the_loaded_history(self):
        self.configure()
        self.seed(40)
        self.provider()
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-1', 'text': '繼續幫我找'}))
        self.assertEqual(len(self.summary_calls), 1)
        # 220 short rows push both the summary's message and the rows it covers out of the last 200.
        for index in range(110):
            self.store.start_chat(f'short-{index}', f'短{index}')
            self.store.save_reply(f'short-{index}', {'text': f'好{index}', 'events': [], 'status': 'completed'})
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-2', 'text': '還記得條件嗎'}))
        self.assertEqual(len(self.summary_calls), 1)
        texts = self.texts(self.reply_calls[-1])
        self.assertEqual(texts[0], SUMMARY_PREFIX + SUMMARY_TEXT)
        self.assertFalse(any(text.startswith('A39：') for text in texts), 'rows the summary covers are not resent')
        self.assertTrue(any(text == '短109' for text in texts))

    def test_editing_a_summarised_message_drops_the_stale_summary(self):
        self.configure()
        self.seed(40)
        self.provider()
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-1', 'text': '繼續幫我找'}))
        self.assertEqual(len(self.summary_calls), 1)
        original = next(item for item in self.client.get('/api/messages').json()['items'] if item['request_id'] == 'seed-38')
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'edited', 'text': 'Q38：改成只找台南',
            'conversation_id': 'legacy', 'edit_message_id': original['id'], 'edit_request_id': 'seed-38'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        with self.store.connect() as db:
            edited = db.execute("SELECT rowid FROM messages WHERE id=?", (original['id'],)).fetchone()[0]
            summaries = db.execute("""SELECT m.request_id, c.rowid FROM messages m
                JOIN messages c ON c.id=json_extract(m.content_json,'$.context_summary.through_id')""").fetchall()
        self.assertNotIn('turn-1', [request for request, _ in summaries], 'the summary covering the edited row is gone')
        self.assertTrue(all(through < edited for _, through in summaries), 'no remaining summary covers the edited row')
        calls = len(self.summary_calls)
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-2', 'text': '繼續'}))
        sent = self.texts(self.reply_calls[-1]) + [call['input'][0]['content'] for call in self.summary_calls[calls:]]
        self.assertTrue(any('Q38：改成只找台南' in text for text in sent), 'the corrected text reaches the model')
        self.assertFalse(any(text.startswith('Q38：' + '問') for text in sent))

    def test_short_conversation_is_sent_unchanged(self):
        self.configure()
        self.seed(3)
        self.provider()
        chats.events(self.client.post('/api/chat', json={'request_id': 'short', 'text': '你好'}))
        self.assertEqual(self.summary_calls, [])
        self.assertTrue(self.texts(self.reply_calls[0])[0].startswith('Q0：'))

    def test_failed_summary_still_replies_on_trimmed_history(self):
        self.configure()
        self.seed(40)
        self.provider()
        transport = self.app.state.llm_transport

        def handle(request):
            if json.loads(request.content).get('instructions') == SUMMARY_SYSTEM:
                return httpx.Response(500)
            return transport.handle_request(request)

        self.app.state.llm_transport = httpx.MockTransport(handle)
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'no-summary', 'text': '繼續'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        texts = self.texts(self.reply_calls[0])
        self.assertFalse(any(text.startswith(SUMMARY_PREFIX) or text.startswith('Q0：') for text in texts))

    def test_provider_overflow_compacts_harder_and_resends_once(self):
        self.configure(256_000)
        self.seed(20, size=1000)
        overflow = httpx.Response(400, json={'error': {'code': 'context_length_exceeded', 'message': 'synthetic private detail'}})
        self.provider(lambda payload: overflow if len(self.reply_calls) == 1 else chats.response(text='精簡後完成'))
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'overflow', 'text': '繼續'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.reply_calls), 2)
        self.assertEqual(len(self.summary_calls), 1)
        self.assertFalse(any(name == 'retry' for name, _ in output), 'overflow is not a generic retry')
        self.assertTrue(self.texts(self.reply_calls[1])[0].startswith(SUMMARY_PREFIX))
        self.assertLess(len(encode(self.reply_calls[1])), len(encode(self.reply_calls[0])))

    def test_parameter_errors_mentioning_the_window_are_not_overflow(self):
        self.configure()
        self.seed(3)
        for index, message in enumerate(('max_tokens: 300000 exceeds the model context window limit of 200000',
                                         'max_tokens: 8192 exceeds the context window of 4096',
                                         'max_tokens plus the context length must not exceed the context window')):
            with self.subTest(message=message):
                rejected = httpx.Response(400, json={'error': {'message': message}})
                self.provider(lambda payload, rejected=rejected: rejected)
                output = chats.events(self.client.post('/api/chat', json={'request_id': f'bad-parameter-{index}', 'text': '你好'}))
                error = next(data for name, data in output if name == 'error')
                self.assertEqual(error['code'], 'provider_http_error')
                self.assertEqual(len(self.reply_calls), 1, 'a parameter error is not compacted and resent')

    def test_repeated_overflow_reports_a_clear_error(self):
        self.configure(256_000)
        self.seed(20, size=1000)
        for response in (httpx.Response(400, json={'error': {'message': 'prompt is too long: 300000 tokens > 200000 maximum'}}),
                         httpx.Response(413, text='request entity too large')):
            with self.subTest(status=response.status_code):
                self.provider(lambda payload, response=response: response)
                output = chats.events(self.client.post('/api/chat', json={'request_id': f'too-long-{response.status_code}', 'text': '繼續'}))
                error = next(data for name, data in output if name == 'error')
                self.assertEqual(error['code'], 'context_overflow')
                self.assertNotIn('300000', error['message'])
                self.assertEqual(len(self.reply_calls), 2, 'one compacted resend, no generic retries')



if __name__ == '__main__':
    unittest.main()
