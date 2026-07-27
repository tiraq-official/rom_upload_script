import os
import re
import glob
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

# ==================== CONFIGURATION ====================
TELEGRAM_TOKEN = "8860525999:AAHb4taCWi12n82n2cEU8lhtMRMFOIcnBeE"
TELEGRAM_CHAT_ID = "-1003934445801"
PIXELDRAIN_API_KEY = "02b47b2e-aea7-449c-a3a0-71bd4c1afa7c"
MAX_RETRIES = 3
# =======================================================

def get_latest_rom():
    """Finds the newest ROM ZIP in the current directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    zip_files = glob.glob(os.path.join(script_dir, "*.zip"))
    
    if not zip_files:
        print("❌ Error: No ROM ZIP files found in the current directory.")
        exit(1)
        
    # Sort by modification time to get the newest ZIP file
    latest_zip = max(zip_files, key=os.path.getmtime)
    filename = os.path.basename(latest_zip)
    
    # Try to extract the device codename from standard ROM naming conventions (e.g., ROM-OFFICIAL-codename.zip)
    # Falls back to "Unknown" if it can't detect a pattern
    match = re.search(r"[-_]([a-zA-Z0-9_]+)\.zip$", filename, re.IGNORECASE)
    device_codename = match.group(1) if match else "Unknown"
    
    return latest_zip, device_codename

def parse_rom_details(zip_path, device_codename):
    filename = os.path.basename(zip_path)
    
    # Detect ROM Name
    if "-AOSP" in filename:
        rom_name = filename.split('-')[0] + "-AOSP"
    else:
        rom_name = filename.split('-')[0]
            
    version_match = re.search(r"[-_]([\d\.]+)", filename)
    if version_match:
        rom_name = f"{rom_name} {version_match.group(1)}"
        
    # Detect OFFICIAL / UNOFFICIAL
    if "UNOFFICIAL" in filename.upper():
        build_type = "UNOFFICIAL"
    elif "OFFICIAL" in filename.upper():
        build_type = "OFFICIAL"
    else:
        build_type = "Unknown"

    # Android version attempt from filename pattern (e.g. -13.0- or -14-)
    android_match = re.search(r"[-_](1[2-5])[\.-]", filename)
    android_version = android_match.group(1) if android_match else "Unknown"

    # Detect build date
    date_match = re.search(r"(20\d{6})", filename)
    if date_match:
        raw_date = date_match.group(1)
        try:
            build_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            build_date = datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%Y-%m-%d")
    else:
        build_date = datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%Y-%m-%d")

    # Calculate exact file size
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
    """Uploads a file directly to GoFile with automatic server detection."""
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
            
            with open(file_path, 'rb') as f:
                upload_res = requests.post(upload_url, files={'file': f}, timeout=600)
                
            upload_res.raise_for_status()
            upload_data = upload_res.json()
            
            if upload_data.get("status") == "ok":
                print(f"✅ GoFile completed: {filename}")
                return upload_data["data"]["downloadPage"]
            else:
                raise Exception(f"Server rejected: {upload_data}")
        except Exception as e:
            print(f"⚠️ GoFile attempt {attempt} failed for {filename}: {e}")
            if attempt == MAX_RETRIES: raise e
            time.sleep(5)

def upload_file_to_pixeldrain(file_path):
    """Uploads a file to Pixeldrain using authorized User API Key."""
    if not os.path.exists(file_path):
        return None
        
    filename = os.path.basename(file_path)
    print(f"🚀 Starting Pixeldrain upload: {filename}...")
    
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            upload_url = "https://pixeldrain.com/api/file"
            with open(file_path, 'rb') as f:
                upload_res = requests.post(
                    upload_url, 
                    files={'file': f}, 
                    auth=('', PIXELDRAIN_API_KEY),
                    timeout=600
                )
            upload_res.raise_for_status()
            upload_data = upload_res.json()
            
            if upload_data.get("success") is True:
                file_id = upload_data.get("id")
                print(f"✅ Pixeldrain completed: {filename}")
                return f"https://pixeldrain.com/u/{file_id}"
            else:
                raise Exception(f"Pixeldrain rejected: {upload_data}")
        except Exception as e:
            print(f"⚠️ Pixeldrain attempt {attempt} failed for {filename}: {e}")
            if attempt == MAX_RETRIES: raise e
            time.sleep(5)

def upload_wrapper(file_path):
    """Worker task executing dual uploads concurrently."""
    filename = os.path.basename(file_path)
    gofile_url = upload_file_to_gofile(file_path)
    pixeldrain_url = upload_file_to_pixeldrain(file_path)
    return filename, {"gofile": gofile_url or "N/A", "pixeldrain": pixeldrain_url or "N/A"}

def upload_all_images(rom_zip_path):
    """Checks the current directory for the ROM ZIP and image files."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    files_to_upload = [
        rom_zip_path,
        os.path.join(script_dir, "boot.img"),
        os.path.join(script_dir, "dtbo.img"),
        os.path.join(script_dir, "vendor_boot.img")
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
            except Exception as e:
                print(f"❌ Critical Error: Upload failed for {os.path.basename(file_path)}.")
                exit(1)
    return links

def send_telegram_notification(details, links):
    """Generates release message and posts to Telegram."""
    def get_links_str(target_name):
        match_key = next((k for k in links.keys() if target_name in k), None)
        if target_name == "boot.img":
            match_key = next((k for k in links.keys() if "boot.img" in k and "vendor" not in k), None)
            
        if match_key and links[match_key]["gofile"] != "N/A":
            return f"🔗 [GoFile]({links[match_key]['gofile']}) | [Pixeldrain]({links[match_key]['pixeldrain']})"
        return "N/A"

    rom_links = get_links_str(details["filename"])
    boot_links = get_links_str("boot.img")
    dtbo_links = get_links_str("dtbo.img")
    vendor_boot_links = get_links_str("vendor_boot.img")

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
        f"📦 *ROM*\n{rom_links}\n\n"
        f"💿 *Boot*\n{boot_links}\n\n"
        f"📀 *DTBO*\n{dtbo_links}\n\n"
        f"⚙ *Vendor Boot*\n{vendor_boot_links}\n\n"
        f"#{details['codename']} #Android{details['android']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown", "disable_web_page_preview": True}
    res = requests.post(url, json=payload)
    if res.status_code == 200:
        print("📢 Telegram notification sent successfully!")
    else:        print(f"❌ Failed to send Telegram notification: {res.text}")

def main():
    print("🔍 Scanning current folder for ZIP and image files...")
    rom_zip, codename = get_latest_rom()
    print(f"📦 Found newest ROM: {os.path.basename(rom_zip)} (Codename: {codename})")
    
    details = parse_rom_details(rom_zip, codename)
    
    print("🚀 Uploading assets in parallel to GoFile & Pixeldrain...")
    uploaded_links = upload_all_images(rom_zip)
    
    links_file = "release_links.txt"
    with open(links_file, "w") as f:
        for fname, mirrors in uploaded_links.items():
            f.write(f"{fname}:\n  GoFile: {mirrors['gofile']}\n  Pixeldrain: {mirrors['pixeldrain']}\n")
    print(f"📝 Saved links locally to {links_file}")
    
    send_telegram_notification(details, uploaded_links)

if __name__ == "__main__":
    main()
