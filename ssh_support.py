"""SSH inventory and host-key verification for this lab. No stored passwords."""
import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent
KNOWN_HOSTS = BASE / 'known_hosts.local'
INVENTORY = BASE / 'inventory.ssh.local.json'
MGMT_PORTS = {'R1': 'Ethernet0/2', 'R2': 'Ethernet0/2', 'R3': 'Ethernet0/1',
              'SW1': 'Ethernet0/2', 'SW2': 'Ethernet0/2'}

# IOS in this lab advertises group14-sha1 (2048-bit) and ssh-rsa.
# Prefer stronger methods when available; never fall back to 1024-bit group1.
DISABLED_ALGORITHMS = {
    'kex': ['diffie-hellman-group1-sha1', 'diffie-hellman-group-exchange-sha1'],
    'ciphers': ['3des-cbc', 'aes128-cbc', 'aes192-cbc', 'aes256-cbc'],
    'macs': ['hmac-md5', 'hmac-md5-96', 'hmac-sha1-96'],
}


def require_compatible_paramiko():
    import paramiko
    if paramiko.__version__ != '4.0.0':
        raise RuntimeError('This IOS lab requires paramiko==4.0.0; installed: '
                           + paramiko.__version__ + '. Install requirements.txt and retry.')


def connection_args(host, username, password):
    require_compatible_paramiko()
    return dict(device_type='cisco_ios', host=host, port=22, username=username,
                password=password, secret='', ssh_strict=True, system_host_keys=False,
                alt_host_keys=True, alt_key_file=str(KNOWN_HOSTS), use_keys=False,
                allow_agent=False, conn_timeout=15, auth_timeout=30, banner_timeout=30,
                fast_cli=False, disabled_algorithms=DISABLED_ALGORITHMS)


def load_inventory(names):
    if not INVENTORY.exists():
        raise ValueError('Run BAT_SSH.cmd first to configure SSH and discover addresses.')
    data = json.loads(INVENTORY.read_text(encoding='utf-8'))
    missing = set(names) - set(data.get('devices', {}))
    if missing:
        raise ValueError('SSH setup incomplete for: ' + ', '.join(sorted(missing)))
    if not KNOWN_HOSTS.exists():
        raise ValueError('Missing known_hosts.local. Run BAT_SSH.cmd again.')
    return data


def interface_ip(text, interface):
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[0].lower() == interface.lower() and fields[-2:] == ['up', 'up']:
            try:
                return str(ipaddress.IPv4Address(fields[1]))
            except ipaddress.AddressValueError:
                pass
    return None


def verify_identity(conn, expected):
    """Read configured hostname rather than comparing potentially noisy prompts."""
    if conn.check_config_mode():
        conn.exit_config_mode()
    conn.enable()
    text = conn.send_command('show running-config | include ^hostname', read_timeout=30)
    names = re.findall(r'^\s*hostname\s+(\S+)\s*$', text, re.M)
    if names != [expected]:
        raise RuntimeError(f'Device identity mismatch: expected {expected}, read {names!r}. No configuration applied.')
    return text


def named_public_keys(text, name):
    for block in re.split(r'(?=^Key name:)', text, flags=re.M):
        match = re.match(r'Key name:\s*([^\r\n]+)', block)
        if match and match.group(1).strip() == name:
            return console_public_keys(block)
    return set()


def rsa_setup_commands(text, role):
    """Preserve existing keys; switches get an explicitly selected SSH key."""
    if role == 'switch':
        label = 'LAB_AUTOMATION_SSH'
        named = named_public_keys(text, label)
        names = re.findall(r'^Key name:\s*([^\r\n]+)', text, re.M)
        if label in [n.strip() for n in names] and not named:
            raise RuntimeError('LAB_AUTOMATION_SSH exists but is not a verified 2048-bit key; not replacing it.')
        commands = [] if named else [f'crypto key generate rsa general-keys label {label} modulus 2048']
        return commands + [f'ip ssh rsa keypair-name {label}']
    if console_public_keys(text):
        return []
    if 'Key name:' in text:
        raise RuntimeError('Router RSA keys could not be verified; keys were not replaced.')
    return ['crypto key generate rsa general-keys modulus 2048']


def console_public_keys(text):
    """Decode Cisco 'Key Data' hex DER into comparable SSH public-key blobs."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    keys = set()
    for block in re.split(r'Key Data\s*:', text, flags=re.I)[1:]:
        chunks = []
        for line in block.splitlines():
            line = line.strip()
            if re.fullmatch(r'(?:[0-9A-Fa-f]{2,}\s*)+', line):
                chunks.append(re.sub(r'\s+', '', line))
            elif chunks:
                break
        if not chunks:
            continue
        try:
            key = serialization.load_der_public_key(bytes.fromhex(''.join(chunks)))
            if isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048:
                openssh = key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
                keys.add(base64.b64decode(openssh.split()[1]))
        except (ValueError, TypeError):
            continue
    return keys


def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


def pin_verified_key(host, remote_key, console_keys):
    """Never trust a key solely because it was presented by an SSH endpoint."""
    import paramiko
    if remote_key.asbytes() not in console_keys:
        raise ValueError('SSH host key does not match the public key read from this node console.')
    known = paramiko.HostKeys()
    if KNOWN_HOSTS.exists():
        known.load(str(KNOWN_HOSTS))
    known.add(host, remote_key.get_name(), remote_key)
    known.save(str(KNOWN_HOSTS))
