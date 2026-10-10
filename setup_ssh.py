"""One-time SSH bootstrap through the existing EVE console connections."""
import argparse
import datetime
import getpass
import ipaddress
import json
import re
import socket
import sys
import time

import lab  # Also locates this computer's prepared Python dependencies.
from ssh_support import (INVENTORY, MGMT_PORTS, connection_args, console_public_keys,
                         fingerprint, interface_ip, pin_verified_key,
                         DISABLED_ALGORITHMS, require_compatible_paramiko,
                         verify_identity, rsa_setup_commands, named_public_keys)

ERROR = r'% (?:Invalid input|Incomplete command|Ambiguous command|Error)'


def configure(conn, commands):
    # Do not enable a session log: these commands include the local password.
    return conn.send_config_set(commands, cmd_verify=False, error_pattern=ERROR, read_timeout=120)


def bootstrap(device, eve_host, host_ip, username, password, outdir):
    from netmiko import ConnectHandler
    import paramiko
    name = device['name']
    port = MGMT_PORTS[name]
    print(f'{name}: configuring management and SSH...', flush=True)
    with ConnectHandler(device_type='cisco_ios_telnet', host=eve_host, port=device['port'],
                        username='', password='', secret='', conn_timeout=15,
                        auth_timeout=30, fast_cli=False) as conn:
        identity = verify_identity(conn, name)
        (outdir / f'{name}-identity.txt').write_text(identity, encoding='utf-8')
        running = conn.send_command('show running-config', read_timeout=60)
        before_keys = conn.send_command('show crypto key mypubkey rsa', read_timeout=30)
        (outdir / f'{name}-public-key-before.txt').write_text(before_keys, encoding='utf-8')
        key_commands = rsa_setup_commands(before_keys, device['role'])
        commands = ['ip domain-name automation.lab',
                    f'username {username} privilege 15 secret {password}',
                    'ip access-list standard LAB_SSH_HOST', 'no 10',
                    f'10 permit host {host_ip}', '20 deny any', 'exit']
        if device['role'] == 'switch':
            commands += ['vlan 99', 'name SSH_MGMT', 'exit', f'interface {port}',
                         'description SSH_TO_CLOUD0', 'switchport mode access',
                         'switchport access vlan 99', 'spanning-tree portfast', 'no shutdown',
                         'exit', 'interface Vlan99', 'description SSH_MANAGEMENT',
                         'ip address dhcp', 'no shutdown', 'exit']
            mgmt_interface = 'Vlan99'
        else:
            commands += [f'interface {port}', 'description SSH_TO_CLOUD0',
                         'ip address dhcp', 'no shutdown', 'exit']
            mgmt_interface = port
        configure(conn, commands)
        if key_commands:
            configure(conn, key_commands)
        commands = ['ip ssh version 2', 'ip ssh time-out 60', 'ip ssh authentication-retries 3']
        vty_lines = re.findall(r'^line vty \d+(?: \d+)?', running, re.M) or ['line vty 0 4']
        for line in vty_lines:
            commands += [line, 'login local', 'transport input ssh',
                         'access-class LAB_SSH_HOST in', 'exec-timeout 15 0', 'exit']
        configure(conn, commands)
        conn.save_config()
        deadline = time.monotonic() + 120
        address = None
        while time.monotonic() < deadline:
            brief = conn.send_command('show ip interface brief', read_timeout=30)
            address = interface_ip(brief, mgmt_interface)
            if address:
                break
            time.sleep(3)
        (outdir / f'{name}-interfaces.txt').write_text(brief, encoding='utf-8')
        if not address:
            raise RuntimeError(f'No DHCP address on {mgmt_interface}. Check Cloud0 cable and VMware VMnet8 DHCP.')
        if ipaddress.ip_address(address) not in ipaddress.ip_network(host_ip + '/24', strict=False):
            raise RuntimeError('Management DHCP address is outside this lab VMnet8 /24: ' + address)
        public_text = conn.send_command('show crypto key mypubkey rsa', read_timeout=30)
        (outdir / f'{name}-public-key.txt').write_text(public_text, encoding='utf-8')
        keys = (named_public_keys(public_text, 'LAB_AUTOMATION_SSH')
                if device['role'] == 'switch' else console_public_keys(public_text))
        if not keys:
            raise RuntimeError('Could not parse a strong RSA public key from console. SSH key was NOT trusted.')
        ssh_status = conn.send_command('show ip ssh', read_timeout=30)
        (outdir / f'{name}-ssh-status.txt').write_text(ssh_status, encoding='utf-8')
        if not re.search(r'SSH Enabled\s*-\s*version 2', ssh_status, re.I):
            raise RuntimeError('Device did not report SSH version 2 enabled.')
    # Handshake only: compare public keys BEFORE transmitting the password.
    with socket.create_connection((address, 22), timeout=15) as sock:
        transport = paramiko.Transport(sock, disabled_algorithms=DISABLED_ALGORITHMS)
        try:
            transport.start_client(timeout=30)
            key = transport.get_remote_server_key()
            pin_verified_key(address, key, keys)
            key_fp = fingerprint(key)
        finally:
            transport.close()
    with ConnectHandler(**connection_args(address, username, password)) as ssh:
        verify_identity(ssh, name)
        if not ssh.check_enable_mode():
            raise RuntimeError('SSH login succeeded but privilege was incorrect.')
        text = ssh.send_command('show ip interface brief', read_timeout=30)
        (outdir / f'{name}-ssh-proof.txt').write_text(text, encoding='utf-8')
    print(f'{name}: SSH PASS {address}  {key_fp}', flush=True)
    return {'host': address, 'port': 22, 'interface': mgmt_interface, 'fingerprint': key_fp}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-ip', default='192.168.126.1', help='Windows VMnet8 IPv4 address')
    parser.add_argument('--username', default='netadmin')
    args = parser.parse_args()
    try:
        require_compatible_paramiko()
    except RuntimeError as error:
        print(error)
        return 2
    ipaddress.IPv4Address(args.host_ip)
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', args.username):
        parser.error('Use a simple IOS username: letters, digits, dash, underscore.')
    if not sys.stdin.isatty():
        parser.error('Run BAT_SSH.cmd in an interactive terminal; do not pass passwords through files.')
    print('Keep all six nodes running. Close their interactive consoles.\n'
          'This configures SSH on R1/R2/R3/SW1/SW2; PC1 keeps using its console.\n'
          f'SSH username: {args.username}; allowed Windows address: {args.host_ip}\n'
          'Password is entered locally, hidden, and is not saved in project files.\n'
          'Use 12-64 characters: letters, numbers and !@#$%^&*_.+=:-\n')
    password = getpass.getpass('New SSH password: ')
    if not re.fullmatch(r'[A-Za-z0-9!@#$%^&*_.+=:\-]{12,64}', password):
        print('Password must meet the character and length rules above. No changes made.')
        return 2
    if password != getpass.getpass('Repeat password: '):
        print('Passwords do not match. No changes made.')
        return 2
    inventory = json.loads((lab.BASE / 'inventory.json').read_text())
    stamp = datetime.datetime.now().astimezone().strftime('%Y%m%d-%H%M%S-%f')
    outdir = lab.BASE / 'runs' / ('ssh-setup-' + stamp)
    outdir.mkdir(parents=True)
    state = {'username': args.username, 'allowed_host': args.host_ip, 'devices': {}}
    # Rebuild discovery from successful logins; failed devices cannot retain stale entries.
    results = []
    for device in inventory['devices']:
        if device['kind'] != 'ios':
            continue
        name = device['name']
        try:
            state['devices'][name] = bootstrap(device, inventory['eve_host'], args.host_ip,
                                               args.username, password, outdir)
            result = {'device': name, 'ok': True, **state['devices'][name]}
        except Exception as error:
            message = str(error).replace(password, '[REDACTED]')
            print(f'{name}: SSH FAIL: {message}', flush=True)
            result = {'device': name, 'ok': False, 'error': message}
        results.append(result)
        INVENTORY.write_text(json.dumps(state, indent=2), encoding='utf-8')
        (outdir / 'report.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('\nSetup report:', outdir)
    if not all(r['ok'] for r in results):
        print('Setup incomplete. Inspect report.json; console access is still available.')
        return 1
    print('\nChecking the complete lab: 5 Cisco nodes over SSH, PC1 over console...', flush=True)
    auth = {**state, 'password': password}
    checks = [lab.run_device(inventory['eve_host'], d, 'verify', outdir, auth)
              for d in inventory['devices']]
    (outdir / 'verify-report.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
    if not all(r['ok'] for r in checks):
        print('SSH setup passed; some network checks failed. See verify-report.json.')
        return 1
    print('\nALL PASS. Use KIEM_TRA_SSH.cmd next time.\nSSH addresses:')
    for name, item in state['devices'].items():
        print(f"  {name}: ssh {args.username}@{item['host']}")
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled. Run BAT_SSH.cmd again to complete setup.')
        raise SystemExit(130)
