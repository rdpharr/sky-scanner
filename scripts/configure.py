#!/usr/bin/env python3
"""Generate local credentials and a TLS certificate; never overwrite secrets."""
import argparse
import ipaddress
import os
from pathlib import Path
import secrets
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--bind-address', default='127.0.0.1')
parser.add_argument('--lan-subnet', default='192.168.1.0/24')
parser.add_argument('--serial', default='')
args = parser.parse_args()
ipaddress.ip_address(args.bind_address)
ipaddress.ip_network(args.lan_subnet)
root = Path(__file__).resolve().parents[1]
os.umask(0o077)
secret_dir = root / 'secrets'
secret_dir.mkdir(exist_ok=True, mode=0o700)
env = root / '.env'
if env.exists():
    raise SystemExit('.env already exists; preserve or edit it manually')
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes',
    '-keyout', str(secret_dir / 'server.key'), '-out', str(secret_dir / 'server.crt'),
    '-days', '365', '-subj', '/CN=sky-scanner', '-addext',
    f'subjectAltName=DNS:sky-scanner,DNS:db,IP:{args.bind_address}'], check=True,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# Individual files are mounted into the DB container. Parent directory stays 0700.
for name in ('server.key', 'server.crt'):
    (secret_dir / name).chmod(0o644)
env.write_text(f'DB_PASSWORD={secrets.token_urlsafe(32)}\n'
    f'EXPLORER_PASSWORD={secrets.token_urlsafe(32)}\n'
    f'DB_BIND_ADDRESS={args.bind_address}\nLAN_SUBNET={args.lan_subnet}\n'
    f'RECEIVER_ID=home-hackrf\nHACKRF_SERIAL={args.serial}\n')
print('Created .env and secrets/server.crt. Keep .env and server.key private.')
