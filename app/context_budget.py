"""Provider-agnostic context budget: estimate tokens, mask old tool results, then trim old history."""
import json
import unicodedata

from .store import encode

DEFAULT_WINDOW = 256_000
# Compaction starts well before the window so long-context pricing tiers and estimation error have room.
TRIGGER_RATIO = 0.7
TARGET_RATIO = 0.5
OUTPUT_RESERVE = 4096 + 2048
IMAGE_TOKENS = 1600
MESSAGE_OVERHEAD = 4
KEEP_TOOL_ROUNDS = 2
MASKED_PREFIX = '[已省略較早的工具結果以節省上下文'


def window(settings):
    return int(settings.get('llm_context_window') or DEFAULT_WINDOW)


def budget(settings, ratio=TRIGGER_RATIO):
    return max(8_000, int(window(settings) * ratio) - OUTPUT_RESERVE)


def text_tokens(text):
    # Rough and deliberately conservative: CJK and other wide characters ≈ 1 token, the rest ≈ 4 characters.
    wide = sum(1 for char in text if unicodedata.east_asian_width(char) in ('W', 'F'))
    return wide + (len(text) - wide + 3) // 4


def message_tokens(message):
    content = message.get('content')
    if isinstance(content, list):
        total = sum(IMAGE_TOKENS if block.get('type') == 'image' else text_tokens(block.get('text', '')) for block in content)
    else:
        total = text_tokens(content or '')
    for call in message.get('tool_calls') or []:
        total += text_tokens(call.get('name', '') + call.get('arguments', ''))
    return total + MESSAGE_OVERHEAD


def estimate(messages, tools=()):
    return sum(map(message_tokens, messages)) + text_tokens(encode(list(tools)))


def _job_ids(content):
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, dict):
        return []
    ids = [value['job_id']] if isinstance(value.get('job_id'), str) else []
    for item in value.get('items') or []:
        if isinstance(item, dict):
            ident = item.get('id') or item.get('job_id')
            if isinstance(ident, str):
                ids.append(ident)
    return list(dict.fromkeys(ids))


def mask_tool_results(messages, keep_rounds=KEEP_TOOL_ROUNDS):
    """Replace tool results older than the last `keep_rounds` tool rounds with a short placeholder.

    Reasoning text and tool calls stay intact; the job ids remain so the model can fetch details again.
    Returns how many results were masked.
    """
    rounds = [index for index, message in enumerate(messages) if message.get('role') == 'assistant' and message.get('tool_calls')]
    if keep_rounds <= 0:
        cutoff = len(messages)
    elif len(rounds) > keep_rounds:
        cutoff = rounds[-keep_rounds]
    else:
        return 0
    names = {call['id']: call['name'] for message in messages for call in message.get('tool_calls') or []}
    masked = 0
    for message in messages[:cutoff]:
        content = message.get('content')
        if message.get('role') != 'tool' or not isinstance(content, str) or content.startswith(MASKED_PREFIX):
            continue
        ids = _job_ids(content)
        note = f'{MASKED_PREFIX}：{names.get(message.get("tool_call_id"), "工具")}'
        if ids:
            note += '；涉及 job_id ' + '、'.join(ids[:30])
        message['content'] = note + '；需要時可再查詢。]'
        masked += 1
    return masked


def trim_history(messages, start, end, limit, tools=()):
    """Drop the oldest whole history messages in messages[start:end] until the estimate fits `limit`.

    The system prompt before `start` and everything from `end` (the current turn) are kept.
    Returns the dropped messages in their original order.
    """
    dropped = []
    while estimate(messages, tools) > limit and start < end:
        dropped.append(messages.pop(start))
        end -= 1
        # Never leave a history assistant reply without the user message it answered at the front.
        while start < end and messages[start].get('role') != 'user':
            dropped.append(messages.pop(start))
            end -= 1
    return dropped


def fit(messages, history_start, history_end, trigger, target, tools=(), keep_rounds=KEEP_TOOL_ROUNDS):
    """Over `trigger` tokens: tier 1 masks old tool results, tier 2 drops the oldest history down to `target`.

    Trimming to a lower target than the trigger leaves room to grow, so a saved summary stays reusable
    for several turns instead of being rewritten every turn. Returns (masked, dropped).
    """
    if estimate(messages, tools) <= trigger:
        return 0, []
    masked = mask_tool_results(messages, keep_rounds)
    if estimate(messages, tools) <= trigger:
        return masked, []
    return masked, trim_history(messages, history_start, history_end, target, tools)
