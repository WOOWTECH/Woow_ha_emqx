# Woow EMQX ngrok TCP (1883) Integration Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development in this session) to implement task-by-task.

**Goal:** Add built-in ngrok support to the Woow EMQX add-on so a single `tcp 1883` MQTT tunnel is opened and its resolved public URL is printed to the add-on Log, driven entirely by config (authtoken + optional reserved TCP address).

**Architecture:** Mirror the headscale add-on's ngrok pattern (Dockerfile installs the ngrok v3 binary; an s6 service starts the tunnel; a companion oneshot polls `http://127.0.0.1:4040/api/tunnels` and logs the resolved URL). Unlike headscale, EMQX never needs the public URL injected into its own config — it only listens locally — so no config rewrite step is required. MQTT-over-WS (8083) is out of scope (handled by Cloudflare Tunnel).

**Tech Stack:** HA add-on config/schema YAML, Debian (hassio debian-base), bashio, s6-overlay v3 (s6-rc.d), ngrok v3 static binary, curl/jq.

---

## Source of truth

- Canonical repo: `/data/pi-agent/home/pi-cwd-20260817/repos/Woow_ha_emqx` (subdir `emqx`).
- The `Woow_HA_App_Store` copy of `emqx/` is a **daily-synced mirror** (`.github/workflows/sync-upstreams.yml`, mapping `emqx|Woow_ha_emqx|emqx`). Edit `Woow_ha_emqx` as the source of truth; do not hand-edit the App Store copy.
- Verified s6-rc.d v3 convention (from `emqx/rootfs`):
  - oneshot `init-emqx`: `type`="oneshot" (644), `run`=executable (755, shebang `#!/command/with-contenv bashio`), `up`=**plain text file** containing a single line equal to the run script's absolute path (644), `dependencies.d/base` (empty 644).
  - longrun `emqx`: `type`="longrun" (644), `run`=executable (755), `dependencies.d/init-emqx`.
  - Services join the boot bundle via empty files `user/contents.d/<service>`.

## Reference (headscale template)

- `headscale/Dockerfile`: installs `ngrok-v3-stable-linux-${ARCH}.tgz` from `https://bin.equinox.io/c/bNyj1mQVY4c`.
- `headscale/.../services.d/ngrok/run`: uses `NGROK_AUTHTOKEN` env; `ngrok tcp <port>` / `ngrok http <port> --url=<domain>`.
- `headscale/.../headscale/run`: polls `http://127.0.0.1:4040/api/tunnels` → `.tunnels[0].public_url`.
- NOTE: headscale uses legacy `services.d`; EMQX uses s6-rc.d v3, so the "disabled" idiom differs (Task 3).

---

## Config schema (confirmed)

```yaml
options:
  env_vars: []
  ngrok_enabled: false
  ngrok_authtoken: ""
  ngrok_tcp_addr: ""

schema:
  env_vars:
    - name: match(^EMQX_([A-Z0-9_])+$)
      value: str
  ngrok_enabled: bool
  ngrok_authtoken: password?
  ngrok_tcp_addr: str?
```

Behavior contract:
- `ngrok_enabled=false` → ngrok services idle, no tunnel, only an info log "ngrok 未啟用，service idle".
- `ngrok_enabled=true` + empty `ngrok_authtoken` → log ERROR (do NOT crash EMQX); ngrok stays idle.
- `ngrok_enabled=true` + authtoken → start `ngrok tcp 1883`.
  - `ngrok_tcp_addr` set → `--remote-addr=<addr>` (reserved fixed address).
  - `ngrok_tcp_addr` empty → ngrok auto-assigns (ephemeral).
- After tunnel is up, log exactly: `>>> MQTT ngrok: tcp://<host>:<port>`.

---

## Tasks

### Task 1: Add ngrok options/schema + version bump

**Files:** Modify `emqx/config.yaml`

- Bump `version` to `"5.9.0"` (user-approved; signals HAOS `update_available`).
- Add `ngrok_enabled`, `ngrok_authtoken`, `ngrok_tcp_addr` to `options` and `schema` (exact YAML above). Keep `env_vars` unchanged. Validate with a YAML parse + assert keys.

**Validation:** `python3 -c` load config.yaml, assert the three options and schema exist, authtoken is `password?`, tcp_addr is `str?`.

### Task 2: Install ngrok binary in Dockerfile

**Files:** Modify `emqx/Dockerfile`

Add `ARG NGROK_BASE_URL="https://bin.equinox.io/c/bNyj1mQVY4c"` and, in the existing `RUN` (reusing the already-computed `ARCH` variable = amd64/arm64), install ngrok:

```dockerfile
&& curl -fsSL -o /tmp/ngrok.tgz \
    "${NGROK_BASE_URL}/ngrok-v3-stable-linux-${ARCH}.tgz" \
&& tar -xzf /tmp/ngrok.tgz -C /usr/local/bin ngrok \
&& chmod +x /usr/local/bin/ngrok \
```

Add `jq` to the `apt-get install` line (curl is already present and used for the EMQX download; jq is required by the announce service). Preserve all existing apt arguments and cleanup.

**Validation:** `docker build` will be exercised in Task 6; locally check syntax/ARG presence.

### Task 3: Add `ngrok` longrun service

**Files:**
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok/type` (content `longrun`, mode 644)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok/run` (mode 755)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok/dependencies.d/emqx` (empty, 644)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/ngrok` (empty, 644)

`ngrok/run`:

```bash
#!/command/with-contenv bashio
# shellcheck shell=bash
# ngrok tunnel：只負責 raw MQTT tcp 1883；8083 WS 由 Cloudflare 處理。
if ! bashio::config.true 'ngrok_enabled'; then
    bashio::log.info "ngrok 未啟用（'ngrok_enabled: false'），service idle"
    exec sleep infinity
fi

if ! bashio::config.has_value 'ngrok_authtoken'; then
    bashio::log.error "已啟用 ngrok 但 'ngrok_authtoken' 為空；EMQX 本體仍會照常運行，ngrok service idle"
    exec sleep infinity
fi

NGROK_AUTHTOKEN=$(bashio::config 'ngrok_authtoken')
export NGROK_AUTHTOKEN

if bashio::config.has_value 'ngrok_tcp_addr'; then
    addr=$(bashio::config 'ngrok_tcp_addr')
    bashio::log.info "Starting ngrok tcp 1883 --remote-addr=${addr}"
    exec ngrok tcp 1883 --remote-addr="${addr}" --log stdout
fi

bashio::log.info "Starting ngrok tcp 1883 (auto address)"
exec ngrok tcp 1883 --log stdout
```

Rationale: `dependencies.d/emqx` guarantees EMQX (and its 0.0.0.0:1883 listener) is up before ngrok dials `localhost:1883`. `exec sleep infinity` keeps the longrun "up" without a restart loop when disabled.

**Validation:** `bash -n run`, script is 755, `type`/`contents.d` present.

### Task 4: Add `ngrok-announce` oneshot service (log the URL)

**Files:**
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok-announce/type` (content `oneshot`, 644)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok-announce/run` (mode 755)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok-announce/up` (content `/etc/s6-overlay/s6-rc.d/ngrok-announce/run`, 644)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/ngrok-announce/dependencies.d/ngrok` (empty, 644)
- Create: `emqx/rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/ngrok-announce` (empty, 644)

`ngrok-announce/run`:

```bash
#!/command/with-contenv bashio
# shellcheck shell=bash
# 等待 ngrok tcp tunnel 上線後，把 public URL 印到 add-on Log。
if ! bashio::config.true 'ngrok_enabled'; then
    exit 0
fi

readonly NGROK_API='http://127.0.0.1:4040/api/tunnels'
public_url=''
for _ in $(seq 1 60); do
    public_url=$(curl -sf "${NGROK_API}" 2>/dev/null \
        | jq -r '.tunnels[0].public_url // empty' 2>/dev/null) \
        || public_url=''
    [ -n "${public_url}" ] && break
    sleep 2
done

if [ -z "${public_url}" ]; then
    bashio::log.warning "等不到 ngrok tcp tunnel URL（60 x 2s 逾時）；請看 ngrok service log"
    exit 0
fi

bashio::log.info ">>> MQTT ngrok: ${public_url}"
```

**Validation:** `bash -n run`, `up` content path is correct and mode 644, `type`="oneshot". The dependency chain `base → init-emqx → emqx → ngrok → ngrok-announce` must be intact (transitive deps via s6-rc).

### Task 5: Docs + changelog

**Files:**
- Modify: `emqx/README.md` and/or `emqx/DOCS.md` — document the three options; state 8083 WS is via Cloudflare (not ngrok); note TCP address stability (reserved address needed for a fixed port on restart).
- Modify: `emqx/CHANGELOG.md` — add entry describing the built-in ngrok tcp 1883 tunnel + log URL output.
- Modify: `emqx/translations/en.yaml` and `emqx/translations/zh-Hant.yaml` — add option labels/descriptions for the three new config fields (match existing translation keys if present).

**Validation:** YAML parse translations; grep for the new option names in docs.

### Task 6: Build and verify (HAOS)

**Files:** none (verify only)

1. Run `bash -n` on both new `run` scripts; confirm modes (`stat -c %a`) = 755 for `run`, 644 for `type`/`up`/deps/contents.d.
2. From `emqx/`, build the amd64 image via Supervisor local add-on (or `docker build --build-arg BUILD_FROM=ghcr.io/hassio-addons/debian-base:9.2.0 --build-arg BUILD_ARCH=amd64`) and confirm ngrok binary present at `/usr/local/bin/ngrok` and `jq` present.
3. Deploy as local add-on on HAOS: set `ngrok_enabled: true`, a real authtoken, `ngrok_tcp_addr: ""`; start; assert the Log prints `>>> MQTT ngrok: tcp://...` (URL from `http://127.0.0.1:4040/api/tunnels`).
4. Negative test: `ngrok_enabled: false` → no tunnel, EMQX still healthy on 1883.
5. Restore live add-on to its prior config; do not leave the test add-on running alongside the existing `woow-emqx`.

---

## Versioning decision (resolved)

- Bump to `5.9.0` (user-approved) in `config.yaml`; document under `## 5.9.0` in CHANGELOG. `addon_info.yaml` continues to track upstream `emqx/emqx` releases as metadata only (there is no CI in this repo that regenerates `config.yaml` version — verified: no `.github/` in `Woow_ha_emqx`).