#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""OS-level confinement for a benchmark agent run.

Two independent restrictions, both enforced outside the agent's control:

  - Filesystem: an allowlist. The whole home directory is denied by default;
    `allow_rw` subtrees are opened for writing, `allow_ro` for reading, and
    `deny` subtrees are re-masked last. System paths (/usr, /bin, ...) stay
    open so the toolchain works. This is how the benchmark enforces "write
    only to solvers/<solver>; never touch the referee" at the OS level rather
    than trusting the prompt.
  - Network: `off` (default), `loopback`, or `on`. Enforcement is layered onto
    the `claude` CLI via its own Bash sandbox (--settings) plus WebFetch/WebSearch
    removal; see network_cli_args(). The FS wrapper does not itself block sockets.

macOS uses Seatbelt (`sandbox-exec`); Linux uses bubblewrap (`bwrap`).
"""
import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path

NETWORK_MODES = ("off", "loopback", "on")
DEFAULT_NETWORK = "off"
_LOOPBACK_DOMAINS = ["localhost", "127.0.0.1", "[::1]"]


class SandboxUnavailable(RuntimeError):
    """This host cannot provide the requested confinement; fail closed."""


@dataclasses.dataclass(frozen=True)
class FsPolicy:
    """Allowlist filesystem confinement. Home is denied by default; allow_ro /
    allow_rw subtrees are opened, deny subtrees re-masked last. System paths
    stay open."""
    allow_rw: tuple = ()
    allow_ro: tuple = ()
    deny: tuple = ()

    def merged(self, *, allow_rw=(), allow_ro=(), deny=()):
        return FsPolicy(allow_rw=(*self.allow_rw, *allow_rw),
                        allow_ro=(*self.allow_ro, *allow_ro),
                        deny=(*self.deny, *deny))

    def provenance(self):
        def actual(roots):
            return sorted({str(Path(r).resolve()) for r in roots if Path(r).exists()})
        return {'allow_rw': actual(self.allow_rw), 'allow_ro': actual(self.allow_ro),
                'deny': actual(self.deny), 'default': 'home denied; system paths open'}


def _resolve_existing(roots):
    out = []
    for r in roots:
        p = Path(r).resolve()
        if p.exists():
            out.append(str(p))
    return out


def _covers(root, path):
    root, path = Path(root), Path(path)
    return root == path or root in path.parents


# ------------------------------------------------------------------ macOS Seatbelt
def _sb_esc(s):
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _seatbelt_profile(home, allow_ro, allow_rw, deny):
    def subpaths(roots):
        return '\n'.join('        (subpath "%s")' % _sb_esc(r) for r in roots)
    ancestors = set()
    home_p = Path(home)
    for r in allow_ro + allow_rw:
        for parent in Path(r).parents:
            if parent == home_p or home_p in parent.parents:
                ancestors.add(str(parent))
    ancestors.add(home)
    lines = ['(version 1)', '(allow default)',
             '(deny file-read* file-write*', '        (subpath "%s"))' % _sb_esc(home),
             '(allow file-read-metadata']
    lines += ['        (literal "%s")' % _sb_esc(a) for a in sorted(ancestors)]
    lines += [')']
    if allow_ro:
        lines += ['(allow file-read*', subpaths(allow_ro), ')']
        # read-only also outside home (e.g. a repo under /local); the rw roots below re-allow writes
        lines += ['(deny file-write*', subpaths(allow_ro), ')']
    if allow_rw:
        lines += ['(allow file-read* file-write*', subpaths(allow_rw), ')']
    if deny:
        lines += ['(deny file-read* file-write*', subpaths(deny), ')']
    return '\n'.join(lines) + '\n'


# ------------------------------------------------------------------ Linux bubblewrap
_bwrap_ok = None


def _bwrap_check(launcher):
    global _bwrap_ok
    if _bwrap_ok is not None:
        if _bwrap_ok is not True:
            raise SandboxUnavailable(_bwrap_ok)
        return
    try:
        r = subprocess.run([launcher, '--die-with-parent', '--dev-bind', '/', '/',
                            '--perms', '0000', '--tmpfs', '/tmp', 'true'],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        _bwrap_ok = 'bwrap smoke test failed: %s' % e
        raise SandboxUnavailable(_bwrap_ok)
    if r.returncode != 0:
        _bwrap_ok = 'bwrap cannot run here: %s' % (r.stderr.strip() or 'unknown')
        raise SandboxUnavailable(_bwrap_ok)
    _bwrap_ok = True


def _bwrap_args(launcher, home, allow_ro, allow_rw, deny):
    home_p = Path(home)
    args = [launcher, '--die-with-parent', '--dev-bind', '/', '/',
            '--perms', '0111', '--tmpfs', home]
    # read-only roots are bound read-only wherever they are (also outside home, e.g. a repo under
    # /local); rw roots come after, so a writable subtree of a read-only root stays writable
    for r in sorted(set(allow_ro)):
        args += ['--ro-bind', r, r]
    for r in sorted(set(allow_rw)):
        args += ['--bind', r, r]
    for r in sorted(set(deny)):
        if not Path(r).is_dir():
            raise SandboxUnavailable('deny root %s is not a directory' % r)
        args += ['--perms', '0000', '--tmpfs', r]
    return args


def fs_confine_wrap(argv, policy, *, protect=None):
    """Return ARGV wrapped so the child runs under POLICY. Raises
    SandboxUnavailable if this host cannot confine (fail closed)."""
    home = str(Path.home().resolve())
    allow_ro = _resolve_existing(policy.allow_ro)
    allow_rw = _resolve_existing(policy.allow_rw)
    deny = _resolve_existing(policy.deny)
    for r in allow_ro + allow_rw:
        if Path(r) == Path(home):
            raise SandboxUnavailable('allow root is the whole home: confinement would be vacuous')
    if protect is not None:
        cwd = Path(protect).resolve()
        for r in deny:
            if _covers(r, cwd):
                raise SandboxUnavailable('deny root %s covers the agent workspace %s' % (r, cwd))
        if (cwd == Path(home) or Path(home) in cwd.parents) and \
                not any(_covers(r, cwd) for r in allow_ro + allow_rw):
            raise SandboxUnavailable('workspace %s is under home but not allow-listed' % cwd)
    if sys.platform == 'darwin':
        launcher = shutil.which('sandbox-exec')
        if not launcher:
            raise SandboxUnavailable('sandbox-exec not found on this macOS host')
        return [launcher, '-p', _seatbelt_profile(home, allow_ro, allow_rw, deny), *argv]
    if sys.platform.startswith('linux'):
        launcher = shutil.which('bwrap')
        if not launcher:
            raise SandboxUnavailable('bwrap not found; install bubblewrap for FS confinement')
        _bwrap_check(launcher)
        return [*_bwrap_args(launcher, home, allow_ro, allow_rw, deny), *argv]
    raise SandboxUnavailable('filesystem confinement not implemented on %r' % sys.platform)


# ------------------------------------------------------------------ network
def resolve_network(value):
    v = (value or DEFAULT_NETWORK).strip().lower()
    v = {'0': 'off', 'false': 'off', 'no': 'off', 'none': 'off',
         '1': 'on', 'true': 'on', 'yes': 'on'}.get(v, v)
    if v not in NETWORK_MODES:
        raise SystemExit('network must be one of %s, not %r' % (', '.join(NETWORK_MODES), value))
    return v


# Network-capable Bash commands blocked when network != on. This is a TOOL-POLICY
# block (evaluated even under --dangerously-skip-permissions), not an OS firewall.
# We use it rather than the `claude` CLI's own OS Bash-sandbox because that sandbox
# is itself a sandbox-exec on macOS, and macOS forbids nesting it inside our FS
# Seatbelt wrapper. On Linux (bubblewrap FS confinement) a hard socket-level block
# can be added instead; see README.
NETWORK_DENY_RULES = [
    'Bash(curl:*)', 'Bash(wget:*)', 'Bash(nc:*)', 'Bash(ncat:*)', 'Bash(telnet:*)',
    'Bash(ssh:*)', 'Bash(scp:*)', 'Bash(rsync:*)', 'Bash(ftp:*)',
    'Bash(pip install:*)', 'Bash(pip3 install:*)', 'Bash(npm install:*)',
    'Bash(cargo install:*)', 'Bash(brew install:*)', 'Bash(apt:*)', 'Bash(apt-get:*)',
    'Bash(git push:*)', 'Bash(git fetch:*)', 'Bash(git pull:*)', 'Bash(git clone:*)',
    'Bash(git ls-remote:*)', 'Bash(git remote:*)', 'Bash(git submodule:*)',
]
NETWORK_DISALLOWED_TOOLS = 'WebFetch,WebSearch'


def network_cli_args(network):
    """Extra `claude` CLI args for NETWORK mode. on -> no restriction; off/loopback
    -> drop WebFetch/WebSearch and deny network-capable Bash commands. (loopback is
    treated like off at the tool level — curl to localhost would also be blocked;
    a finer policy needs the OS sandbox, Linux-only.)"""
    if network == 'on':
        return []
    settings = {'permissions': {'deny': NETWORK_DENY_RULES}}
    return ['--disallowed-tools', NETWORK_DISALLOWED_TOOLS,
            '--settings', json.dumps(settings, separators=(',', ':'))]
