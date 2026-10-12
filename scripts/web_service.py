#!/usr/bin/env python3
"""Install and administer the user-session Virty web console."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from vm_registry import private_directory, atomic_write

LABEL = 'com.virty.web'
STATE = ROOT / 'runtime/web'
PLIST = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
TARGET = 'gui/{}/{}'.format(os.getuid(), LABEL)


def job_definition(config, node):
    return {
        'Label': LABEL,
        'ProgramArguments': [node, str(ROOT / 'Web/server.mjs'), str(config)],
        'WorkingDirectory': str(ROOT),
        'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'VIRTY_BROKER_HOME': str(ROOT / 'runtime/secrets')},
        'LimitLoadToSessionType': 'Aqua', 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 10, 'ExitTimeOut': 10, 'ProcessType': 'Background', 'Umask': 0o077,
        'StandardOutPath': str(STATE / 'service.stdout.log'),
        'StandardErrorPath': str(STATE / 'service.stderr.log'),
    }


def launchctl(*args, check=True):
    result = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or 'launchctl failed.')
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Virty web console service and local account setup')
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install', help='Install the Aqua web service; does not restart VM workers')
    install.add_argument('--origin', help='Canonical HTTPS origin; required for a new installation')
    install.add_argument('--bind', default='127.0.0.1', help='Backend bind address (default: loopback)')
    install.add_argument('--port', type=int, default=8765)
    install.add_argument('--proxy', help='Trusted proxy IP; required for a new installation')
    for name in ('start', 'stop', 'restart', 'status', 'uninstall'):
        commands.add_parser(name)
    setup = commands.add_parser('setup', help='Show the one-time setup code in a local terminal and open account enrollment')
    setup.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    try:
        private_directory(STATE)
        config_path = STATE / 'config.json'
        if args.command == 'install':
            node = shutil.which('node')
            if not node:
                raise ValueError('Install Node.js 20 or newer first.')
            if not 1024 <= args.port <= 65535:
                raise ValueError('Use a port from 1024 to 65535.')
            if not config_path.exists():
                if not args.origin or not args.proxy:
                    raise ValueError('New installations require explicit --origin and --proxy values.')
                atomic_write(config_path, (json.dumps({'origin': args.origin, 'bind': args.bind, 'port': args.port,
                                                      'proxyPeers': [args.proxy], 'state': str(STATE)}, indent=2) + '\n').encode())
            PLIST.parent.mkdir(parents=True, exist_ok=True)
            if PLIST.exists():
                previous = plistlib.loads(PLIST.read_bytes())
                if previous.get('Label') != LABEL or str(ROOT / 'Web/server.mjs') not in previous.get('ProgramArguments', []):
                    raise ValueError('Refusing to replace an unrelated LaunchAgent.')
                launchctl('bootout', TARGET, check=False)
            for filename in ('service.stdout.log', 'service.stderr.log'):
                descriptor = os.open(STATE / filename, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
            atomic_write(PLIST, plistlib.dumps(job_definition(config_path, str(Path(node).resolve()))))
            launchctl('enable', TARGET)
            launchctl('bootstrap', 'gui/{}'.format(os.getuid()), str(PLIST))
            print('Installed {}. Run virty-web setup in your local terminal to enroll your account.'.format(LABEL))
        elif args.command == 'setup':
            if not sys.stdout.isatty() or not sys.stdin.isatty():
                raise ValueError('Account setup requires a local interactive terminal; the code is never returned to agent tools.')
            if (STATE / 'account.json').exists():
                raise ValueError('The account is already configured. Sign in at the web address.')
            config = json.loads(config_path.read_text())
            code_path = STATE / 'setup-code'
            if not code_path.exists():
                raise ValueError('Start the web service first, then retry setup.')
            print('Open {}/setup, then enter this one-time setup code:'.format(config['origin']))
            print(code_path.read_text().strip())
            print('Choose your username/password, then add the displayed key to your authenticator app.')
            if not args.no_open:
                subprocess.run(['/usr/bin/open', config['origin'] + '/setup'], check=True)
        elif args.command == 'status':
            result = launchctl('print', TARGET, check=False)
            print(json.dumps({'ok': True, 'loaded': result.returncode == 0,
                              'accountConfigured': (STATE / 'account.json').exists(),
                              'origin': json.loads(config_path.read_text())['origin'] if config_path.exists() else None}))
        elif args.command == 'stop':
            launchctl('disable', TARGET)
            launchctl('bootout', TARGET, check=False)
            print('Web service stopped. VM workers continue running.')
        elif args.command == 'uninstall':
            launchctl('bootout', TARGET, check=False)
            PLIST.unlink(missing_ok=True)
            print('Web LaunchAgent removed. Account and VM data retained.')
        else:
            if not PLIST.exists():
                raise ValueError('Install the web service first.')
            launchctl('enable', TARGET)
            if launchctl('print', TARGET, check=False).returncode:
                launchctl('bootstrap', 'gui/{}'.format(os.getuid()), str(PLIST))
            launchctl('kickstart', '-k' if args.command == 'restart' else '-p', TARGET)
            print('Web service started. VM workers continue running.')
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
