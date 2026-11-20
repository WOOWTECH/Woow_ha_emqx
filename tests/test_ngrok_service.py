"""Tests for the ngrok s6 service: it forwards the port EMQX's MQTT TCP
listener actually binds (issue 204: EMQX moved to 1885 next to Mosquitto).

The service script runs with plain bash, a stub bashio reading a temporary
options.json, the listener library at its repository path, and `ngrok`
replaced by a stub that prints its arguments.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok/run"
LIB = ROOT / "emqx/rootfs/usr/lib/woow-emqx/mqtt-listener.sh"

BASHIO_STUB = r"""
bashio::log.info() { echo "[INFO] $*"; }
bashio::log.warning() { echo "[WARNING] $*"; }
bashio::log.error() { echo "[ERROR] $*"; }
bashio::config() {
    case "$1" in
        'env_vars|keys') jq -r '.env_vars | keys[]' "${TEST_OPTIONS}" ;;
        env_vars\[*\].name) jq -r ".env_vars[${1//[^0-9]/}].name" "${TEST_OPTIONS}" ;;
        env_vars\[*\].value) jq -r ".env_vars[${1//[^0-9]/}].value" "${TEST_OPTIONS}" ;;
        *) jq -r --arg k "$1" '.[$k] // empty' "${TEST_OPTIONS}" ;;
    esac
}
bashio::config.true() { [[ "$(bashio::config "$1")" == "true" ]]; }
bashio::config.has_value() { [[ -n "$(bashio::config "$1")" ]]; }
"""


class NgrokHarness(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("jq") is None:
            self.skipTest("jq is required")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("ngrok", 'echo "NGROK $*"'), ("sleep", 'echo "IDLE"')):
            stub = bin_dir / name
            stub.write_text(f"#!/bin/bash\n{body}\n")
            stub.chmod(0o755)
        self.path = f"{bin_dir}:{os.environ['PATH']}"

    def run_service(self, env_vars=(), **options):
        opts = {"ngrok_enabled": True, "ngrok_authtoken": "tok", **options,
                "env_vars": [{"name": n, "value": v} for n, v in env_vars]}
        (self.tmp / "options.json").write_text(json.dumps(opts))
        script = RUN.read_text(encoding="utf-8")
        script = script.replace("#!/command/with-contenv bashio", "#!/bin/bash")
        script = script.replace("source /usr/lib/woow-emqx/mqtt-listener.sh",
                                BASHIO_STUB + f"\nsource {LIB}")
        path = self.tmp / "run"
        path.write_text(script)
        proc = subprocess.run(
            ["bash", str(path)],
            env={"PATH": self.path, "TEST_OPTIONS": str(self.tmp / "options.json")},
            capture_output=True, text=True, timeout=30,
        )
        return proc


class NgrokTargetTest(NgrokHarness):
    def ngrok_args(self, proc) -> str:
        lines = [l for l in proc.stdout.splitlines() if l.startswith("NGROK ")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return lines[0][len("NGROK "):]

    def test_default_listener_forwards_1883(self) -> None:
        self.assertEqual(self.ngrok_args(self.run_service()), "tcp 1883 --log stdout")

    def test_bare_port_is_followed(self) -> None:
        proc = self.run_service([("EMQX_LISTENERS__TCP__DEFAULT__BIND", "1885")])
        self.assertEqual(self.ngrok_args(proc), "tcp 1885 --log stdout")

    def test_wildcard_host_forms_forward_the_port(self) -> None:
        for bind in ("0.0.0.0:1885", ":1885", "[::]:1885", '"0.0.0.0:1885"', " 1885 "):
            with self.subTest(bind=bind):
                proc = self.run_service([("EMQX_LISTENERS__TCP__DEFAULT__BIND", bind)])
                self.assertEqual(self.ngrok_args(proc), "tcp 1885 --log stdout")

    def test_specific_host_is_kept(self) -> None:
        for bind, target in (("127.0.0.1:1885", "127.0.0.1:1885"), ("::1:1885", "[::1]:1885"),
                             ("[::1]:1885", "[::1]:1885")):
            with self.subTest(bind=bind):
                proc = self.run_service([("EMQX_LISTENERS__TCP__DEFAULT__BIND", bind)])
                self.assertEqual(self.ngrok_args(proc), f"tcp {target} --log stdout")

    def test_other_env_vars_do_not_matter_and_last_bind_wins(self) -> None:
        proc = self.run_service([
            ("EMQX_DASHBOARD__DEFAULT_PASSWORD", "x"),
            ("EMQX_LISTENERS__TCP__DEFAULT__BIND", "1884"),
            ("EMQX_LISTENERS__SSL__DEFAULT__ENABLE", "false"),
            ("EMQX_LISTENERS__TCP__DEFAULT__BIND", "1885"),
        ])
        self.assertEqual(self.ngrok_args(proc), "tcp 1885 --log stdout")

    def test_reserved_address_uses_the_moved_port(self) -> None:
        proc = self.run_service([("EMQX_LISTENERS__TCP__DEFAULT__BIND", "1885")],
                                ngrok_tcp_addr="1.tcp.ngrok.io:20000")
        self.assertEqual(self.ngrok_args(proc),
                         "tcp 1885 --remote-addr=1.tcp.ngrok.io:20000 --log stdout")

    def test_unreadable_bind_never_falls_back_to_1883(self) -> None:
        for bind in ("abc", "0.0.0.0:", "0.0.0.0:70000", "0"):
            with self.subTest(bind=bind):
                proc = self.run_service([("EMQX_LISTENERS__TCP__DEFAULT__BIND", bind)])
                self.assertNotIn("NGROK", proc.stdout)
                self.assertIn("IDLE", proc.stdout)
                self.assertIn("[ERROR]", proc.stdout)

    def test_disabled_ngrok_stays_idle(self) -> None:
        proc = self.run_service([("EMQX_LISTENERS__TCP__DEFAULT__BIND", "1885")], ngrok_enabled=False)
        self.assertNotIn("NGROK", proc.stdout)
        self.assertIn("IDLE", proc.stdout)


if __name__ == "__main__":
    unittest.main()
