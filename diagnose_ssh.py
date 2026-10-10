"""Read only SSH diagnostics; no passwords or full configurations collected."""
import datetime
import json
import lab
from netmiko import ConnectHandler


def main():
    inventory = json.loads((lab.BASE / 'inventory.json').read_text())
    folder = lab.BASE / 'runs' / ('ssh-diagnose-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    folder.mkdir(parents=True)
    for device in inventory['devices']:
        if device['name'] not in ('R2', 'SW1', 'SW2'):
            continue
        name = device['name']
        lines = []
        try:
            with ConnectHandler(device_type='cisco_ios_telnet', host=inventory['eve_host'],
                                port=device['port'], username='', password='', secret='',
                                conn_timeout=10, auth_timeout=30, fast_cli=False) as conn:
                lines.append('initial prompt: ' + repr(conn.find_prompt()))
                if conn.check_config_mode():
                    conn.exit_config_mode()
                conn.enable()
                for command in ('show running-config | include ^hostname',
                                'show ip interface brief', 'show crypto key mypubkey rsa', 'show ip ssh'):
                    lines.append(command + '\n' + conn.send_command(command, read_timeout=60))
            print(name + ': diagnostics saved', flush=True)
        except Exception as error:
            lines.append(str(error))
            print(name + ': ' + str(error), flush=True)
        (folder / (name + '.txt')).write_text('\n\n'.join(lines), encoding='utf-8')
    print('Diagnostics:', folder)


if __name__ == '__main__':
    main()
