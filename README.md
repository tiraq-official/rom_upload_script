# 🚀 Automated Android ROM Deployer

An automated Python script built for Android custom ROM developers to automatically parse, upload, and announce new ROM builds to Telegram channels right from their CI/CD or local build environment.

---

## ✨ Features

* 🔍 **Smart Scan:** Automatically identifies the newest ROM ZIP file located in `out/target/product/*`.
* 📱 **Codename Detection:** Dynamically extracts the target device's codename based on the build output directory structure.
* 📦 **ROM Metadata Parsing:** Detects the ROM name, variation variant (`OFFICIAL` / `UNOFFICIAL`), and compilation date directly from the filename or file attributes.
* 🤖 **Android Version Extraction:** Pulls the correct Android version release number seamlessly from `build.prop`.
* 💾 **Precise File Sizing:** Computes the exact file footprint dynamically scaled in Gigabytes (e.g., `2.37 GB`).
* ⚡ **High-Speed Parallel Uploads:** Leverages concurrent worker threads to upload the core ROM ZIP alongside complementary image assets (`boot.img`, `dtbo.img`, and `vendor_boot.img`) simultaneously to **GoFile**.
* 🔄 **Resilient Networking:** Implements automatic request retry safety loops (up to 3 structural attempts) to counter sporadic network timeouts.
* 📝 **Local Link Logging:** Automatically pipes all generated download pages cleanly into a local `release_links.txt` reference file.
* 📢 **Telegram Integration:** Dispatches beautifully formatted rich-text notification cards immediately to a predefined channel or chat group.

---

## 📊 Sample Notification Format

When a build successfully finishes processing, your Telegram channel receives a clean update card matching this structure:

```text
━━━━━━━━━━━━━━━━━━━━━━
📦 ROM Build Completed

📱 Device : fogos
🎯 ROM    : AxionOS 2.8
🤖 Android: 16
🏷 Type    : UNOFFICIAL
📅 Build   : 2026-07-20
💾 Size    : 2.31 GB

⬇ Downloads

📦 ROM
[https://gofile.io/d/XXXXXX](https://gofile.io/d/XXXXXX)

💿 Boot
[https://gofile.io/d/XXXXXX](https://gofile.io/d/XXXXXX)

📀 DTBO
[https://gofile.io/d/XXXXXX](https://gofile.io/d/XXXXXX)

⚙ Vendor Boot
[https://gofile.io/d/XXXXXX](https://gofile.io/d/XXXXXX)

#fogos #Android16
━━━━━━━━━━━━━━━━━━━━━━`
