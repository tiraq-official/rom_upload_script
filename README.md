# 🚀 Automated Android ROM Deployer — fogos edition

An automated Python script built for the **Moto G34/G45 5G (fogos)** custom ROM maintenance workflow. After a successful build, it parses the ROM ZIP, uploads it to **SourceForge** (permanent storage), mirrors it to **GoFile** and **Pixeldrain**, and announces a clean release post to your Telegram channel — straight from your CI/CD server or local build environment.

---

## ✨ Features

- 🎯 **Targeted Device Support:** Locked to `out/target/product/fogos` for the Moto G34/G45 5G — no generic codename guessing.
- 🏷 **Maintained ROM Roster:** Only reacts to the four maintained ROMs — **Axion OS**, **Lunaris OS**, **Voltage OS**, and **Pixelify OS** (also recognized by its alias **ASCP**). Any other ZIP in the output folder is ignored.
- 🔤 **Flexible Name Matching:** Filenames are normalized (lowercased, spaces/dashes/underscores/dots stripped) before matching, so `LunarisOS-...`, `Lunaris OS_...`, `lunaris_os-...`, and `Lunaris-OS...` all resolve to the same ROM.
- 📦 **ROM Metadata Parsing:** Detects ROM version, build variant (`OFFICIAL` / `UNOFFICIAL`), and build date directly from the filename — with UNOFFICIAL checked before OFFICIAL so substring collisions can't mislabel the build.
- 🤖 **Android Version Extraction:** Pulls the Android release number from `build.prop`, with a filename fallback for builds where the prop isn't reachable.
- 💾 **Precise File Sizing:** Computes the exact ROM footprint in Gigabytes (e.g. `2.23 GB`).
- 🏛 **SourceForge Permanent Storage:** Uploads the ROM ZIP via `rsync` (with `--partial` resume support) into the correct per-ROM folder of your SourceForge project — the archive link never expires.
- ⚡ **High-Speed Parallel Uploads:** The SourceForge transfer, and the GoFile + Pixeldrain uploads of the ROM and images (`boot.img`, `dtbo.img`, `vendor_boot.img`), all run concurrently — total release time equals the slowest single upload, not the sum of all of them.
- 🌐 **Dual Mirror Support:** Every file goes to both **GoFile** and **Pixeldrain** (authorized user API key) in parallel threads.
- 🔄 **Resilient Networking:** Automatic retry loops (up to 3 attempts per mirror) counter sporadic network timeouts. `--partial` means an interrupted multi-GB SourceForge transfer resumes instead of restarting from zero.
- 🛡 **Graceful Degradation:** If one mirror fails, the Telegram post simply shows whichever links succeeded. If SourceForge fails entirely, the release is still posted with GoFile/Pixeldrain only — the script never dies mid-release.
- 📝 **Local Link Logging:** Every generated link is written to a local `release_links.txt` reference file.
- 📢 **Telegram Integration:** Dispatches a clean release card using only formatting available to non-premium accounts (bold, inline code, and links) — no broken markup, no premium-only entities.

---

## 📁 SourceForge Project Layout

Uploads land in the project's File Release System (FRS), one folder per ROM:

```
orb-of-fortune-teller/           ← SourceForge project
└── fogos/                       ← base path
    ├── axion/
    ├── lunaris/
    ├── voltage/
    └── pixelify/
```

Remote rsync destination used by the script:

```
tiraq,orb-of-fortune-teller@frs.sourceforge.net:/home/frs/project/orb-of-fortune-teller/fogos/<rom>/
```

Public download links follow the permanent format:

```
https://sourceforge.net/projects/orb-of-fortune-teller/files/fogos/<rom>/<filename>.zip/download
```

---

## ⚙️ Setup

### 1. Requirements

- Python 3.8+
- `requests` (`pip install requests`)
- `rsync` (usually preinstalled; `apt install rsync` on Debian/Ubuntu)
- `ssh` (for the SourceForge upload)

### 2. Configure the script

All settings live in the `CONFIGURATION` block at the top of `release.py`. Sensitive values can also be supplied as environment variables, which keeps them out of the file itself:

| Setting | Environment variable | Purpose |
|---|---|---|
| `TELEGRAM_TOKEN` | `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | `TELEGRAM_CHAT_ID` | Target channel/group chat ID |
| `PIXELDRAIN_API_KEY` | `PIXELDRAIN_API_KEY` | Pixeldrain user API key |
| `SOURCEFORGE_USER` | `SOURCEFORGE_USER` | SourceForge username (`tiraq`) |
| `SOURCEFORGE_PROJECT` | `SOURCEFORGE_PROJECT` | Project unix name (`orb-of-fortune-teller`) |
| `SOURCEFORGE_BASE_PATH` | `SOURCEFORGE_BASE_PATH` | Folder inside the project (`fogos`) |

### 3. SourceForge SSH access (one-time, per server)

rsync logs into `frs.sourceforge.net` over SSH, so the build server needs a registered SSH key. On the **build server**:

```bash
# Generate a dedicated key (no passphrase, so CI can run unattended)
ssh-keygen -t ed25519 -C "fogos-release-server" -f ~/.ssh/id_ed25519 -N ""

# Print the public key and copy its output
cat ~/.ssh/id_ed25519.pub
```

Then on sourceforge.net: click your username → **Account** → **SSH Settings** → paste the key → **Add**.

Verify it works (it should connect without asking for a password; a restricted-shell message and disconnect is normal):

```bash
ssh -T tiraq,orb-of-fortune-teller@frs.sourceforge.net
```

### 4. Run it

```bash
# from the build tree root (script expects ../out/target/product/fogos)
python3 release.py
```

---

## 🔐 Security Notes (rented-server workflow)

Because builds may happen on disposable rented servers, the recommended key lifecycle is:

1. **Dedicated key per server** — never reuse a personal key for uploads, so revocation never affects anything else.
2. **Revoke when done** — after the release upload completes (look for `✅ SourceForge completed`), delete the key from Account → SSH Settings. The leftover private key on the returned server instantly becomes useless.
3. **Clean up the server** — `rm ~/.ssh/id_ed25519*` before returning the hardware, as basic hygiene.

Note that SourceForge SSH keys are per-account but the rsync shell is restricted to file operations under `/home/frs/project/...` — an active key can do nothing else with your account.

---

## 📊 Sample Notification

When a build finishes processing, your Telegram channel receives this card (raw message, as sent):

```text
━━━━━━━━━━━━━━━━━━━━
📦 *Pixelify OS 15.0 | OFFICIAL*
📱 *Device*: Moto G34/G45 5G (fogos)

🤖 Android: `15`
📅 Build Date: `18-09-2025`
💾 Size: `2.33 GB`

⬇️ *Downloads*

*ROM*
[SourceForge](https://sourceforge.net/projects/orb-of-fortune-teller/files/fogos/pixelify/PixelifyOS_fogos-15.0-20250918-0524.zip/download) • [GoFile](https://gofile.io/d/AbCdEf) • [Pixeldrain](https://pixeldrain.com/u/abc123)

*Boot Image*
[GoFile](https://gofile.io/d/B1) • [Pixeldrain](https://pixeldrain.com/u/b1)

*DTBO Image*
[GoFile](https://gofile.io/d/D1) • [Pixeldrain](https://pixeldrain.com/u/d1)

*Vendor Boot Image*
[GoFile](https://gofile.io/d/V1) • [Pixeldrain](https://pixeldrain.com/u/v1)

━━━━━━━━━━━━━━━━━━━━
⚠️ Flash at your own risk. Always take a backup before installing.
#fogos #PixelifyOS #Android15
```

Only the ROM entry carries a SourceForge link — boot/DTBO/vendor-boot images stay on the two temporary mirrors.

---

## 🧩 How It Works

1. **Scan** — globs `out/target/product/fogos/*.zip`, keeps only files matching a maintained ROM, picks the newest by modification time.
2. **Parse** — extracts ROM name, version, build type, Android version, date, and size from the filename, `build.prop`, and file attributes.
3. **Upload** — rsyncs the ROM ZIP to the matching SourceForge folder while, in parallel, the ROM and images go out to GoFile and Pixeldrain via worker threads.
4. **Log** — writes every link to `release_links.txt`.
5. **Announce** — sends the formatted release card to Telegram.

## 🔄 Adding or Renaming a ROM

Everything ROM-related lives in one dictionary at the top of the script — display name, match keywords, and SourceForge folder:

```python
SUPPORTED_ROMS = {
    "Axion OS":    {"keywords": ["axion"],                    "sf_folder": "axion"},
    "Lunaris OS":  {"keywords": ["lunaris"],                  "sf_folder": "lunaris"},
    "Voltage OS":  {"keywords": ["voltage"],                  "sf_folder": "voltage"},
    "Pixelify OS": {"keywords": ["pixelify", "ascp"],         "sf_folder": "pixelify"},
}
```

Add an entry, create the matching folder on SourceForge, and the script picks it up automatically.
