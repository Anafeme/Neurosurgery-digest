"""Resume a weekly Telegram batch from acknowledged receipts, without duplicate retries."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class DeliveryError(RuntimeError):
    """A controlled diagnostic that never contains credentials or HTTP response bodies."""


def week_key(now=None):
    current = (now or datetime.now(ZoneInfo('Europe/Moscow'))).astimezone(ZoneInfo('Europe/Moscow')).date()
    return (current - timedelta(days=current.weekday())).isoformat()


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def load_state(path):
    if not Path(path).exists():
        return None
    try:
        state = json.loads(Path(path).read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('version') != 1
                or not isinstance(state.get('week'), str)
                or not isinstance(state.get('messages'), list)
                or not state['messages']
                or not all(isinstance(m, str) and m for m in state['messages'])
                or not isinstance(state.get('sent'), list)
                or not all(type(i) is int and 0 <= i < len(state['messages']) for i in state['sent'])
                or not isinstance(state.get('completed'), bool)):
            raise ValueError
        uncertain = state.get('uncertain')
        if uncertain is not None and (type(uncertain) is not int or not 0 <= uncertain < len(state['messages'])):
            raise ValueError
        if state['completed'] and set(state['sent']) != set(range(len(state['messages']))):
            raise ValueError
        return state
    except (OSError, ValueError, TypeError):
        raise DeliveryError('Invalid delivery checkpoint; automatic resend stopped') from None


def deliver_weekly(factory, sender, path, now=None, force=False):
    """Generate once, persist each receipt, and send only unacknowledged messages."""
    week = week_key(now)
    state = load_state(path)
    if state and state['week'] == week and state['completed'] and not force:
        print(f'SKIP: digest for week {week} already delivered', flush=True)
        return 0
    if not state or state['week'] != week or force:
        messages = factory()
        if not isinstance(messages, list) or not messages or not all(isinstance(m, str) and m for m in messages):
            raise DeliveryError('Invalid digest batch')
        state = {'version': 1, 'week': week, 'messages': messages,
                 'sent': [], 'uncertain': None, 'completed': False}
        save_state(path, state)
    if state.get('uncertain') is not None:
        raise DeliveryError('Telegram delivery is uncertain; automatic resend paused to avoid duplicates')

    pending = [i for i in range(len(state['messages'])) if i not in state['sent']]

    def acknowledge(index):
        original_index = pending[index]
        if original_index not in state['sent']:
            state['sent'].append(original_index)
        save_state(path, state)

    def uncertain(index):
        state['uncertain'] = pending[index]
        save_state(path, state)

    delivered = sender([state['messages'][i] for i in pending],
                       on_delivered=acknowledge, on_uncertain=uncertain) if pending else 0
    if set(state['sent']) != set(range(len(state['messages']))):
        raise DeliveryError('Digest has unconfirmed messages')
    state['completed'] = True
    save_state(path, state)
    print(f'SUCCESS: weekly checkpoint confirms {len(state["messages"])} Telegram messages; week={week}', flush=True)
    return delivered
