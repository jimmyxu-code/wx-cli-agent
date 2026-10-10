"""Database incremental pull using wx-cli's real message query, not session summaries.

Caller provides the already scoped/sanitized wx-cli runner. Keep the returned
cursor only after consuming messages; replaying the old cursor repeats a batch.
No key extraction or transmission, no UI, and no automatic background process.
"""


class ReceiveError(Exception):
    pass


def initialize(run_query, contact, account_args=()):
    envelope = run_query(['query', contact, *account_args, '--limit', '1',
                          '--order', 'desc', '--format', 'json', '--no-server'])
    items = envelope.get('items', [])
    if not isinstance(items, list):
        raise ReceiveError('invalid_message_envelope')
    seq = max((int(item['sort_seq']) for item in items), default=0)
    return {'sortSeq': seq, 'startingFromNow': True, 'messages': [],
            'backend': 'wx_cli_database', 'completeMessageStream': False}


def pull(run_query, contact, sort_seq, account_args=(), limit=50):
    if isinstance(sort_seq, bool) or not isinstance(sort_seq, int) or sort_seq < 0:
        raise ReceiveError('invalid_cursor')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ReceiveError('invalid_limit')
    # One extra row detects a truncated sort_seq bucket. Never advance past
    # unseen rows sharing the same sequence (upstream anchor is strict >).
    envelope = run_query(['query', contact, *account_args, '--after-sort-seq', str(sort_seq),
                          '--limit', str(limit + 1), '--order', 'asc', '--format', 'json', '--no-server'])
    items = envelope.get('items', [])
    if not isinstance(items, list) or any(not isinstance(x, dict) or
            not isinstance(x.get('sort_seq'), int) or x['sort_seq'] <= sort_seq for x in items):
        raise ReceiveError('invalid_message_envelope')
    items = sorted(items, key=lambda x: x['sort_seq'])
    more = len(items) > limit or envelope.get('paging', {}).get('has_more', False)
    if more and items:
        # Defer the complete tail bucket, even if that makes this batch smaller.
        tail = items[-1]['sort_seq']
        items = [x for x in items if x['sort_seq'] < tail]
        if not items:
            raise ReceiveError('cursor_bucket_overflow_increase_limit_or_manual_history_query')
    return {'messages': items, 'sortSeq': max((x['sort_seq'] for x in items), default=sort_seq),
            'hasMore': bool(more), 'backend': 'wx_cli_database',
            'completeMessageStream': False,
            'limitations': ['Only selected conversation; no guarantee for late lower-sequence inserts.',
                            'Keep prior cursor until the batch has been consumed.']}
