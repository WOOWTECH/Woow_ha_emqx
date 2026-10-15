"""Unit tests for .github/scripts/check_release.py (the CI release guard)."""

from __future__ import annotations

import importlib.util
import re
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "emqx"

_spec = importlib.util.spec_from_file_location(
    "check_release", ROOT / ".github" / "scripts" / "check_release.py"
)
check_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_release)


class ReleaseGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        shutil.copytree(ADDON, self.tmp / "emqx")
        shutil.copy(ROOT / "README.md", self.tmp / "README.md")
        self.addon = self.tmp / "emqx"

    def edit(self, name: str, old: str, new: str, count: int = 1) -> None:
        path = self.addon / name
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"{old!r} not in {name}")
        path.write_text(text.replace(old, new, count), encoding="utf-8")

    def sub(self, name: str, pattern: str, new: str) -> None:
        path = self.addon / name
        text, n = re.subn(
            pattern, lambda _: new, path.read_text(encoding="utf-8"), flags=re.M
        )
        self.assertGreater(n, 0, f"{pattern!r} not in {name}")
        path.write_text(text, encoding="utf-8")

    def assertRejected(self, needle: str) -> None:
        errors = check_release.check(self.addon)
        self.assertTrue(
            any(needle in error for error in errors),
            f"expected an error containing {needle!r}, got {errors}",
        )

    # --- the repository as committed -----------------------------------
    def test_repository_passes(self) -> None:
        self.assertEqual(check_release.check(ADDON), [])

    # --- broker version guard -------------------------------------------
    def test_rejects_emqx_5_9(self) -> None:
        self.sub("Dockerfile", r'^ARG EMQX_VERSION=.*$', 'ARG EMQX_VERSION="v5.9.0"')
        self.assertRejected("is not v5.8.x")

    def test_rejects_emqx_6(self) -> None:
        self.sub("Dockerfile", r'^ARG EMQX_VERSION=.*$', 'ARG EMQX_VERSION="6.3.1"')
        self.assertRejected("is not v5.8.x")

    def test_rejects_emqx_5_7(self) -> None:
        self.sub("Dockerfile", r'^ARG EMQX_VERSION=.*$', 'ARG EMQX_VERSION="v5.7.2"')
        self.assertRejected("is not v5.8.x")

    def test_accepts_later_5_8_patch(self) -> None:
        self.sub("Dockerfile", r'^ARG EMQX_VERSION=.*$', 'ARG EMQX_VERSION="v5.8.10"')
        self.edit("addon_info.yaml", "current_version: v5.8.9", "current_version: v5.8.10")
        self.edit("CHANGELOG.md", "EMQX broker 5.8.9", "EMQX broker 5.8.10")
        self.edit("README.md", "| EMQX | 5.8.9 |", "| EMQX | 5.8.10 |")
        self.assertEqual(check_release.check(self.addon), [])

    def test_rejects_enterprise_download(self) -> None:
        self.edit(
            "Dockerfile",
            "https://github.com/emqx/emqx/releases/download/${EMQX_VERSION}/",
            "https://packages.emqx.io/emqx-ee/${EMQX_VERSION}/",
        )
        self.assertRejected("enterprise edition")

    # --- download pinning -------------------------------------------------
    def test_rejects_missing_emqx_sha(self) -> None:
        self.sub("Dockerfile", r'^ARG EMQX_SHA256_ARM64=.*\n', "")
        self.assertRejected("EMQX_SHA256_ARM64 must be a pinned SHA-256")

    def test_rejects_malformed_ngrok_sha(self) -> None:
        self.sub("Dockerfile", r'^ARG NGROK_SHA256_AMD64=.*$', 'ARG NGROK_SHA256_AMD64="abc"')
        self.assertRejected("NGROK_SHA256_AMD64 must be a pinned SHA-256")

    def test_rejects_unverified_download(self) -> None:
        self.edit(
            "Dockerfile",
            '&& echo "${EMQX_SHA256}  /tmp/emqx.tar.gz" | sha256sum -c - \\\n',
            "",
        )
        self.assertRejected("does not verify /tmp/emqx.tar.gz")

    def test_rejects_piped_tarball(self) -> None:
        self.edit(
            "Dockerfile",
            "&& tar -xzf /tmp/emqx.tar.gz -C /opt/emqx",
            "&& curl -fL https://example.invalid/x.tgz | tar zxf - -C /opt/emqx",
        )
        self.assertRejected("pipes a download straight into tar")

    def test_rejects_floating_ngrok(self) -> None:
        self.edit(
            "Dockerfile",
            '"${NGROK_BASE_URL}/ngrok_${NGROK_VERSION}-0_${ARCH}.deb"',
            '"${NGROK_BASE_URL}/ngrok-v3-stable-linux-${ARCH}.tgz"',
        )
        self.assertRejected("floating ngrok")

    def test_rejects_curl_without_fail(self) -> None:
        self.edit("Dockerfile", "curl -fsSL \\", "curl -sSL \\")
        self.assertRejected("curl without --fail")

    def test_rejects_without_retry(self) -> None:
        self.edit("Dockerfile", "--retry 5 --retry-delay 3 --retry-all-errors ", "")
        self.assertRejected("must retry")

    # --- version agreement ----------------------------------------------
    def test_rejects_config_changelog_mismatch(self) -> None:
        self.edit("config.yaml", 'version: "5.9.1"', 'version: "5.9.2"')
        self.assertRejected("newest CHANGELOG section is 5.9.1")

    def test_rejects_changelog_not_newest_on_top(self) -> None:
        self.edit("CHANGELOG.md", "## v5.9.0", "## v5.9.5")
        self.assertRejected("newest version on top")

    def test_rejects_changelog_without_broker_version(self) -> None:
        self.edit(
            "CHANGELOG.md",
            "Add-on 5.9.1; EMQX broker 5.8.9 (the last",
            "Add-on 5.9.1 (the last",
        )
        path = self.addon / "CHANGELOG.md"
        newest = check_release.changelog_section(path.read_text(encoding="utf-8"), "5.9.1")
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                newest, newest.replace("5.8.9", "5.8.x")
            ),
            encoding="utf-8",
        )
        self.assertRejected("must state the broker version 5.8.9")

    def test_rejects_addon_info_mismatch(self) -> None:
        self.edit("addon_info.yaml", "current_version: v5.8.9", "current_version: v5.8.8")
        self.assertRejected("differs from Dockerfile EMQX_VERSION")

    def test_rejects_addon_info_tracking_latest(self) -> None:
        self.sub("addon_info.yaml", r"^  version_template:.*$", "")
        self.assertRejected("restrict tracking to v5.8.x")

    def test_addon_info_template_rejects_enterprise(self) -> None:
        self.sub(
            "addon_info.yaml",
            r"^  version_template:.*$",
            r"  version_template: regex:^v(?<major>5)\.(?<minor>8)|v(?<x>\d+)\.(?<patch>\d+)",
        )
        self.assertRejected("accepts v5.9.0")

    def test_rejects_build_yaml_base_mismatch(self) -> None:
        self.edit("build.yaml", "debian-base:9.5.0", "debian-base:9.2.0")
        self.assertRejected("differs from the Dockerfile BUILD_FROM")

    def test_rejects_stale_root_readme(self) -> None:
        readme = self.tmp / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace("v5.9.1", "v5.9.0"),
            encoding="utf-8",
        )
        self.assertRejected("README.md does not mention add-on v5.9.1")


class DockerfileContractTest(unittest.TestCase):
    """Facts the add-on relies on, read straight from the files."""

    def setUp(self) -> None:
        self.dockerfile = (ADDON / "Dockerfile").read_text(encoding="utf-8")
        self.args = check_release.dockerfile_args(self.dockerfile)

    def test_upstream_checksums_are_pinned(self) -> None:
        # From emqx-5.8.9-debian13-{amd64,arm64}.tar.gz.sha256 of the v5.8.9 release
        self.assertEqual(
            self.args["EMQX_SHA256_AMD64"],
            "c3b5a8f7c83601867d0fc1ef19a851f6f16f56a9436de74cb05f81f188a44a90",
        )
        self.assertEqual(
            self.args["EMQX_SHA256_ARM64"],
            "524b5dd8ccad1f7dc5e5c15171328618e1ea7703fed56e17006a27258a13ecd4",
        )

    def test_arch_selects_its_own_checksum(self) -> None:
        self.assertRegex(
            self.dockerfile,
            r'amd64\)\s*\\\s*ARCH="amd64";\s*\\\s*EMQX_SHA256="\$\{EMQX_SHA256_AMD64\}";'
            r'\s*\\\s*NGROK_SHA256="\$\{NGROK_SHA256_AMD64\}"',
        )
        self.assertRegex(
            self.dockerfile,
            r'aarch64\)\s*\\\s*ARCH="arm64";\s*\\\s*EMQX_SHA256="\$\{EMQX_SHA256_ARM64\}";'
            r'\s*\\\s*NGROK_SHA256="\$\{NGROK_SHA256_ARM64\}"',
        )

    def test_no_point_release_package_pins(self) -> None:
        self.assertNotRegex(self.dockerfile, r"\b[a-z0-9.+-]+=\d+:?[\w.~+-]*-\d+\b")

    def test_healthcheck_is_kept(self) -> None:
        self.assertIn("HEALTHCHECK CMD curl --fail http://127.0.0.1:18083", self.dockerfile)


if __name__ == "__main__":
    unittest.main()
