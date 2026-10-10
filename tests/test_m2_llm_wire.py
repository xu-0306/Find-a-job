import copy
import unittest

from app.llm_wire import build_payload, decode_event


MESSAGES = [
    {"role": "system", "content": "Be kind."},
    {"role": "user", "content": "Find jobs."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call-1", "name": "search_jobs", "arguments": '{"keyword":"採購"}'}],
    },
    {"role": "tool", "tool_call_id": "call-1", "content": "結果"},
]
TOOLS = [{
    "name": "search_jobs",
    "description": "Search jobs",
    "parameters": {"type": "object", "properties": {"keyword": {"type": "string"}}},
}]


class LlmWireTests(unittest.TestCase):
    def test_image_blocks_translate_without_mutation_and_reject_unknown_content(self):
        messages = [{'role':'user','content':[{'type':'image','media_type':'image/png','data':'AAAA'}, {'type':'text','text':'請讀圖'}]}]
        before = copy.deepcopy(messages)
        for protocol in ('responses','chat_completions','anthropic_messages'):
            payload = build_payload(protocol,'vision',messages,[])
            block = payload['input' if protocol=='responses' else 'messages'][0]['content'][0]
            if protocol == 'responses': self.assertEqual(block,{'type':'input_image','image_url':'data:image/png;base64,AAAA'})
            elif protocol == 'chat_completions': self.assertEqual(block,{'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}})
            else: self.assertEqual(block,{'type':'image','source':{'type':'base64','media_type':'image/png','data':'AAAA'}})
            self.assertEqual(messages,before)
        for role,content in [('assistant',messages[0]['content']),('system',messages[0]['content']),('user',[]),
                             ('user',[{'type':'image','media_type':'image/svg+xml','data':'AAAA'}]),
                             ('user',[{'type':'image','media_type':'image/png','data':''}]),('user',[{'type':'url','url':'https://private.invalid'}])]:
            with self.subTest(role=role,content=content), self.assertRaises(ValueError):
                build_payload('responses','vision',[{'role':role,'content':content}],[])

    def test_build_payloads_are_streaming_and_do_not_mutate_input(self):
        for protocol in ("responses", "chat_completions", "anthropic_messages"):
            messages = copy.deepcopy(MESSAGES)
            tools = copy.deepcopy(TOOLS)
            original_messages = copy.deepcopy(messages)
            original_tools = copy.deepcopy(tools)
            payload = build_payload(protocol, "model-x", messages, tools)
            self.assertEqual(messages, original_messages)
            self.assertEqual(tools, original_tools)
            self.assertTrue(payload["stream"])
            self.assertEqual(payload["model"], "model-x")
            if protocol == "responses":
                self.assertEqual(payload["instructions"], "Be kind.")
                self.assertEqual(payload["input"][1]["type"], "function_call")
                self.assertEqual(payload["input"][1]["call_id"], "call-1")
                self.assertEqual(payload["input"][2]["type"], "function_call_output")
                self.assertEqual(payload["tools"][0]["name"], "search_jobs")
            elif protocol == "chat_completions":
                self.assertEqual(payload["messages"][2]["tool_calls"][0]["id"], "call-1")
                self.assertEqual(payload["tools"][0]["function"]["parameters"], TOOLS[0]["parameters"])
            else:
                self.assertEqual(payload["system"], "Be kind.")
                self.assertEqual(payload["messages"][1]["content"][0]["type"], "tool_use")
                self.assertEqual(payload["messages"][2]["content"][0]["type"], "tool_result")
                self.assertEqual(payload["max_tokens"], 4096)

    def test_responses_fragments_parallel_calls_and_terminal_events(self):
        state = {}
        events = [
            {"type": "response.output_text.delta", "delta": "找"},
            {"type": "response.output_text.delta", "delta": "到了"},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", "id": "item-a", "call_id": "call-a", "name": "a", "arguments": ""},
            },
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {"type": "function_call", "id": "item-b", "call_id": "call-b", "name": "b", "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "item-a", "delta": '{"x"'},
            {"type": "response.function_call_arguments.delta", "output_index": 1, "item_id": "item-b", "delta": '{"y":2}'},
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item-a",
                "arguments": '{"x":1}',
            },
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {"type": "function_call", "id": "item-b", "call_id": "call-b", "name": "b", "arguments": '{"y":2}'},
            },
            {"type": "response.completed"},
            {"type": "response.completed"},
        ]
        output = [item for event in events for item in decode_event("responses", event, state)]
        self.assertEqual(output, [
            {"kind": "text_delta", "text": "找"},
            {"kind": "text_delta", "text": "到了"},
            {"kind": "tool_call", "id": "call-a", "name": "a", "arguments": '{"x":1}'},
            {"kind": "tool_call", "id": "call-b", "name": "b", "arguments": '{"y":2}'},
            {"kind": "done"},
        ])

    def test_responses_arguments_done_validates_item_id_and_uses_stored_call(self):
        state = {}
        item = {"type": "function_call", "id": "item-7", "call_id": "call-9", "name": "list_preferences", "arguments": ""}
        self.assertEqual(decode_event("responses", {"type": "response.output_item.added", "output_index": 2, "item": item}, state), [])
        self.assertEqual(decode_event("responses", {
            "type": "response.function_call_arguments.done",
            "output_index": 2,
            "item_id": "item-7",
            "arguments": "{}",
        }, state), [{"kind": "tool_call", "id": "call-9", "name": "list_preferences", "arguments": "{}"}])
        self.assertEqual(decode_event("responses", {
            "type": "response.output_item.done",
            "output_index": 2,
            "item": {**item, "arguments": "{}"},
        }, state), [])
        mismatched = {}
        self.assertEqual(decode_event("responses", {"type": "response.output_item.added", "output_index": 3, "item": item}, mismatched), [])
        with self.assertRaisesRegex(ValueError, "^invalid LLM wire data$"):
            decode_event("responses", {
                "type": "response.function_call_arguments.done",
                "output_index": 3,
                "item_id": "wrong-item",
                "arguments": "{}",
            }, mismatched)

    def test_chat_calls_emit_at_finish_and_done_sentinel(self):
        state = {}
        first = decode_event("chat_completions", {"choices": [{"delta": {"content": "嗨"}, "finish_reason": None}]}, state)
        second = decode_event("chat_completions", {
            "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-a", "function": {"name": "a", "arguments": '{"x"'}},
                {"index": 1, "id": "call-b", "function": {"name": "b", "arguments": '{"y":2}'}},
            ]}, "finish_reason": None}],
        }, state)
        second += decode_event("chat_completions", {
            "choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": ":1}"}},
            ]}, "finish_reason": "tool_calls"}],
        }, state)
        terminal = decode_event("chat_completions", "[DONE]", state)
        self.assertEqual(first, [{"kind": "text_delta", "text": "嗨"}])
        self.assertEqual(second, [
            {"kind": "tool_call", "id": "call-a", "name": "a", "arguments": '{"x":1}'},
            {"kind": "tool_call", "id": "call-b", "name": "b", "arguments": '{"y":2}'},
        ])
        self.assertEqual(terminal, [{"kind": "done"}])
        self.assertEqual(decode_event("chat_completions", "[DONE]", state), [])

    def test_anthropic_text_tool_and_reasoning_are_normalized(self):
        state = {}
        stream = [
            {"type": "message_start", "message": {"id": "m"}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "secret"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "secret"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "secret"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "好"}},
            {"type": "content_block_stop", "index": 1},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "tool-1", "name": "search_jobs", "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"keyword":"採購"}'}},
            {"type": "content_block_stop", "index": 2},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ]
        output = [item for event in stream for item in decode_event("anthropic_messages", event, state)]
        self.assertEqual(output, [
            {"kind": "text_delta", "text": "好"},
            {"kind": "tool_call", "id": "tool-1", "name": "search_jobs", "arguments": '{"keyword":"採購"}'},
            {"kind": "done"},
        ])
        self.assertNotIn("secret", str(output))

    def test_provider_errors_are_safe_and_malformed_required_shape_raises(self):
        state = {}
        result = decode_event("responses", {"type": "error", "message": "api-key-secret"}, state)
        self.assertEqual(result[0]["kind"], "error")
        self.assertNotIn("api-key-secret", str(result))
        self.assertEqual(decode_event("responses", {"type": "response.completed"}, state), [])
        with self.assertRaisesRegex(ValueError, "^invalid LLM wire data$"):
            decode_event("responses", {"type": "response.output_text.delta"}, {})
        with self.assertRaisesRegex(ValueError, "^invalid LLM wire data$"):
            build_payload("chat_completions", "model", [{"role": "user", "content": 1}], [])


if __name__ == "__main__":
    unittest.main()
