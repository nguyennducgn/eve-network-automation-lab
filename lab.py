"""Small EVE-NG lab: deploy, inspect, verify and back up over console or SSH.

SSH is available after setup_ssh.py; VPCS always uses its EVE console.
Run only against the named lab while it is open and its six nodes are running.
"""
import argparse
import concurrent.futures
import datetime
import getpass
import json
from pathlib import Path
import re
import sys
import time

BASE = Path(__file__).resolve().parent
# Packages prepared for this computer; a normal pip install works on other PCs.
LOCAL_PACKAGES = BASE.parent.parent / 'work' / 'python_packages'
if LOCAL_PACKAGES.is_dir():
    sys.path.insert(0, str(LOCAL_PACKAGES))
# Isolated compatibility dependency for the IOS images in this lab.
SSH_PACKAGES = BASE.parent.parent / 'work' / 'ssh_compat'
if SSH_PACKAGES.is_dir():
    sys.path.insert(0, str(SSH_PACKAGES))


def config_lines(name):
    return [line.strip() for line in (BASE / 'configs' / f'{name}.cfg').read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('!') and line.strip() != 'end']


def terminal_text(text):
    text = re.sub(r'\x1b\][^\x07]*(?:\x07|\x1b\\)', '', text)
    return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)


def vpcs_command(conn, command, transcript, timeout=30):
    # A console may already contain a banner/prompt when we attach. Drain it
    # before sending exactly one Return; CRLF can generate two prompts in VPCS.
    old = conn.read_very_eager().decode(errors='replace')
    if old:
        transcript.append('[pending console output]\n' + terminal_text(old))
    conn.write((command + '\r').encode('ascii'))
    response = ''
    deadline = time.monotonic() + timeout
    prompt_since = None
    while time.monotonic() < deadline:
        chunk = conn.read_very_eager().decode(errors='replace')
        if chunk:
            response += chunk
            prompt_since = None
        clean = terminal_text(response)
        # For nonempty commands the response must contain that command's echo.
        # A leftover prompt alone must never count as successful completion.
        echoed = not command or command in clean
        ended = bool(re.search(r'(?:^|[\r\n])[\w.\[\]-]+>\s*$', clean))
        if echoed and ended:
            if prompt_since is None:
                prompt_since = time.monotonic()
            elif time.monotonic() - prompt_since >= 0.25:
                transcript.append(command + '\n' + clean)
                return clean
        time.sleep(0.05)
    transcript.append(command + '\n' + terminal_text(response))
    raise RuntimeError('VPCS did not complete command: ' + (command or '<Return>'))


def vpcs_ip_is_correct(text, expected_ip):
    return bool(re.search(r'^\s*IP/MASK\s*:\s*' + re.escape(expected_ip) + r'/24\s*$', text, re.M))


def run_device(host, device, action, output_dir, ssh=None):
    from netmiko import ConnectHandler
    from netmiko._telnetlib import telnetlib
    name = device['name']
    record = {'device': name, 'action': action, 'ok': False, 'checks': []}
    use_ssh = ssh is not None and device['kind'] != 'vpcs'
    record['transport'] = 'ssh' if use_ssh else 'console'
    transcript = []

    def check(label, passed):
        record['checks'].append({'name': label, 'passed': bool(passed)})

    try:
        if device['kind'] == 'vpcs':
            with telnetlib.Telnet(host, device['port'], timeout=10) as conn:
                def pc_command(command):
                    return vpcs_command(conn, command, transcript)
                pc_command('')
                if action == 'deploy':
                    for command in config_lines(name):
                        text = pc_command(command)
                        if re.search(r'Invalid|Unknown|Bad command', text, re.I):
                            raise RuntimeError(text)
                text = pc_command('show ip')
                if action in ('verify', 'deploy'):
                    check('PC address and /24 mask', vpcs_ip_is_correct(text, device['ip']))
                    check('PC gateway', bool(re.search(r'^\s*GATEWAY\s*:\s*10\.10\.10\.1\s*$', text, re.M)))
                if action == 'deploy':
                    if not all(c['passed'] for c in record['checks']):
                        raise RuntimeError('PC1 IP/gateway was not applied; configuration not saved')
                    pc_command('save')
                if action == 'verify':
                    for target in ('10.10.10.1', '10.10.10.11', '10.10.10.12',
                                   '10.255.0.1', '10.255.0.2', '10.255.0.3'):
                        text = pc_command(f'ping {target} -c 3')
                        replies = len(re.findall(r'bytes from\s+' + re.escape(target), text, re.I))
                        if replies < 3:
                            text = pc_command(f'ping {target} -c 3')
                            replies = len(re.findall(r'bytes from\s+' + re.escape(target), text, re.I))
                        check('PC1 -> ' + target, replies == 3)
                if action == 'backup':
                    (output_dir / 'PC1-show-ip.txt').write_text(text, encoding='utf-8')
        else:
            if use_ssh:
                from ssh_support import connection_args
                params = connection_args(ssh['devices'][name]['host'], ssh['username'], ssh['password'])
            else:
                params = dict(device_type='cisco_ios_telnet', host=host,
                              port=device['port'], username='', password='',
                              secret='', conn_timeout=10, auth_timeout=30, fast_cli=False)
            with ConnectHandler(**params) as conn:
                from ssh_support import verify_identity
                verify_identity(conn, name)
                if action == 'deploy':
                    text = conn.send_config_set(config_lines(name), cmd_verify=False,
                                                error_pattern=r'% (?:Invalid input|Incomplete command|Ambiguous command|Error)',
                                                read_timeout=120)
                    transcript.append(text)
                    conn.set_base_prompt()
                    transcript.append(conn.save_config())
                if action == 'backup':
                    text = conn.send_command('show running-config', read_timeout=60)
                    (output_dir / f'{name}-running.cfg').write_text(text + '\n', encoding='utf-8')
                    startup = conn.send_command('show startup-config', read_timeout=60)
                    (output_dir / f'{name}-startup.cfg').write_text(startup + '\n', encoding='utf-8')
                    check('Configuration retrieved', f'hostname {name}' in text and '\nend' in text)
                commands = ['show ip interface brief']
                if device['role'] == 'router':
                    commands += ['show ip ospf neighbor', 'show ip route ospf']
                else:
                    commands += ['show vlan brief', 'show interfaces trunk']
                for command in commands:
                    text = conn.send_command(command, read_timeout=60)
                    transcript.append(command + '\n' + text)
                    if action == 'verify':
                        if command == 'show ip interface brief':
                            check('Expected IP is up/up', bool(re.search(
                                re.escape(device['ip']) + r'\s+\S+\s+\S+\s+up\s+up', text)))
                        elif command == 'show ip ospf neighbor':
                            check('OSPF FULL neighbor count', len(re.findall(r'\bFULL/', text)) == device['ospf_neighbors'])
                        elif command == 'show vlan brief':
                            check('VLAN 10 active', bool(re.search(r'^10\s+AUTOMATION_LAN\s+active', text, re.M)))
                        elif command == 'show interfaces trunk':
                            check('Trunk forwarding VLAN 10', bool(re.search(
                                r'Vlans in spanning tree forwarding state and not pruned[\s\S]*\b10\b', text)))
                if action == 'verify':
                    target = '10.255.0.1' if name == 'R3' else '10.255.0.3'
                    for attempt in range(2):
                        text = conn.send_command(f'ping {target} repeat 5 timeout 1', read_timeout=30)
                        transcript.append(text)
                        if 'Success rate is 100 percent' in text:
                            break
                    check('Reachability -> ' + target, 'Success rate is 100 percent' in text)
        record['ok'] = all(c['passed'] for c in record['checks'])
    except Exception as error:
        record['error'] = str(error).replace(ssh['password'], '[REDACTED]') if use_ssh else str(error)
        if getattr(error, 'winerror', None) == 10061 or '10061' in str(error):
            record['hint'] = ('EVE refused the console connection. Open Network_Automation_6_Nodes, '
                              'choose More actions > Start all nodes, wait for boot, and retry. '
                              'If already running, compare the node console port with inventory.json.')
    (output_dir / f'{name}-{action}.txt').write_text('\n\n'.join(transcript), encoding='utf-8')
    print(f"{name:4} {action:7} {'PASS' if record['ok'] else 'FAIL'}", flush=True)
    if not record['ok']:
        print(f"  {name}: {record.get('hint', record.get('error', 'One or more checks failed; see report.json'))}", flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'facts', 'deploy', 'verify', 'backup', 'all'])
    parser.add_argument('--host', help='Override EVE address from inventory')
    parser.add_argument('--devices', nargs='+', help='Only these inventory device names')
    parser.add_argument('--transport', choices=['console', 'ssh'], default='console')
    args = parser.parse_args()
    inventory = json.loads((BASE / 'inventory.json').read_text())
    devices = inventory['devices']
    if args.devices:
        unknown = set(args.devices) - {d['name'] for d in devices}
        if unknown:
            parser.error('Unknown devices: ' + ', '.join(sorted(unknown)))
        devices = [d for d in devices if d['name'] in args.devices]
    host = args.host or inventory['eve_host']
    ssh = None
    if args.transport == 'ssh':
        from ssh_support import load_inventory
        try:
            ssh = load_inventory([d['name'] for d in devices if d['kind'] == 'ios'])
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.action == 'plan':
        for device in devices:
            target = f"{ssh['devices'][device['name']]['host']}:22 (SSH)" if ssh and device['kind'] == 'ios' else f"{host}:{device['port']} (console)"
            print(f"{device['name']}: {target}")
            print('\n'.join(config_lines(device['name'])))
        return 0
    try:
        import netmiko
    except ImportError:
        print('Install dependencies: python -m pip install -r requirements.txt')
        return 2
    if ssh and any(d['kind'] == 'ios' for d in devices):
        ssh['password'] = getpass.getpass('SSH password (hidden): ')
        if not ssh['password']:
            parser.error('Password cannot be empty')
    stamp = datetime.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
    folder = BASE / 'runs' / stamp
    folder.mkdir(parents=True)
    stages = ['deploy', 'verify', 'backup'] if args.action == 'all' else [args.action]
    results = []
    for stage in stages:
        if stage == 'verify' and args.action == 'all':
            print('Waiting 45 seconds for OSPF and spanning tree...', flush=True)
            time.sleep(45)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            stage_results = list(pool.map(lambda d: run_device(host, d, stage, folder, ssh), devices))
        results.extend(stage_results)
        if stage == 'deploy' and not all(item['ok'] for item in stage_results):
            print('Deployment incomplete; remaining stages skipped. Check the logs.')
            break
    report = {'lab': inventory['lab'], 'eve_host': host,
              'time': datetime.datetime.now().astimezone().isoformat(), 'results': results}
    (folder / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    print('Results:', folder)
    return 0 if all(item['ok'] for item in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
