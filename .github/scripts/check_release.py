#!/usr/bin/env python3
"""Release guard for the Woow EMQX add-on.

Fails (exit 1) when the add-on would ship something it must not:

* an EMQX broker outside the 5.8 line. v5.8.9 is the last Apache-2.0
  open-source release; 5.9+ and 6.x are the Business Source License
  enterprise edition, which may not be shipped embedded in this add-on;
* a download that is not pinned to a SHA-256 checksum and verified;
* version statements that disagree: config.yaml, the newest CHANGELOG
  section, addon_info.yaml, build.yaml and the READMEs.

Usage: check_release.py [ADDON_DIR]   (default: emqx, next to the repo root)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ALLOWED_BROKER = re.compile(r"^v5\.8\.\d+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ARCHES = ("AMD64", "ARM64")
# Anything that points at the enterprise edition.
ENTERPRISE_MARKERS = ("emqx-enterprise", "emqx-ee", "packages.emqx.io")


def parse_version(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        raise ValueError(f"not a semantic version: {text!r}")
    return tuple(int(part) for part in match.groups())


def dockerfile_args(dockerfile: str) -> dict[str, str]:
    """Default values of every ARG declared with a value."""
    args: dict[str, str] = {}
    for match in re.finditer(r'^ARG\s+(\w+)=(?:"([^"]*)"|(\S+))', dockerfile, re.M):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        args.setdefault(match.group(1), value)
    return args


def changelog_versions(changelog: str) -> list[str]:
    """Versions of the '## vX.Y.Z' headings, newest (topmost) first."""
    return re.findall(r"^##\s+v?(\d+\.\d+\.\d+)\b", changelog, re.M)


def changelog_section(changelog: str, version: str) -> str:
    match = re.search(
        rf"^##\s+v?{re.escape(version)}\b(.*?)(?=^##\s|\Z)", changelog, re.M | re.S
    )
    return match.group(1) if match else ""


def check(addon_dir: Path) -> list[str]:
    errors: list[str] = []

    def read(name: str) -> str:
        path = addon_dir / name
        if not path.is_file():
            errors.append(f"{path} is missing")
            return ""
        return path.read_text(encoding="utf-8")

    dockerfile = read("Dockerfile")
    changelog = read("CHANGELOG.md")
    readme = read("README.md")
    config = yaml.safe_load(read("config.yaml") or "{}") or {}
    build = yaml.safe_load(read("build.yaml") or "{}") or {}
    info = yaml.safe_load(read("addon_info.yaml") or "{}") or {}
    root_readme_path = addon_dir.parent / "README.md"
    root_readme = (
        root_readme_path.read_text(encoding="utf-8")
        if root_readme_path.is_file()
        else ""
    )
    if errors:
        return errors

    args = dockerfile_args(dockerfile)

    # --- Broker: open-source 5.8 line only --------------------------------
    broker = args.get("EMQX_VERSION", "")
    if not ALLOWED_BROKER.match(broker):
        errors.append(
            f"Dockerfile EMQX_VERSION={broker!r} is not v5.8.x; 5.9+ and 6.x are "
            "the BSL enterprise edition and may not be shipped"
        )
    lowered = dockerfile.lower()
    for marker in ENTERPRISE_MARKERS:
        if marker in lowered:
            errors.append(f"Dockerfile references the enterprise edition ({marker})")
    if "github.com/emqx/emqx/releases/download/${EMQX_VERSION}/" not in dockerfile:
        errors.append(
            "Dockerfile must download EMQX from the emqx/emqx GitHub release "
            "of ${EMQX_VERSION}"
        )

    # --- Downloads: pinned and verified -----------------------------------
    for prefix in ("EMQX", "NGROK"):
        for arch in ARCHES:
            name = f"{prefix}_SHA256_{arch}"
            value = args.get(name, "")
            if not SHA256.match(value):
                errors.append(f"Dockerfile {name} must be a pinned SHA-256 (got {value!r})")
    if not re.match(r"^\d+\.\d+\.\d+$", args.get("NGROK_VERSION", "")):
        errors.append("Dockerfile NGROK_VERSION must pin an exact ngrok release")
    for prefix, target in (("EMQX", "/tmp/emqx.tar.gz"), ("NGROK", "/tmp/ngrok.deb")):
        verify = rf'echo "\${{{prefix}_SHA256}}\s+{re.escape(target)}"\s*\|\s*sha256sum -c'
        if not re.search(verify, dockerfile):
            errors.append(f"Dockerfile does not verify {target} with sha256sum -c")
    if "stable-linux" in dockerfile:
        errors.append("Dockerfile downloads the floating ngrok 'stable' build")
    for line in dockerfile.splitlines():
        if re.search(r"\bcurl\b", line) and "HEALTHCHECK" not in line:
            if not re.search(r"-\w*f", line):
                errors.append(f"curl without --fail in Dockerfile: {line.strip()}")
    if "--retry" not in dockerfile:
        errors.append("Dockerfile downloads must retry (--retry)")
    if re.search(r"\|\s*tar\s+\S*z", dockerfile):
        errors.append("Dockerfile pipes a download straight into tar; verify it first")

    # --- Versions agree ---------------------------------------------------
    addon_version = str(config.get("version", ""))
    try:
        parse_version(addon_version)
    except ValueError as exc:
        errors.append(f"config.yaml version: {exc}")

    versions = changelog_versions(changelog)
    if not versions:
        errors.append("CHANGELOG.md has no '## vX.Y.Z' section")
    else:
        if versions[0] != addon_version:
            errors.append(
                f"newest CHANGELOG section is {versions[0]} but config.yaml "
                f"version is {addon_version}"
            )
        try:
            ordered = [parse_version(v) for v in versions]
            if ordered[0] != max(ordered) or len(set(ordered)) != len(ordered):
                errors.append(
                    "CHANGELOG sections must be unique with the newest version on "
                    "top (the Store refuses a lower version)"
                )
        except ValueError as exc:
            errors.append(f"CHANGELOG.md: {exc}")
        newest = changelog_section(changelog, versions[0])
        if broker and broker.lstrip("v") not in newest:
            errors.append(
                f"newest CHANGELOG section must state the broker version "
                f"{broker.lstrip('v')}"
            )

    source = info.get("source") or {}
    if source.get("current_version") != broker:
        errors.append(
            f"addon_info.yaml current_version={source.get('current_version')!r} "
            f"differs from Dockerfile EMQX_VERSION={broker!r}"
        )
    if source.get("repo") != "emqx/emqx":
        errors.append("addon_info.yaml must track emqx/emqx")
    template = str(source.get("version_template", ""))
    if not template.startswith("regex:") or not re.search(r"\(\?<major>5\)", template) \
            or not re.search(r"\(\?<minor>8\)", template):
        errors.append(
            "addon_info.yaml source.version_template must restrict tracking to v5.8.x"
        )
    else:
        pattern = template[len("regex:"):].replace("(?<", "(?P<")
        for probe, allowed in (("v5.8.9", True), ("v5.8.10", True),
                               ("v5.9.0", False), ("v6.0.0", False)):
            if bool(re.match(pattern, probe)) != allowed:
                errors.append(
                    f"addon_info.yaml version_template {'rejects' if allowed else 'accepts'} {probe}"
                )

    base = args.get("BUILD_FROM", "")
    for arch, image in (build.get("build_from") or {}).items():
        if image != base:
            errors.append(
                f"build.yaml build_from.{arch}={image} differs from the Dockerfile "
                f"BUILD_FROM default {base}"
            )

    if broker and broker.lstrip("v") not in readme:
        errors.append(f"{addon_dir.name}/README.md does not mention EMQX {broker.lstrip('v')}")
    if root_readme and addon_version and f"v{addon_version}" not in root_readme:
        errors.append(f"README.md does not mention add-on v{addon_version}")

    return errors


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[2]
    addon_dir = Path(argv[1]) if len(argv) > 1 else root / "emqx"
    errors = check(addon_dir)
    for error in errors:
        print(f"::error::{error}")
    if not errors:
        print(f"{addon_dir.name}: release checks passed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
