#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a Claude Code stream-json event list into cost/token/tool metrics and
write the per-run artifacts (summary.json, COST.md).
"""
import json


def collect(events):
    """Reduce stream-json events to a metrics dict. Assistant messages are
    deduped by message id (keep last — a message is re-sent as it streams)."""
    by_id = {}
    tool_names = {}
    thinking = 0
    result = None
    for obj in events:
        t = obj.get('type')
        if t == 'result':
            u = obj.get('usage') or {}
            result = {'subtype': obj.get('subtype'), 'is_error': obj.get('is_error'),
                      'num_turns': obj.get('num_turns'), 'duration_ms': obj.get('duration_ms'),
                      'duration_api_ms': obj.get('duration_api_ms'),
                      'total_cost_usd': obj.get('total_cost_usd'), 'usage': u}
        if t != 'assistant':
            continue
        msg = obj.get('message') or {}
        mid = msg.get('id')
        usage = msg.get('usage') or {}
        rec = {'model': msg.get('model'), 'parent_tool_use_id': obj.get('parent_tool_use_id'),
               'input_tokens': usage.get('input_tokens', 0),
               'output_tokens': usage.get('output_tokens', 0),
               'cache_read_tokens': usage.get('cache_read_input_tokens', 0),
               'cache_write_tokens': usage.get('cache_creation_input_tokens', 0)}
        if mid is not None:
            by_id[mid] = rec
        for block in (msg.get('content') or []):
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                n = block.get('name', '?')
                tool_names[n] = tool_names.get(n, 0) + 1
            elif block.get('type') == 'thinking':
                thinking += 1
    calls = list(by_id.values())
    totals = {k: sum(c[k] for c in calls) for k in
              ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')}
    by_model = {}
    for c in calls:
        m = c['model'] or '?'
        d = by_model.setdefault(m, {'llm_call_count': 0, 'input_tokens': 0, 'output_tokens': 0,
                                    'cache_read_tokens': 0, 'cache_write_tokens': 0})
        d['llm_call_count'] += 1
        for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'):
            d[k] += c[k]
    return {'llm_call_count': len(calls), 'totals': totals, 'by_model': by_model,
            'tool_use_count': sum(tool_names.values()), 'tool_names': tool_names,
            'thinking_block_count': thinking,
            'subagent_llm_call_count': sum(1 for c in calls if c['parent_tool_use_id']),
            'result': result}


def _dur(ms):
    if not ms:
        return '0s'
    s = ms / 1000.0
    if s < 60:
        return '%.1fs' % s
    return '%dm%02ds' % (int(s // 60), int(s % 60))


def write_summary(run_dir, metrics):
    (run_dir / 'summary.json').write_text(json.dumps(metrics, indent=2))


def write_cost_md(run_dir, run_id, metrics, auth_mode, diffstat):
    r = metrics.get('result') or {}
    usage = r.get('usage') or {}
    nominal = '  (subscription — cost is nominal, not billed per-token)' if auth_mode == 'subscription' else ''
    lines = [
        '# Cost — run `%s`' % run_id, '',
        '    Auth / billing:        %s%s' % (auth_mode, nominal),
        '    Total cost:            $%.4f' % (r.get('total_cost_usd') or 0),
        '    Duration (API):        %s' % _dur(r.get('duration_api_ms')),
        '    Duration (wall):       %s' % _dur(r.get('duration_ms')),
        '    Turns:                 %s' % r.get('num_turns'),
        '    LLM calls:             %d (%d by subagents)' % (metrics['llm_call_count'],
                                                             metrics['subagent_llm_call_count']),
        '    Tokens (in/out):       %d / %d' % (metrics['totals']['input_tokens'],
                                                metrics['totals']['output_tokens']),
        '    Cache (read/write):    %d / %d' % (metrics['totals']['cache_read_tokens'],
                                                metrics['totals']['cache_write_tokens']),
        '    Code changes:          +%s / -%s lines' % (diffstat.get('lines_added', 0),
                                                        diffstat.get('lines_removed', 0)),
        '    Tool uses:             %s' % ' '.join('%s=%d' % kv for kv in
                                                   sorted(metrics['tool_names'].items())),
        '', '## Raw usage (from result event)', '', '```json',
        json.dumps(usage, indent=2), '```', '',
    ]
    (run_dir / 'COST.md').write_text('\n'.join(lines))
