"""Run one command inside a lane with the campaign environment, as a single prompt-free call.

Usage:
    python run.py <lane-dir> <env-file> [--out <file>] [--tail N] -- <command> [args...]

The env file is the campaign's env.sh (VAR=value / export VAR=value / unset VAR, with $VAR expansion)
or a PowerShell rendering ($env:VAR = 'value' / Remove-Item Env:VAR). Lines that are neither are ignored,
so `mkdir -p` lines are handled here by creating every directory-valued temp/cache variable.
The command runs with the lane as working directory. With --out, combined output goes to the file and
the exit code to <file>.exit; the last N lines (default 60) are printed. The exit code is passed through.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ASSIGN = re.compile(r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$')
PS_ASSIGN = re.compile(r'^\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
PS_REMOVE = re.compile(r'^Remove-Item\s+Env:([A-Za-z_][A-Za-z0-9_]*)')
DIR_VARS = {'TMPDIR', 'TEMP', 'TMP', 'PIP_CACHE_DIR', 'UV_CACHE_DIR', 'XDG_CACHE_HOME', 'NPM_CONFIG_CACHE', 'PYTHONPYCACHEPREFIX'}


def unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        return value[1:-1]
    return value


def expand(value, env):
    return re.sub(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?', lambda m: env.get(m.group(1), ''), value)


PS_LOCAL = re.compile(r'^\$(?!env:)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def load_env(path):
    env = dict(os.environ)
    local = {}
    for raw in Path(path).read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = PS_LOCAL.match(line)
        if match:
            local[match.group(1)] = expand(unquote(match.group(2)), {**env, **local})
            continue
        if line.startswith('unset '):
            for name in line.split()[1:]:
                env.pop(name, None)
            continue
        match = PS_REMOVE.match(line)
        if match:
            env.pop(match.group(1), None)
            continue
        match = ASSIGN.match(line) or PS_ASSIGN.match(line)
        if match:
            name, value = match.group(1), match.group(2)
            env[name] = expand(unquote(value.split(' #')[0]), {**env, **local})
    for name in DIR_VARS & env.keys():
        if env[name]:
            Path(env[name]).mkdir(parents=True, exist_ok=True)
    env.pop('SSLKEYLOGFILE', None)
    return env


def main(argv):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    if '--' not in argv or len(argv) < 4:
        raise SystemExit(__doc__)
    split = argv.index('--')
    head, command = argv[:split], argv[split + 1:]
    lane, env_file, options = head[0], head[1], head[2:]
    out = options[options.index('--out') + 1] if '--out' in options else None
    tail = int(options[options.index('--tail') + 1]) if '--tail' in options else 60
    env = load_env(env_file)
    executable = shutil.which(command[0], path=env.get('PATH')) or command[0]
    result = subprocess.run([executable, *command[1:]], cwd=lane, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    text = result.stdout + result.stderr
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding='utf-8')
        Path(out + '.exit').write_text(str(result.returncode), encoding='utf-8')
    print('\n'.join(text.splitlines()[-tail:]))
    print(f'[exit {result.returncode}]')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
