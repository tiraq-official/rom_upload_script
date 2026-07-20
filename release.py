import os
import re
import glob
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = "8860525999:AAHb4taCWi12n82n2cEU8lhtMRMFOIcnBeE"
TELEGRAM_CHAT_ID = "-1003927020988"
PRODUCT_DIR = "../out/target/product"
MAX_RETRIES = 3
# =======================================================

def get_latest_rom():
    """1 & 2: Automatically find the newest ROM ZIP and detect codename folder."""
    search_path = os.path.join(PRODUCT_DIR, "*", "*.zip")
    zip_files = glob.glob(search_path)
    
    if not zip_files:
        print("❌ Error: No ROM ZIP files found in out/target/product/*")
        exit(1)
        
    # Sort by modification time to get the newest
    latest_zip = max(zip_files, key=os.path.getmtime)
    
    # Extract codename from the path component right before the filename
    path_parts = os.path.normpath(latest_zip).split(os.sep)
    device_codename = path_parts[-2]
    
    return latest_zip, device_codename

def parse_rom_details(zip_path, device_codename):
    filename = os.path.basename(zip_path)
    
    # 3. Detect ROM Name & Version dynamically
    # Captures everything up to the device name/status, and grabs the version number later in the string
    if "-AOSP" in filename:
        rom_name = filename.split('-')[0] + "-AOSP"
    else:
        rom_name = filename.split('-')[0]
        
    version_match = re.search(r"[-_]([\d\.]+)", filename)
    if version_match:
        rom_name = f"{rom_name} {version_match.group(1)}"

    # 4. Detect OFFICIAL / UNOFFICIAL (Swapped order to prevent substring collision)
    if "UNOFFICIAL" in filename.upper():
        build_type = "UNOFFICIAL"
    elif "OFFICIAL" in filename.upper():
        build_type = "OFFICIAL"
    else:
        build_type = "Unknown"

    # 5. Detect Android version from build.prop
    android_version = "Unknown"
    device_dir = os.path.dirname(zip_path)
    build_prop_paths = [
        os.path.join(device_dir, "system", "build.prop"),
        os.path.join(device_dir, "vendor", "build.prop"),
        os.path.join(device_dir, "build.prop")
    ]
    for prop_path in build_prop_paths:
        if os.path.exists(prop_path):
            try:
                with open(prop_path, 'r', errors='ignore') as f:
                    content = f.read()
                    match = re.search(r"ro\.build\.version\.release=(\d+)", content)
                    if match:
                        android_version = match.group(1)
                        break
            except Exception:
                pass

    # 6. Detect build date
    date_match = re.search(r"(20\d{6})", filename)
    if date_match:
        raw_date = date_match.group(1)
        try:
            build_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            build_date = datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%Y-%m-%d")
    else:
        build_date = datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%Y-%m-%d")

    # 7. Calculate exact file size
    bytes_size = os.path.getsize(zip_path)
    gb_size = bytes_size / (1024 ** 3)
    file_size = f"{gb_size:.2f} GB"

    return {
        "filename": filename,
        "codename": device_codename,
        "rom_name": rom_name,
        "type": build_type,
        "android": android_version,
        "date": build_date,
        "size": file_size
    }

def upload_file_to_gofile(file_path):
    """8 & 9: Uploads a file directly to GoFile with automatic server detection and retries."""
    if not os.path.exists(file_path):
        return os.path.basename(file_path), None

    filename = os.path.basename(file_path)
    print(f"🚀 Starting GoFile upload: {filename}...")
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Step 1: Query GoFile to find the best current available server
            server_res = requests.get("https://api.gofile.io/servers", timeout=15)
            server_res.raise_for_status()
            server_data = server_res.json()
            
            if server_data.get("status") != "ok" or not server_data.get("data", {}).get("servers"):
                raise Exception("Failed to retrieve an active GoFile server node.")
                
            # Pick the first available target server node
            target_server = server_data["data"]["servers"][0]["name"]
            upload_url = f"https://{target_server}.gofile.io/contents/uploadfile"
            
            # Step 2: Stream file up to the selected node
            with open(file_path, 'rb') as f:
                upload_res = requests.post(upload_url, files={'file': f}, timeout=600)
                
            upload_res.raise_for_status()
            upload_data = upload_res.json()
            
            if upload_data.get("status") == "ok":
                download_url = upload_data["data"]["downloadPage"]
                print(f"✅ Successfully uploaded {filename} to GoFile!")
                return filename, download_url
            else:
                raise Exception(f"GoFile server rejected upload: {upload_data}")
                
        except Exception as e:
            print(f"⚠️ Attempt {attempt} failed for {filename}: {e}")
            if attempt == MAX_RETRIES:
                raise e
            time.sleep(5)

def upload_all_images(device_dir, rom_zip_path):
    """Orchestrates parallel uploads of the ZIP and structural image assets."""
    files_to_upload = [
        rom_zip_path,
        os.path.join(device_dir, "boot.img"),
        os.path.join(device_dir, "dtbo.img"),
        os.path.join(device_dir, "vendor_boot.img")
    ]
    
    existing_files = [f for f in files_to_upload if os.path.exists(f)]
    links = {}
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_file = {executor.submit(upload_file_to_gofile, f): f for f in existing_files}
        
        for future in as_completed(future_to_file):
            file_path = future_to_file[future]
            try:
                filename, url = future.result()
                if url:
                    links[filename] = url
            except Exception as e:
                print(f"❌ Critical Error: Parallel upload completely failed for {os.path.basename(file_path)}.")
                exit(1)
                
    return links

def send_telegram_notification(details, links):
    """11: Formats and fires off the Telegram notification payload."""
    rom_url = links.get(details["filename"], "N/A")
    boot_url = next((v for k, v in links.items() if "boot.img" in k and "vendor" not in k), "N/A")
    dtbo_url = next((v for k, v in links.items() if "dtbo.img" in k), "N/A")
    vendor_boot_url = next((v for k, v in links.items() if "vendor_boot.img" in k), "N/A")

    message = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📦 *ROM Build Completed*\n\n"
        f"📱 *Device* : `{details['codename']}`\n"
        f"🎯 *ROM*    : `{details['rom_name']}`\n"
        f"🤖 *Android*: `{details['android']}`\n"
        f"🏷 *Type*    : `{details['type']}`\n"
        f"📅 *Build*   : `{details['date']}`\n"
        f"💾 *Size*    : `{details['size']}`\n\n"
        "⬇ *Downloads*\n\n"
        f"📦 *ROM*\n{rom_url}\n\n"
        f"💿 *Boot*\n{boot_url}\n\n"
        f"📀 *DTBO*\n{dtbo_url}\n\n"
        f"⚙ *Vendor Boot*\n{vendor_boot_url}\n\n"
        f"#{details['codename']} #Android{details['android']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    res = requests.post(url, json=payload)
    if res.status_code == 200:
        print("📢 Telegram notification sent successfully!")
    else:
        print(f"❌ Failed to send Telegram notification: {res.text}")

def main():
    print("🔍 Scanning for builds...")
    rom_zip, codename = get_latest_rom()
    device_dir = os.path.dirname(rom_zip)
    
    print(f"📦 Found newest ROM: {rom_zip} ({codename})")
    details = parse_rom_details(rom_zip, codename)
    
    print("🚀 Uploading assets in parallel to GoFile...")
    uploaded_links = upload_all_images(device_dir, rom_zip)
    
    links_file = "release_links.txt"
    with open(links_file, "w") as f:
        for fname, url in uploaded_links.items():
            f.write(f"{fname}: {url}\n")
    print(f"📝 Saved links locally to {links_file}")
    
    send_telegram_notification(details, uploaded_links)

if __name__ == "__main__":
    main()
