# Woow_ha_emqx — WoowTech EMQX Home Assistant Add-on Repository

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FWOOWTECH%2FWoow_ha_emqx)

Home Assistant add-on repository for the [EMQX](https://www.emqx.io/) MQTT
broker — a high-performance, scalable MQTT message broker for IoT that also
serves as an advanced alternative to the Mosquitto MQTT add-on in Home
Assistant, with a built-in web dashboard.

EMQX 是全球領先的開源分散式 MQTT 訊息代理,適用於大規模 IoT 部署,可作為
Home Assistant Mosquitto MQTT Broker 的進階替代方案,並提供圖形化管理介面。

## Add-ons in this repository | 本倉庫的 add-on

| Add-on | Description |
|---|---|
| [Woow EMQX](emqx/) | EMQX MQTT broker (v5.8.9) with the EMQX Dashboard (amd64 / aarch64) |

## Installation | 安裝

1. Click the badge above (or **Settings → Add-ons → Add-on Store → ⋮ →
   Repositories**) and add:
   `https://github.com/WOOWTECH/Woow_ha_emqx`
2. Find **Woow EMQX** in the store and click **INSTALL**.
3. Start the add-on, then open the Web UI. Default login is `admin` /
   `public` — change the password immediately in the Dashboard and enable
   authentication under **Access Control → Authentication**.
4. Details, options and troubleshooting: [emqx/README.md](emqx/README.md)

> **Migrated from `Woow_eqmx_docker_compose_all` (branch `ha`, note the typo
> `eqmx` in the old repo name).** If you added the old repository URL,
> **remove it and add this one** — archived repos no longer receive updates.
> 若你先前加入的是舊倉庫 `Woow_eqmx_docker_compose_all` 網址,請移除並改加
> 本倉庫網址,才能繼續收到更新。

## Other deployment platforms | 其他部署平台

- Docker / Podman Compose → [Woow_podman_emqx](https://github.com/WOOWTECH/Woow_podman_emqx)
- K3s / Kubernetes Helm chart → [Woow_k3s_emqx](https://github.com/WOOWTECH/Woow_k3s_emqx)
