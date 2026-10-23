"""Configure only our private Ubuntu fixture to use authenticated loopback SMTP.

QEMU's 10.0.2.2 gateway reaches this host's loopback listener. TLS verifies that
IP against a dedicated fixture CA; no mail is delivered over the Internet.
SMTP credentials and received messages are sealed with Windows-user DPAPI.
"""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".tmp/trial-ubuntu"


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    owner = load_helper("ubuntu_owner", ROOT / "scripts/acceptance/trial-ubuntu-first-owner.py")
    smtp = load_helper("acceptance_smtp", ROOT / "scripts/acceptance/trial-smtp-tls.py")
    ssh = load_helper("ubuntu_ssh", ROOT / "scripts/acceptance/trial-ubuntu-ssh.py")
    messages = []
    lock = threading.Lock()

    def received(body):
        with lock:
            if len(messages) >= 100:
                raise ValueError("Independent acceptance mailbox is full")
            messages.append(base64.b64encode(body).decode("ascii"))
            path = WORK / "smtp-messages.dpapi"
            pending = path.with_suffix(".pending")
            pending.write_bytes(owner.protect(json.dumps(messages).encode()))
            pending.replace(path)

    with tempfile.TemporaryDirectory(prefix="ubuntu-smtp-", dir=WORK) as directory:
        with smtp.LocalSmtp(Path(directory), ip_sans=("10.0.2.2",), on_message=received) as fixture:
            settings = fixture.settings()
            settings["smtp_host"] = "10.0.2.2"
            payload = {"settings": settings, "ca": fixture.ca_path.read_text()}
            client = ssh.connect()
            try:
                stdin, stdout, stderr = client.exec_command("sudo python3 -", timeout=60)
                # Sent directly through encrypted SSH stdin, never argv or stdout.
                script = """import json,os
from pathlib import Path
value=json.loads(PAYLOAD)
state=Path('/opt/metahub-trial-state')
assert (state/'current-release').is_file()
assert (state/'current-release').read_text().strip().startswith('/opt/metahub-candidate-20261015-')
assert 'METAHUB_DOMAIN=trial.local' in (state/'compose.env').read_text()
runtime=state/'backend/runtime.env'
lines=runtime.read_text().splitlines()
mapping={('METAHUB_'+key.upper()):str(item) for key,item in value['settings'].items() if key!='environment'}
lines=[line for line in lines if line.split('=',1)[0] not in mapping]
pending=runtime.with_suffix('.pending')
pending.write_text('\\n'.join(lines+[key+'='+item for key,item in mapping.items()])+'\\n')
pending.chmod(0o400)
os.chown(pending,10001,10001)
pending.replace(runtime)
ca=state/'backend/tls/ca.crt'
ca.write_text(ca.read_text().rstrip()+'\\n'+value['ca'])
ca.chmod(0o444)
print(json.dumps({'status':'fixture_smtp_configured','existing_encryption_roots_preserved':True}))
""".replace("PAYLOAD", repr(json.dumps(payload)))
                stdin.write(script)
                stdin.flush()
                stdin.channel.shutdown_write()
                status = stdout.channel.recv_exit_status()
                if status != 0:
                    raise ValueError("Independent Ubuntu SMTP configuration was refused")
            finally:
                client.close()
            (WORK / "smtp-current.dpapi").write_bytes(owner.protect(json.dumps(settings).encode()))
            received_bytes = len(messages)
            print(json.dumps({"status": "ready", "listener": "127.0.0.1", "port": fixture.port,
                              "guest_host": "10.0.2.2", "security": "starttls", "public_delivery": False,
                              "messages_received": received_bytes}), flush=True)
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass


if __name__ == "__main__":
    main()
