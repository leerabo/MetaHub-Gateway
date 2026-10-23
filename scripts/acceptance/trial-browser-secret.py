"""Use our DPAPI-sealed fixture credentials without printing them in CLI input.

This one-use loopback broker is acceptance tooling, never production code. Run it
only after inspecting a fresh browser snapshot of the task-owned Ubuntu login.
The generated Playwright CLI file contains a consumed nonce, not credentials.
"""
from __future__ import annotations

import argparse
import base64
from email import policy
from email.parser import BytesParser
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import secrets
import re
import socket
import subprocess
import threading
import time

ROOT = Path(__file__).resolve().parents[2]


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", choices=("login", "platform-mfa", "registration", "verify-email"), required=True)
    parser.add_argument("--session", choices=("metahub-ubuntu-trial", "metahub-ubuntu-workspace"),
                        default="metahub-ubuntu-trial")
    parser.add_argument("--submit-browser", action="store_true",
                        help="Launch the existing private Playwright CLI session while the broker is live")
    parser.add_argument("--credentials-name", default="first-owner-credentials.dpapi",
                        help="Existing sealed own-account material; never create or replace it")
    args = parser.parse_args()
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+[.]dpapi", args.credentials_name)
            or not (ROOT / '.tmp/trial-ubuntu' / args.credentials_name).is_file()):
        raise ValueError("Use existing task-owned sealed credentials")
    owner = load_helper("ubuntu_owner", ROOT / "scripts/acceptance/trial-ubuntu-first-owner.py")
    owner.CREDENTIALS = ROOT / '.tmp/trial-ubuntu' / args.credentials_name
    credentials = owner.load_credentials()
    if credentials.get("status") != "initialized":
        raise ValueError("Independent Ubuntu fixture owner is not initialized")
    if args.step == "login":
        payload = {"email": credentials["email"], "password": credentials["password"]}
        action = """
  const form = page.locator('[data-identity-login-form]');
  await form.locator('input[name="email"]').fill(value.email);
  await form.locator('input[name="password"]').fill(value.password);
  await form.locator('button[type="submit"]').click();
"""
    elif args.step == "registration":
        # The existing-account registration flow proves the unchanged password
        # before verifying email; it does not reset this fixture's credential.
        payload = {"email": credentials["email"], "password": credentials["password"],
                   "displayName": credentials["name"]}
        action = """
  const form = page.locator('[data-identity-registration-form]');
  await form.locator('input[name="email"]').fill(value.email);
  await form.locator('input[name="displayName"]').fill(value.displayName);
  await form.locator('input[name="password"]').fill(value.password);
  await form.locator('input[name="confirmPassword"]').fill(value.password);
  await form.locator('input[name="consent"]').check();
  const captcha = form.locator('input[name="captchaAnswer"]');
  if (await captcha.count() && !(await captcha.inputValue())) {
    return {step: 'registration', filled: true, needs_visual_captcha: true};
  }
  await form.locator('button[type="submit"]').click();
"""
    elif args.step == "verify-email":
        mailbox = ROOT / '.tmp/trial-ubuntu/smtp-messages.dpapi'
        if not mailbox.is_file() or time.time() - mailbox.stat().st_mtime > 300:
            raise ValueError("No recent independently received SMTP message")
        messages = json.loads(owner.protect(mailbox.read_bytes(), decrypt=True))
        code = None
        for encoded in reversed(messages):
            message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(encoded))
            recipients = [address.addr_spec for address in message['to'].addresses]
            if recipients != [credentials['email']]:
                continue
            body = message.get_body(preferencelist=('plain',))
            if body is None:
                continue
            match = re.search(r'Your MetaHub verification code:\s*([0-9]{6})(?![0-9])', body.get_content())
            if match:
                code = match[1]
                break
        if code is None:
            raise ValueError("No own-account verification message found")
        payload = {"code": code}
        action = """
  const form = page.locator('[data-identity-verification-form]');
  await form.locator('input[name="code"]').fill(value.code);
  await form.locator('button[type="submit"]').click();
"""
    else:
        ssh = load_helper("ubuntu_ssh", ROOT / "scripts/acceptance/trial-ubuntu-ssh.py")
        client = ssh.connect()
        try:
            _, clock, _ = client.exec_command("date +%s", timeout=30)
            epoch = int(clock.read().strip())
            if clock.channel.recv_exit_status() != 0:
                raise ValueError("Cannot read fixture authenticator clock")
        finally:
            client.close()
        # Generate when consumed, not while the browser command is still being
        # scheduled. A slow CLI launch must not submit an already expired code.
        clock_received = time.monotonic()
        totp_secret = credentials["totp_secret"]
        payload = {}
        action = """
  const form = page.locator('[data-platform-session-form]');
  await form.locator('input[name="totpCode"]').fill(value.code);
  await form.locator('button[type="submit"]').click();
"""
    credentials.clear()
    nonce = secrets.token_urlsafe(48)
    path = "/" + nonce
    lifetime = 180 if args.submit_browser else 90
    expires = time.monotonic() + lifetime
    consumed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, *_):
            pass

        def do_GET(self):
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if (self.headers.get("Host") != expected_host or self.headers.get("Origin")
                    or time.monotonic() > expires or consumed.is_set()
                    or not hmac.compare_digest(self.path, path)):
                self.send_error(404)
                return
            consumed.set()
            if args.step == "platform-mfa":
                current_epoch = epoch + time.monotonic() - clock_received
                payload["code"] = owner.totp(totp_secret, current_epoch)
            body = json.dumps(payload).encode()
            payload.clear()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        server.timeout = 1
        # Bound every local connection so an abandoned client cannot outlive the
        # broker's expiry. No request body or credentials are written to logs.
        server.socket.settimeout(1)
        url = f"http://127.0.0.1:{server.server_port}{path}"
        code = """async (page) => {
  const target = new URL(page.url());
  if (target.origin !== 'https://trial.local:8446' || target.pathname !== EXPECTED_PATH) {
    throw new Error('Refusing credentials outside the independent Ubuntu fixture');
  }
  const response = await page.request.get(BROKER_URL, { timeout: 10000 });
  if (!response.ok()) throw new Error('One-use acceptance broker unavailable');
  const value = await response.json();
  await response.dispose();
ACTION
  return { step: STEP, submitted: true };
}
""".replace("BROKER_URL", json.dumps(url)).replace("ACTION", action).replace("STEP", json.dumps(args.step)).replace(
            "EXPECTED_PATH", json.dumps("/platform.html" if args.step == "platform-mfa" else "/identity.html"))
        script = ROOT / ".tmp" / f"pw-ubuntu-{args.session}-{args.step}-private.js"
        script.write_text(code, encoding="utf-8")
        print(json.dumps({"status": "ready", "script": str(script), "expires_seconds": lifetime,
                          "credentials_printed": False}), flush=True)
        def serve():
            while not consumed.is_set() and time.monotonic() < expires:
                try:
                    server.handle_request()
                except (TimeoutError, socket.timeout):
                    continue
        if args.submit_browser:
            node = Path("C:/Program Files/nodejs/node.exe")
            npx = node.parent / "node_modules/npm/bin/npx-cli.js"
            if not node.is_file() or not npx.is_file():
                raise ValueError("Private CLI runtime unavailable")
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            result = subprocess.run(
                [str(node), str(npx), "--offline", "--yes", "--package", "@playwright/cli", "playwright-cli",
                 "-s=" + args.session, "run-code", "--filename", str(script)],
                cwd=ROOT / "output/playwright/trial-ubuntu", timeout=lifetime,
            )
            worker.join(timeout=3)
            if result.returncode:
                payload.clear()
                return result.returncode
        else:
            serve()
        payload.clear()
        print(json.dumps({"status": "consumed" if consumed.is_set() else "expired"}), flush=True)
        return 0 if consumed.is_set() else 1


if __name__ == "__main__":
    raise SystemExit(main())
