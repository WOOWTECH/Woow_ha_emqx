"""Tests for the emqx s6 service: node name, Erlang cookie, log masking.

The service script runs under bashio inside the add-on container. Here it is
run with plain bash, a stub bashio and its absolute paths redirected into a
temporary directory, and `emqx foreground` replaced by a dump of its
environment.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "emqx/rootfs/etc/s6-overlay/s6-rc.d/emqx/run"
LIB = ROOT / "emqx/rootfs/usr/lib/woow-emqx/node-identity.sh"

BASHIO_STUB = r"""
bashio::log.info() { echo "[INFO] $*"; }
bashio::log.warning() { echo "[WARNING] $*"; }
bashio::log.error() { echo "[ERROR] $*"; }
bashio::exit.nok() { echo "[FATAL] $*"; exit 1; }
bashio::info.hostname() {
    [[ -n "${TEST_HOSTNAME:-}" ]] || return 1
    echo "${TEST_HOSTNAME}"
}
bashio::config() {
    case "$1" in
        'env_vars|keys') jq -r '.env_vars | keys[]' "${TEST_OPTIONS}" ;;
        env_vars\[*\].name) jq -r ".env_vars[${1//[^0-9]/}].name" "${TEST_OPTIONS}" ;;
        env_vars\[*\].value) jq -r ".env_vars[${1//[^0-9]/}].value" "${TEST_OPTIONS}" ;;
        *) return 1 ;;
    esac
}
"""


class ServiceHarness(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("jq") is None:
            self.skipTest("jq is required")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = self.tmp / "data/emqx"
        self.state.mkdir(parents=True)
        self.hosts = self.tmp / "hosts"
        self.hosts.write_text("127.0.0.1\tlocalhost\n")
        self.options = self.tmp / "options.json"
        self.set_env_vars([])

    def set_env_vars(self, pairs: list[tuple[str, str]]) -> None:
        self.options.write_text(
            json.dumps({"env_vars": [{"name": n, "value": v} for n, v in pairs]})
        )

    def make_db(self, *names: str) -> None:
        for name in names:
            (self.state / "data/mnesia" / name).mkdir(parents=True)

    def env(self, hostname: str | None = "homeassistant") -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "TEST_OPTIONS": str(self.options),
            "WOOW_EMQX_HOSTS_FILE": str(self.hosts),
        }
        if hostname is not None:
            env["TEST_HOSTNAME"] = hostname
        return env

    def run_service(self, hostname: str | None = "homeassistant"):
        script = RUN.read_text(encoding="utf-8")
        script = script.replace("#!/command/with-contenv bashio", "#!/bin/bash")
        script = script.replace("/usr/lib/woow-emqx/node-identity.sh", str(LIB))
        script = script.replace("/data/emqx", str(self.state))
        script = script.replace(
            "exec /opt/emqx/bin/emqx foreground", 'echo "---ENV---"; env'
        )
        script = script.replace("source ", BASHIO_STUB + "\nsource ", 1)
        path = self.tmp / "run"
        path.write_text(script)
        proc = subprocess.run(
            ["bash", str(path)],
            env={**self.env(hostname), "WOOW_EMQX_STATE_DIR": str(self.state)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        log, _, dump = proc.stdout.partition("---ENV---\n")
        exported = dict(
            line.split("=", 1) for line in dump.splitlines() if "=" in line
        )
        return proc, log + proc.stderr, exported


class NodeNameTest(ServiceHarness):
    def test_existing_install_keeps_its_database_name(self) -> None:
        self.make_db("emqx@homeassistant.local")
        proc, _, exported = self.run_service(hostname="homeassistant")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@homeassistant.local")
        self.assertEqual(
            (self.state / "node.name").read_text(), "emqx@homeassistant.local"
        )

    def test_renamed_host_still_finds_the_old_database(self) -> None:
        self.make_db("emqx@homeassistant.local")
        _, _, exported = self.run_service(hostname="kitchen")
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@homeassistant.local")

    def test_pinned_name_survives_hostname_change(self) -> None:
        self.make_db("emqx@homeassistant.local")
        self.run_service(hostname="homeassistant")
        # A second database appears (e.g. an old backup); the pin wins.
        self.make_db("emqx@other.local")
        _, _, exported = self.run_service(hostname="other")
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@homeassistant.local")

    def test_fresh_install_uses_loopback_name(self) -> None:
        _, _, exported = self.run_service(hostname="homeassistant")
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@127.0.0.1")
        self.assertEqual((self.state / "node.name").read_text(), "emqx@127.0.0.1")

    def test_several_databases_keep_the_5_9_0_choice(self) -> None:
        self.make_db("emqx@homeassistant.local", "emqx@old.local")
        _, log, exported = self.run_service(hostname="homeassistant")
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@homeassistant.local")
        self.assertIn("Found 2 EMQX databases", log)

    def test_several_databases_without_hostname_fall_back(self) -> None:
        self.make_db("emqx@a.local", "emqx@b.local")
        _, _, exported = self.run_service(hostname=None)
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@127.0.0.1")

    def test_env_var_override_wins_and_is_pinned(self) -> None:
        self.make_db("emqx@homeassistant.local")
        self.set_env_vars([("EMQX_NODE__NAME", "emqx@custom.local")])
        _, _, exported = self.run_service()
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@custom.local")
        self.assertEqual((self.state / "node.name").read_text(), "emqx@custom.local")

    def test_node_host_is_mapped_once(self) -> None:
        self.make_db("emqx@homeassistant.local")
        self.run_service()
        self.run_service()
        lines = [
            line for line in self.hosts.read_text().splitlines()
            if "homeassistant.local" in line
        ]
        self.assertEqual(lines, ["127.0.0.1\thomeassistant.local"])

    def test_ip_node_name_is_not_mapped(self) -> None:
        self.run_service()
        self.assertEqual(self.hosts.read_text(), "127.0.0.1\tlocalhost\n")


class CookieTest(ServiceHarness):
    def test_cookie_is_random_private_and_stable(self) -> None:
        proc, _, first = self.run_service()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cookie_file = self.state / "node.cookie"
        cookie = first["EMQX_NODE__COOKIE"]
        self.assertRegex(cookie, r"^[0-9a-f]{64}$")
        self.assertNotIn("homeassistant", cookie)
        self.assertEqual(cookie_file.read_text(), cookie)
        self.assertEqual(stat.S_IMODE(cookie_file.stat().st_mode), 0o600)

        _, _, second = self.run_service()
        self.assertEqual(second["EMQX_NODE__COOKIE"], cookie)

    def test_each_install_gets_its_own_cookie(self) -> None:
        _, _, first = self.run_service()
        (self.state / "node.cookie").unlink()
        _, _, second = self.run_service()
        self.assertNotEqual(first["EMQX_NODE__COOKIE"], second["EMQX_NODE__COOKIE"])

    def test_cookie_permissions_are_repaired(self) -> None:
        cookie_file = self.state / "node.cookie"
        cookie_file.write_text("a" * 64)
        cookie_file.chmod(0o644)
        _, _, exported = self.run_service()
        self.assertEqual(exported["EMQX_NODE__COOKIE"], "a" * 64)
        self.assertEqual(stat.S_IMODE(cookie_file.stat().st_mode), 0o600)

    def test_cookie_never_reaches_the_log(self) -> None:
        _, log, exported = self.run_service()
        self.assertNotIn(exported["EMQX_NODE__COOKIE"], log)

    def test_upgrade_keeps_database_with_new_cookie(self) -> None:
        # 5.9.0 used cookie == hostname; the database is keyed by node name only.
        self.make_db("emqx@homeassistant.local")
        _, _, exported = self.run_service()
        self.assertEqual(exported["EMQX_NODE__NAME"], "emqx@homeassistant.local")
        self.assertNotEqual(exported["EMQX_NODE__COOKIE"], "homeassistant")


class EnvVarLogTest(ServiceHarness):
    def test_values_are_redacted(self) -> None:
        secret = "s3cr3t-Pa55word"
        self.set_env_vars(
            [("EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__SALT", secret)]
        )
        proc, log, exported = self.run_service()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(secret, log)
        self.assertIn(
            "Setting EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__SALT to [REDACTED]",
            log,
        )
        self.assertEqual(
            exported["EMQX_AUTHENTICATION__1__PASSWORD_HASH_ALGORITHM__SALT"], secret
        )

    def test_fixed_settings_are_kept(self) -> None:
        _, _, exported = self.run_service()
        self.assertEqual(exported["EMQX_NODE__DATA_DIR"], f"{self.state}/data")
        self.assertEqual(exported["EMQX_NODE__ETC_DIR"], f"{self.state}/etc")
        self.assertEqual(exported["EMQX_PLUGINS__INSTALL_DIR"], f"{self.state}/plugins")
        self.assertEqual(exported["EMQX_RPC__PORT_DISCOVERY"], "manual")
        self.assertEqual(exported["EMQX_LOG_DIR"], "/config/log")


class StaticTest(unittest.TestCase):
    def test_run_script_is_executable(self) -> None:
        self.assertTrue(os.access(RUN, os.X_OK))

    def test_scripts_parse(self) -> None:
        for path in (RUN, LIB):
            subprocess.run(["bash", "-n", str(path)], check=True)


class AddonConfigTest(unittest.TestCase):
    def test_watchdog_points_at_status_api(self) -> None:
        import yaml

        config = yaml.safe_load((ROOT / "emqx/config.yaml").read_text())
        self.assertTrue(config["host_network"])
        self.assertEqual(config["watchdog"], "http://[HOST]:[PORT:18083]/api/v5/status")


if __name__ == "__main__":
    unittest.main()
