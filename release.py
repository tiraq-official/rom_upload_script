#!/usr/bin/env python3
"""
Targeted release script for Moto G34/G45 5G (fogos).

After a successful ROM build:
  1. Finds the newest ROM zip in out/target/product/fogos that belongs to one
     of the maintained ROMs (Axion OS, Lunaris OS, Voltage OS, Pixel OS).
  2. Uploads the ROM + boot/dtbo/vendor_boot images to GoFile and Pixeldrain.
  3. Posts a clean release message to Telegram.

NOTE: Tokens/keys can be overridden with environment variables so you don't
have to keep them in this file:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, PIXELDRAIN_API_KEY
"""

import os
import re
import glob
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = os.environ.get(
    "TELEGRAM_TOKEN",
    "8860525999:AAFLlMQa0eeuKoamBapct3YJAClTVD_Eqwc"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003934445801")
PIXELDRAIN_API_KEY = os.environ.get(
    "PIXELDRAIN_API_KEY",
    "2dfa58b9-27f1-4fb7-b0e6-ba5590fd2ce2"
)

# Targeted device
DEVICE_NAME = "Moto G34/G45 5G (fogos)"
PRODUCT_DIR = "../out/target/product/fogos"

# ROMs we maintain: display name -> keyword matched against the filename.
# The filename is normalized first (lowercase, all spaces/dots/dashes/
# underscores stripped) so "Lunaris OS", "LunarisOS", "lunaris_os",
# "Lunaris-OS" etc. all match the keyword "lunaris".
SUPPORTED_ROMS = {
    "Axion OS": "axion",
    "Lunaris OS": "lunaris",
    "Voltage OS": "voltage",
    "Pixel OS": "pixelos",
}

MAX_RETRIES = 3
# =======================================================


def normalize_name(text):
    """Lowercase and strip every separator (spaces, dots, dashes, underscores)."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def detect_rom(filename):
    """Return the display name if the file belongs to a maintained ROM, else None."""
    normalized = normalize_name(filename)
    for display_name, keyword in SUPPORTED_ROMS.items():
        if keyword in normalized:
            return display_name
    return None


def get_latest_rom():
    """Find the newest ROM zip in the fogos out dir that belongs to a maintained ROM."""
    zip_files = glob.glob(os.path.join(PRODUCT_DIR, "*.zip"))
    if not zip_files:
        print(f"❌ Error: No ZIP files found in {PRODUCT_DIR}")
        exit(1)

    candidates = []
    for zip_path in zip_files:
        rom_name = detect_rom(os.path.basename(zip_path))
        if rom_name:
            candidates.append((zip_path, rom_name))

    if not candidates:
        found = ", ".join(os.path.basename(z) for z in zip_files)
        print(
            "❌ Error: No zip matching a maintained ROM "
            f"({', '.join(SUPPORTED_ROMS)}) was found.\n   Found instead: {found}"
        )
        exit(1)

    latest_zip, rom_name = max(candidates, key=lambda c: os.path.getmtime(c[0]))
    return latest_zip, rom_name


def parse_rom_details(zip_path, rom_display_name):
    """Extract build details from the zip filename and build.prop."""
    filename = os.path.basename(zip_path)

    # Version: a token like 2.4 / 15.0 / 3.1.1 (optionally prefixed with 'v')
    rom_version = None
    for token in re.split(r"[-_]", filename):
        if re.fullmatch(r"[vV]?\d+\.\d+(\.\d+)?", token):
            rom_version = token.lstrip("vV")
            break

    # OFFICIAL / UNOFFICIAL (check UNOFFICIAL first to avoid substring collision)
    if "UNOFFICIAL" in filename.upper():
        build_type = "UNOFFICIAL"
    elif "OFFICIAL" in filename.upper():
        build_type = "OFFICIAL"
    else:
        build_type = "UNKNOWN"

    # Android version from build.prop
    android_version = None
    build_prop_paths = [
        os.path.join(os.path.dirname(zip_path), "system", "build.prop"),
        os.path.join(os.path.dirname(zip_path), "vendor", "build.prop"),
        os.path.join(os.path.dirname(zip_path), "build.prop"),
    ]
    for prop_path in build_prop_paths:
        if os.path.exists(prop_path):
            try:
                with open(prop_path, "r", errors="ignore") as f:
                    match = re.search(r"ro\.build\.version\.release=(\d+)", f.read())
                    if match:
                        android_version = match.group(1)
                        break
            except Exception:
                pass

    # Fallback: Android version from the filename (e.g. a "15" or "15.0" token)
    if not android_version:
        for token in re.split(r"[-_]", filename):
            if re.fullmatch(r"[aA]?1[0-9](\.\d+)?", token):
                android_version = re.sub(r"[aA]", "", token).split(".")[0]
                break

    # Build date: YYYYMMDD in the filename, else the file's mtime
    date_match = re.search(r"(20\d{6})", filename)
    if date_match:
        try:
            build_date = datetime.strptime(date_match.group(1), "%Y%m%d").strftime("%d-%m-%Y")
        except ValueError:
            build_date = datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%d-%m-%Y")
    else:
        build_date = datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%d-%m-%Y")

    bytes_size = os.path.getsize(zip_path)
    file_size = f"{bytes_size / (1024 ** 3):.2f} GB"

    return {
        "filename": filename,
        "rom_name": rom_display_name,
        "version": rom_version or "N/A",
        "type": build_type,
        "android": android_version or "N/A",
        "date": build_date,
        "size": file_size,
    }


def upload_file_to_gofile(file_path):
    """Upload a file to GoFile with automatic server detection."""
    if not os.path.exists(file_path):
        return None
    filename = os.path.basename(file_path)
    print(f"🚀 Starting GoFile upload: {filename}...")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            server_res = requests.get("https://api.gofile.io/servers", timeout=15)
            server_res.raise_for_status()
            server_data = server_res.json()
            if server_data.get("status") != "ok" or not server_data.get("data", {}).get("servers"):
                raise Exception("Failed to retrieve GoFile node.")
            target_server = server_data["data"]["servers"][0]["name"]
            upload_url = f"https://{target_server}.gofile.io/contents/uploadfile"
            with open(file_path, "rb") as f:
                upload_res = requests.post(upload_url, files={"file": f}, timeout=600)
            upload_res.raise_for_status()
            upload_data = upload_res.json()
            if upload_data.get("status") == "ok":
                print(f"✅ GoFile completed: {filename}")
                return upload_data["data"]["downloadPage"]
            raise Exception(f"Server rejected: {upload_data}")
        except Exception as e:
            print(f"⚠️ GoFile attempt {attempt} failed for {filename}: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5)


def upload_file_to_pixeldrain(file_path):
    """Upload a file to Pixeldrain using API key auth (HTTP Basic)."""
    if not os.path.exists(file_path):
        return None
    filename = os.path.basename(file_path)
    print(f"🚀 Starting Pixeldrain upload: {filename}...")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(file_path, "rb") as f:
                upload_res = requests.post(
                    "https://pixeldrain.com/api/file",
                    files={"file": f},
                    auth=("", PIXELDRAIN_API_KEY),
                    timeout=600,
                )
            upload_res.raise_for_status()
            upload_data = upload_res.json()
            if upload_data.get("success") is True:
                print(f"✅ Pixeldrain completed: {filename}")
                return f"https://pixeldrain.com/u/{upload_data.get('id')}"
            raise Exception(f"Pixeldrain rejected: {upload_data}")
        except Exception as e:
            print(f"⚠️ Pixeldrain attempt {attempt} failed for {filename}: {e}")
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5)


def upload_wrapper(file_path):
    """Upload one file to both mirrors and return both URLs."""
    filename = os.path.basename(file_path)
    gofile_url = upload_file_to_gofile(file_path)
    pixeldrain_url = upload_file_to_pixeldrain(file_path)
    return filename, {"gofile": gofile_url or "N/A", "pixeldrain": pixeldrain_url or "N/A"}


def upload_all_assets(device_dir, rom_zip_path):
    """Upload the ROM and images to both mirrors concurrently."""
    files_to_upload = [
        rom_zip_path,
        os.path.join(device_dir, "boot.img"),
        os.path.join(device_dir, "dtbo.img"),
        os.path.join(device_dir, "vendor_boot.img"),
    ]
    existing_files = [f for f in files_to_upload if os.path.exists(f)]
    links = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_file = {executor.submit(upload_wrapper, f): f for f in existing_files}
        for future in as_completed(future_to_file):
            file_path = future_to_file[future]
            try:
                filename, mirror_urls = future.result()
                links[filename] = mirror_urls
            except Exception:
                print(f"❌ Critical Error: Parallel upload pipeline failed for {os.path.basename(file_path)}.")
                exit(1)
    return links


def get_links_str(links, target_name):
    """Build a 'GoFile • Pixeldrain' string for a file, using whatever mirrors succeeded."""
    keys = list(links.keys())
    if target_name == "boot.img":
        # Exclude vendor_boot.img so plain boot.img matches correctly
        match_key = next((k for k in keys if "boot.img" in k and "vendor" not in k), None)
    else:
        match_key = next((k for k in keys if target_name in k), None)

    if not match_key:
        return "Not available"

    parts = []
    if links[match_key]["gofile"] != "N/A":
        parts.append(f"[GoFile]({links[match_key]['gofile']})")
    if links[match_key]["pixeldrain"] != "N/A":
        parts.append(f"[Pixeldrain]({links[match_key]['pixeldrain']})")
    return " • ".join(parts) if parts else "Not available"


def build_message(details, links):
    """Compose the Telegram release post (safe for non-premium formatting limits)."""
    rom = details["rom_name"]
    rom_tag = "#" + rom.replace(" ", "")
    android_tag = f" #Android{details['android']}" if details["android"] != "N/A" else ""

    # Header line: ROM name + version + build type
    header = f"{rom} {details['version']}" if details["version"] != "N/A" else rom
    if details["type"] != "UNKNOWN":
        header = f"{header} | {details['type']}"

    message = (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 *{header}*\n"
        f"📱 *Device*: {DEVICE_NAME}\n\n"
        f"🤖 Android: `{details['android']}`\n"
        f"📅 Build Date: `{details['date']}`\n"
        f"💾 Size: `{details['size']}`\n\n"
        "⬇️ *Downloads*\n\n"
        f"*ROM*\n{get_links_str(links, details['filename'])}\n\n"
        f"*Boot Image*\n{get_links_str(links, 'boot.img')}\n\n"
        f"*DTBO Image*\n{get_links_str(links, 'dtbo.img')}\n\n"
        f"*Vendor Boot Image*\n{get_links_str(links, 'vendor_boot.img')}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Flash at your own risk. Always take a backup before installing.\n"
        f"#fogos {rom_tag}{android_tag}"
    )
    return message


def send_telegram_notification(details, links):
    """Post the release message to the Telegram channel."""
    message = build_message(details, links)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    res = requests.post(url, json=payload, timeout=30)
    if res.status_code == 200:
        print("📢 Telegram notification sent successfully!")
    else:
        print(f"❌ Failed to send Telegram notification: {res.text}")


def main():
    print("🔍 Scanning for fogos builds...")
    rom_zip, rom_display_name = get_latest_rom()
    device_dir = os.path.dirname(rom_zip)
    print(f"📦 Found {rom_display_name} build: {os.path.basename(rom_zip)}")

    details = parse_rom_details(rom_zip, rom_display_name)

    print("🚀 Uploading assets in parallel to GoFile & Pixeldrain...")
    uploaded_links = upload_all_assets(device_dir, rom_zip)

    links_file = "release_links.txt"
    with open(links_file, "w") as f:
        for fname, mirrors in uploaded_links.items():
            f.write(f"{fname}:\n  GoFile: {mirrors['gofile']}\n  Pixeldrain: {mirrors['pixeldrain']}\n")
    print(f"📝 Saved links locally to {links_file}")

    send_telegram_notification(details, uploaded_links)


if __name__ == "__main__":
    main()
