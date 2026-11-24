#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run Claude Code as the benchmark agent.

This drives the `claude` CLI as a subprocess (no SDK dependency) in headless
`--print --output-format stream-json` mode, which turns stdout into a JSONL
event stream: every assistant message with its tool calls and per-call token
usage, and a final `result` envelope with run totals (usage, total_cost_usd,
num_turns). One agent, one task, run synchronously with a wall-clock timeout.

Auth can be api-key / subscription / bedrock / auto: the same machine may be
logged in via an Anthropic API key, a Claude subscription (OAuth, stored by the
CLI), or AWS Bedrock, and the benchmark records which one a run billed.
"""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path


# ------------------------------------------------------------------ auth
def resolve_auth(mode):
    """Return (resolved_mode, env_override, warnings). env_override is layered
    on top of os.environ for the child."""
    has_key = bool(os.environ.get('ANTHROPIC_API_KEY'))
    has_oauth = bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN'))
    has_bedrock = os.environ.get('CLAUDE_CODE_USE_BEDROCK') == '1'
    warnings = []
    if mode == 'auto':
        if has_key:
            mode = 'api-key'
        elif has_oauth:
            mode = 'subscription'
        elif has_bedrock:
            mode = 'bedrock'
        else:
            mode = 'subscription'
            warnings.append('no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN / Bedrock env found — '
                            'falling back to the `claude` CLI stored login. Run `claude` and /login, '
                            'or `claude setup-token`, for a reproducible headless credential.')
    env_override = {}
    if mode == 'api-key':
        if not has_key:
            raise SystemExit('--auth api-key but ANTHROPIC_API_KEY is not set')
    elif mode == 'subscription':
        if has_key:
            env_override['ANTHROPIC_API_KEY'] = ''   # mask so the CLI bills the subscription
            warnings.append('ANTHROPIC_API_KEY is set but --auth subscription chosen; masking the key.')
        if not has_oauth:
            warnings.append('using the `claude` CLI stored login. For a reproducible headless '
                            'credential, run `claude setup-token` and export CLAUDE_CODE_OAUTH_TOKEN.')
    elif mode == 'bedrock':
        if not has_bedrock:
            raise SystemExit('--auth bedrock but CLAUDE_CODE_USE_BEDROCK=1 is not set')
        env_override['CLAUDE_CODE_USE_BEDROCK'] = '1'
        for v in ('AWS_REGION', 'AWS_BEARER_TOKEN_BEDROCK', 'AWS_PROFILE', 'AWS_DEFAULT_REGION'):
            if os.environ.get(v):
                env_override[v] = os.environ[v]
    else:
        raise SystemExit('unknown auth mode %r (auto|api-key|subscription|bedrock)' % mode)
    return mode, env_override, warnings


def backend_provenance(auth_mode):
    return 'bedrock' if auth_mode == 'bedrock' else 'anthropic'


# ------------------------------------------------------------------ claude state paths
def claude_state_paths():
    """FS-sandbox exceptions the `claude` CLI itself needs: its config (rw), the
    login Keychain on macOS (ro), ambient plugins/skills re-denied so the agent
    cannot load them."""
    home = Path.home()
    return {'allow_rw': [home / '.claude', home / '.claude.json'],
            'allow_ro': [home / 'Library' / 'Keychains'],
            'deny': [home / '.claude' / 'plugins', home / '.claude' / 'skills']}


def toolchain_ro_paths():
    """Read-only dirs holding the executables the agent invokes but that live
    under $HOME (so the home-default deny would block them): the Node install
    that hosts the `claude` CLI, and the Rust toolchain (~/.rustup) that `cargo
    build` shells out to. System-path tools (/usr, /opt/homebrew, ...) are
    already allowed by the sandbox default."""
    import shutil
    home = Path.home()
    out = []
    for tool in ('claude', 'node'):
        w = shutil.which(tool)
        if not w:
            continue
        p = Path(w)
        out += [p.parent, p.parent.parent, p.resolve().parent]   # bin/, version root, real target
    out += [home / '.rustup', home / '.cargo' / 'bin']
    seen, uniq = set(), []
    for p in out:
        s = str(p)
        if s not in seen and p.exists():
            seen.add(s)
            uniq.append(p)
    return uniq


# ------------------------------------------------------------------ argv
def build_argv(binary='claude', *, model=None, add_dirs=(), extra=()):
    argv = [binary, '--print', '--output-format', 'stream-json', '--verbose',
            '--dangerously-skip-permissions']
    if model:
        argv += ['--model', model]
    for d in add_dirs:
        argv += ['--add-dir', str(d)]
    argv += list(extra)
    return argv


# ------------------------------------------------------------------ stream-json parsing
def stream_events(text):
    events = []
    for line in (text or '').splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def final_text(events):
    for obj in reversed(events):
        if obj.get('type') == 'result':
            r = obj.get('result')
            if isinstance(r, str) and r:
                return r
    return ''


def session_id(events):
    for obj in reversed(events):
        sid = obj.get('session_id')
        if isinstance(sid, str) and sid:
            return sid
    return None


# ------------------------------------------------------------------ run
class AgentRun:
    def __init__(self, returncode, seconds, timed_out, events, transcript_path):
        self.returncode = returncode
        self.seconds = seconds
        self.timed_out = timed_out
        self.events = events
        self.transcript_path = transcript_path
        self.final_text = final_text(events)
        self.session_id = session_id(events)


def run_agent(argv, prompt, *, cwd, env_override=None, timeout, transcript_path):
    """Spawn ARGV, pipe PROMPT on stdin, stream stdout (the stream-json events)
    live to TRANSCRIPT_PATH, and enforce a wall-clock TIMEOUT by killing the
    whole process group. Returns an AgentRun. Stdout is captured in full; the
    transcript survives a crash because each line is flushed as it arrives."""
    env = {**os.environ, **(env_override or {})}
    t0 = time.time()
    p = subprocess.Popen(argv, cwd=str(cwd), env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, universal_newlines=True,
                         bufsize=1, start_new_session=True)
    timed_out = {'v': False}

    def _kill():
        timed_out['v'] = True
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except OSError:
            try:
                p.kill()
            except OSError:
                pass
    timer = threading.Timer(timeout, _kill)
    timer.start()
    chunks = []
    try:
        try:
            p.stdin.write(prompt)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        with open(transcript_path, 'w') as tf:
            for line in p.stdout:
                chunks.append(line)
                tf.write(line)
                tf.flush()
        p.wait()
    finally:
        timer.cancel()
    secs = time.time() - t0
    return AgentRun(p.returncode, round(secs, 3), timed_out['v'],
                    stream_events(''.join(chunks)), str(transcript_path))
