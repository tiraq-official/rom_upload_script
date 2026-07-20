# 🚀 Automated Android ROM Deployer

An automated Python script built for Android custom ROM developers to automatically parse, upload, and announce new ROM builds to Telegram channels right from their CI/CD or local build environment.

---

## ✨ Features

* 🔍 **Smart Scan:** Automatically identifies the newest ROM ZIP file located in `out/target/product/*`.
* 📱 **Codename Detection:** Dynamically extracts the target device's codename based on the build output directory structure.
* 📦 **ROM Metadata Parsing:** Detects the full ROM name, variation variant (`OFFICIAL` / `UNOFFICIAL`), and compilation date directly from the filename or file attributes without substring collision bugs.
* 🤖 **Android Version Extraction:** Pulls the correct Android version release number seamlessly from `build.prop`.
* 💾 **Precise File Sizing:** Computes the exact file footprint dynamically scaled in Gigabytes (e.g., `2.23 GB`).
* ⚡ **High-Speed Parallel Uploads:** Leverages concurrent worker threads to upload the core ROM ZIP alongside complementary image assets (`boot.img`, `dtbo.img`, and `vendor_boot.img`) simultaneously.
* 🌐 **Dual Mirror Support:** Mirror paths are deployed concurrently to both **GoFile** and **Pixeldrain** (using authorized User API keys).
* 🔄 **Resilient Networking:** Implements automatic request retry safety loops (up to 3 structural attempts) to counter sporadic network timeouts.
* 📝 **Local Link Logging:** Automatically pipes all generated download pages cleanly into a local `release_links.txt` reference file.
* 📢 **Telegram Integration:** Dispatches beautifully formatted rich-text notification cards with clean, compact inline mirror hyperlinks immediately to a predefined channel or chat group.

---

## 📊 Sample Notification Format

When a build successfully finishes processing, your Telegram channel receives a clean update card matching this structure:

```text
━━━━━━━━━━━━━━━━━━━━━━
📦 ROM Build Completed

📱 Device : fogos
🎯 ROM    : Lunaris-AOSP 3.12
🤖 Android: 16
🏷 Type    : UNOFFICIAL
📅 Build   : 2026-07-20
💾 Size    : 2.23 GB

⬇ Downloads

📦 ROM
🔗 GoFile | Pixeldrain

💿 Boot
🔗 GoFile | Pixeldrain

📀 DTBO
🔗 GoFile | Pixeldrain

⚙ Vendor Boot
🔗 GoFile | Pixeldrain

#fogos #Android16
━━━━━━━━━━━━━━━━━━━━━━