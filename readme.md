# 🚀 ROM Uploader (`fogos`)

Automated custom ROM release and Telegram publisher for **Moto G34/G45 5G (`fogos`)** 📱.

## ✨ Features

* 🔍 **Auto Build Discovery** — Scans your server for the ROM source root and `out/target/product/fogos`, selecting the newest **full** ROM `.zip` (ignoring incremental, OTA, fastboot, and target-files packages) along with `boot.img` and `dtbo.img`.
* 🧠 **Smart Metadata Detection** — Resolves ROM name, version, Android version, build date, and build time from `build.prop`, `.zip` metadata, source tree flags (`version_defaults.mk` / manifests), and filename.
* ⚡ **Full-Bandwidth Uploads** — Streams files in 4 MB zero-RAM-buffering chunks across 6 concurrent workers to **Pixeldrain** and **GoFile**, retrying each failed upload up to 3 times before aborting cleanly.
* 📊 **Live Terminal UI** — Displays the `rom-upload` banner, detected ROM details, loading status, per-file progress bars with live speeds (`MB/s`), and final mirror links (also saved to `release_links.txt`).
* 🖼️ **Telegram Release Post** — Fetches only the matching ROM banner image from [`tiraq-official/banner_repo`](https://github.com/tiraq-official/banner_repo) and posts a clean HTML summary with **download-only** inline URL buttons.

## 🛠️ Quick Start

### 1. 📦 Install Dependency

```bash
pip install requests
```

### 2. 🔑 Configure Credentials

Export your credentials in the shell (or set them in the `CONFIGURATION` block at the top of `release.py`):

```bash
export TELEGRAM_TOKEN="your_bot_token"
export TELEGRAM_CHAT_ID="your_chat_id"
export PIXELDRAIN_API_KEY="your_pixeldrain_api_key"
```

### 3. ▶️ Run

```bash
# Dry run (simulates uploads, prints links, and sends preview to Telegram if configured)
python3 release.py --dry-run

# Full release (uploads ROM, boot.img, dtbo.img and posts to Telegram)
python3 release.py

# Include extra detection diagnostics
python3 release.py --debug
```

## ⚙️ Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_TOKEN` | `""` | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | `""` | Target Telegram channel or chat ID |
| `PIXELDRAIN_API_KEY` | `""` | Pixeldrain API key |
| `BANNER_REPO` | `tiraq-official/banner_repo` | Public GitHub repo containing ROM banners |
| `MAINTAINER` | `tiraq` | Maintainer name shown in post |
| `DEVICE_CODENAME` | `fogos` | Target device codename |
| `DEVICE_NAME` | `Moto G34/G45 5G (fogos)` | Display device name |
| `PRODUCT_DIR` | *(auto-detected)* | Override `out/target/product/fogos` path |
| `ROM_NAME_OVERRIDE` | *(auto-detected)* | Override detected ROM name |
| `ROM_VERSION_OVERRIDE` | *(auto-detected)* | Override detected ROM version |
| `ANDROID_VERSION_OVERRIDE` | *(auto-detected)* | Override detected Android version |
