#!/usr/bin/env python3
"""
Dynamic ROM release script for Moto G34/G45 5G (fogos).

After a successful ROM build it:
  1. Picks the newest ROM zip in out/target/product/fogos -- ANY ROM, no
     whitelist. Whatever you built last is what gets released.
  2. Works out the ROM name, ROM version and Android version from the build
     output (build.prop) and the ROM source tree, falling back to the zip
     filename when those aren't available.
  3. Uploads the ROM zip + boot / dtbo / vendor_boot images to GoFile and
     Pixeldrain.
  4. Picks one wallpaper at random from the image folder, stamps the ROM name
     onto it in white, and attaches it to an accurate release message posted to
     Telegram (maintainer: tiraq, build type: always UNOFFICIAL).

The terminal shows a live dashboard: an ASCII "ROM UPLOAD" banner, an
"auto upload started" line, the list of everything being uploaded with a tick
appearing as each one finishes, and a moving progress bar at the bottom.

Credentials are read from the environment (recommended) or the CONFIG block
below:
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, PIXELDRAIN_API_KEY

Optional overrides if auto-detection ever gets something wrong:
    ROM_NAME_OVERRIDE, ROM_VERSION_OVERRIDE, ANDROID_VERSION_OVERRIDE

Run with --dry-run to see detection + the dashboard without uploading
anything or touching Telegram.
"""

import os
import re
import sys
import glob
import time
import random
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:  # optional: only needed to stamp the ROM name onto a release image
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False

# ==================== CONFIGURATION ====================
# Fill these in here, or better: export them in your shell before running.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8860525999:AAFLlMQa0eeuKoamBapct3YJAClTVD_Eqwc")          # <-- set me
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1003934445801")
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "020f0c6c-b756-4d48-a31b-410e1d56f1eb")  # <-- set me


MAINTAINER = os.environ.get("MAINTAINER", "tiraq")
BUILD_TYPE = "UNOFFICIAL"  # always unofficial

DEVICE_NAME = os.environ.get("DEVICE_NAME", "Moto G34/G45 5G (fogos)")
PRODUCT_DIR = os.environ.get("PRODUCT_DIR", "../out/target/product/fogos")

# Leave empty to auto-detect.
ROM_NAME_OVERRIDE = os.environ.get("ROM_NAME_OVERRIDE", "")
ROM_VERSION_OVERRIDE = os.environ.get("ROM_VERSION_OVERRIDE", "")
ANDROID_VERSION_OVERRIDE = os.environ.get("ANDROID_VERSION_OVERRIDE", "")

# Release image: one wallpaper is chosen at random from this folder, the ROM
# name is stamped on it in white, and it is attached to the Telegram post.
IMAGES_DIR = os.environ.get("IMAGES_DIR", "release_images")
FONT_PATH = os.environ.get("FONT_PATH", "")  # custom .ttf; empty = auto-pick
TEXT_Y_FRACTION = 0.5  # where the ROM name sits (0 = top, 1 = bottom)

MAX_RETRIES = 3
UPLOAD_WORKERS = 4
# =======================================================

BANNER = r"""
██████╗  ██████╗ ███╗   ███╗   ██╗   ██╗██████╗ ██╗      ██████╗  █████╗ ██████╗
██╔══██╗██╔═══██╗████╗ ████║   ██║   ██║██╔══██╗██║     ██╔═══██╗██╔══██╗██╔══██╗
██████╔╝██║   ██║██╔████╔██║   ██║   ██║██████╔╝██║     ██║   ██║███████║██║  ██║
██╔══██╗██║   ██║██║╚██╔╝██║   ██║   ██║██╔═══╝ ██║     ██║   ██║██╔══██║██║  ██║
██║  ██║╚██████╔╝██║ ╚═╝ ██║   ╚██████╔╝██║     ███████╗╚██████╔╝██║  ██║██████╔╝
╚═╝  ╚═╝ ╚═════╝ ╚═╝     ╚═╝    ╚═════╝ ╚═╝     ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═════╝
"""

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------------------------------------------------------------- terminal

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[97m"
    HIDE_CURSOR = "\033[?25l"
    SHOW_CURSOR = "\033[?25h"
    HOME = "\033[H"
    CLEAR = "\033[2J"
    CLR_EOL = "\033[K"
    CLR_DOWN = "\033[J"


def use_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def paint(text, *codes):
    if not use_color():
        return text
    return "".join(codes) + text + C.RESET


def fmt_time(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def make_bar(frac, width, active, now):
    """A filled progress bar; while active, a highlight slides along it."""
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    cells = ["█"] * filled + ["░"] * (width - filled)
    if active and width > 1:
        pos = int((now * 12) % width)
        cells[pos] = "▓" if cells[pos] == "█" else "▒"
    if use_color():
        bar = "".join(
            paint(ch, C.CYAN if i < filled else C.DIM) for i, ch in enumerate(cells)
        )
    else:
        bar = "".join(cells)
    return "[" + bar + "]"


class Dashboard:
    """Live terminal view: banner + upload list + moving progress bar."""

    def __init__(self, items):
        # items: list of (key, label, sublabel)
        self._lock = threading.RLock()
        self._order = [k for k, _, _ in items]
        self._label = {k: lbl for k, lbl, _ in items}
        self._sub = {k: sub for k, _, sub in items}
        self._status = {k: "pending" for k, _, _ in items}
        self._detail = {k: "" for k, _, _ in items}
        self._header = []
        self._start = time.time()
        self._tty = sys.stdout.isatty()
        self._stop = threading.Event()
        self._thread = None

    def set_header(self, lines):
        with self._lock:
            self._header = list(lines)
            if self._tty:
                self._draw()

    def start(self):
        if self._tty:
            sys.stdout.write(C.CLEAR + C.HOME + C.HIDE_CURSOR)
            sys.stdout.flush()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            for line in self._header:
                print(line)

    def _spin(self):
        while not self._stop.is_set():
            with self._lock:
                self._draw()
            time.sleep(0.1)

    def update(self, key, status, detail=None):
        with self._lock:
            self._status[key] = status
            if detail is not None:
                self._detail[key] = detail
            if self._tty:
                self._draw()
            else:
                sym = {"pending": "○", "active": "…", "done": "✓", "failed": "✗"}
                line = f"  {sym.get(status, '?')} {self._label[key]}"
                if detail:
                    line += f"  {detail}"
                print(line)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
        with self._lock:
            self._draw(final=True)
        if self._tty:
            sys.stdout.write(C.SHOW_CURSOR + "\n")
            sys.stdout.flush()

    def _draw(self, final=False):
        if not self._tty:
            return
        now = time.time()
        elapsed = now - self._start
        lines = list(self._header)

        for key in self._order:
            st = self._status[key]
            if st == "pending":
                sym = paint("○", C.DIM)
            elif st == "active":
                sym = paint(SPINNER[int(now * 10) % len(SPINNER)], C.CYAN, C.BOLD)
            elif st == "done":
                sym = paint("✓", C.GREEN, C.BOLD)
            else:
                sym = paint("✗", C.RED, C.BOLD)

            label = self._label[key]
            detail = self._detail[key]
            tail = ""
            if detail:
                tail = "  " + paint(detail, C.DIM)
            elif self._sub[key] and self._sub[key] != label:
                tail = "  " + paint(self._sub[key], C.DIM)
            lines.append(f"  {sym} {label}{tail}")

        done = sum(1 for k in self._order if self._status[k] in ("done", "failed"))
        total = max(1, len(self._order))
        active = any(self._status[k] == "active" for k in self._order)
        frac = done / total
        bar = make_bar(frac, 44, active and not final, now)
        pct = int(round(frac * 100))

        lines.append("")
        lines.append(
            f"  {bar}  {paint(str(pct) + '%', C.BOLD, C.CYAN)}"
            f"   {done}/{total} files   {paint(fmt_time(elapsed), C.DIM)}"
        )
        if final:
            lines.append("")
            lines.append("  " + paint("✔ All uploads finished.", C.GREEN, C.BOLD))

        frame = C.HOME + "\n".join(l + C.CLR_EOL for l in lines) + C.CLR_DOWN
        sys.stdout.write(frame)
        sys.stdout.flush()


# ---------------------------------------------------------------- helpers

def log(msg):
    print(f"  {paint('•', C.DIM)} {msg}")


def fail(msg, code=1):
    print(f"\n  {paint('✗ ' + msg, C.RED, C.BOLD)}\n")
    sys.exit(code)


def human_size(n):
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{int(val)} B" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024


# ---------------------------------------------------------------- detection

BUILD_PROP_PATHS = [
    "system/build.prop",
    "system/system/build.prop",
    "vendor/build.prop",
    "product/build.prop",
    "odm/build.prop",
    "system_ext/build.prop",
    "build.prop",
]

# Tokens that are never a ROM name in a "ro.<token>.version" key.
_TOKEN_EXCLUDE = {
    "build", "product", "vendor", "odm", "system", "system_ext", "board",
    "boot", "hardware", "kernel", "recovery", "base", "common", "generic",
    "aosp", "config", "custom", "mod", "display", "incremental", "security",
    "sdk", "spl", "patch", "feature", "features", "support", "android",
}

_BRAND_SKIP = {
    "user", "userdebug", "eng", "test", "release", "official", "unofficial",
    "aosp", "generic", "unknown", "dev", "debug", "build", "rom",
}


def read_build_props(product_dir):
    """Merge every build.prop we can find into one dict (first value wins)."""
    props = {}
    for rel in BUILD_PROP_PATHS:
        path = os.path.join(product_dir, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    if key and key not in props:
                        props[key] = value.strip()
        except OSError:
            pass
    return props


def parse_filename(filename, device_slug=None):
    """Best-effort name / version / android / date from the zip filename."""
    base = re.sub(r"\.zip$", "", filename, flags=re.I)
    parts = re.split(r"[-_]", base)

    version = None
    for part in parts:
        if re.fullmatch(r"[vV]?\d+\.\d+(\.\d+)?", part):
            version = part.lstrip("vV")
            break

    android = None
    for part in parts:
        if re.fullmatch(r"[aA]?1[0-9]", part):
            android = re.sub(r"[aA]", "", part)
            break

    date = None
    m = re.search(r"(20\d{6})", base)
    if m:
        try:
            date = datetime.strptime(m.group(1), "%Y%m%d")
        except ValueError:
            date = None

    # Name = everything before the first version / android / date token.
    cut = len(base)
    for pat in (r"[-_]?[vV]?\d+\.\d+", r"[-_][aA]?1[0-9](?=[-_]|$)", r"[-_]?20\d{6}"):
        for mm in re.finditer(pat, base):
            cut = min(cut, mm.start())
    if device_slug:
        idx = base.lower().find(device_slug.lower())
        if idx > 0:
            cut = min(cut, idx)
    name = re.sub(r"[_-]+", " ", base[:cut]).strip()
    return {"name": name or None, "version": version, "android": android, "date": date}


def detect_rom_token_version(props):
    """Find (token, version) from a ROM-specific prop like ro.axion.version."""
    best = None
    for key, value in props.items():
        m = re.match(r"^ro\.([a-z0-9_]+)\.(?:build\.|display\.)?version$", key)
        if not m:
            continue
        token = m.group(1)
        if token in _TOKEN_EXCLUDE or not re.search(r"\d", value):
            continue
        score = 0
        if any(k.startswith(f"ro.{token}.") for k in props):
            score += 1
        if re.match(r"^\d", value):
            score += 1
        if best is None or score > best[0]:
            best = (score, token, value)
    if best:
        return best[1], best[2]
    return None, None


def guess_brand(props, token, device_slug, filename=None):
    """Pick a human ROM name out of the build ids / filename / prop values.

    Candidates are tried in order of trust: the build id/flavor props first,
    then the zip filename, then any other prop that mentions the ROM token.
    The first candidate that yields a match wins, so a clean build id is never
    overridden by a noisier source.
    """
    candidates = []
    for key in ("ro.build.display.id", "ro.build.flavor", "ro.product.build.flavor",
                "ro.build.id", "ro.modversion", "ro.build.description"):
        if props.get(key):
            candidates.append(props[key])
    if filename:
        candidates.append(re.sub(r"\.zip$", "", filename, flags=re.I))
    if token:
        for key, value in props.items():
            if token in key.lower() or token in value.lower():
                candidates.append(value)

    skip = set(_BRAND_SKIP)
    if device_slug:
        skip.add(device_slug.lower())

    best_overall = None
    for text in candidates:
        bare = None
        for word in re.findall(r"[A-Za-z][A-Za-z0-9\.]*", text):
            wl = word.lower()
            if wl in skip or re.match(r"^v?\d", wl):
                continue
            if token:
                if wl.startswith(token):
                    if len(wl) > len(token):
                        return word          # e.g. AxionOS / PixelOS / VoltageOS
                    bare = bare or word
            elif best_overall is None or len(word) > len(best_overall):
                best_overall = word
        if bare:
            return bare.title() if bare.islower() else bare

    if token:
        return token.title()
    return best_overall


def extract_version_from_ids(props):
    for key in ("ro.build.version.incremental", "ro.build.display.id",
                "ro.build.id", "ro.modversion"):
        value = props.get(key)
        if not value:
            continue
        m = re.search(r"[vV]?(\d+\.\d+(?:\.\d+)?)", value)
        if m:
            return m.group(1)
    return None


def detect_android_version(props, filename):
    if ANDROID_VERSION_OVERRIDE:
        return ANDROID_VERSION_OVERRIDE
    for key in ("ro.build.version.release", "ro.system.build.version.release",
                "ro.build.version.release_or_codename",
                "ro.vendor.build.version.release"):
        value = props.get(key)
        if value:
            m = re.match(r"^\d+", value)
            if m:
                return m.group(0)
    return parse_filename(filename)["android"] or "N/A"


def detect_build_date(props, zip_path, fname_date):
    epoch = props.get("ro.build.date.utc")
    if epoch and epoch.isdigit():
        try:
            return datetime.fromtimestamp(int(epoch)).strftime("%d-%m-%Y")
        except (ValueError, OSError, OverflowError):
            pass
    if fname_date:
        return fname_date.strftime("%d-%m-%Y")
    return datetime.fromtimestamp(os.path.getmtime(zip_path)).strftime("%d-%m-%Y")


def find_source_root():
    """Walk up from cwd looking for the Android source tree root."""
    cur = os.path.abspath(os.getcwd())
    for _ in range(8):
        if os.path.isdir(os.path.join(cur, ".repo")) or \
           os.path.isdir(os.path.join(cur, "build", "make")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.path.join(os.getcwd(), ".."))


def manifest_brand(source_root, token):
    """Read the ROM org out of the repo manifest as a last-resort name."""
    if not source_root:
        return None
    paths = []
    mdir = os.path.join(source_root, ".repo", "manifests")
    if os.path.isdir(mdir):
        paths += [os.path.join(mdir, f) for f in os.listdir(mdir) if f.endswith(".xml")]
    for rel in (".repo/manifest.xml",):
        p = os.path.join(source_root, rel)
        if os.path.exists(p):
            paths.append(p)
    if not paths:
        return None

    cleaned = []
    for path in paths:
        try:
            text = open(path, "r", errors="ignore").read()
        except OSError:
            continue
        for url in re.findall(r'fetch\s*=\s*"([^"]+)"', text):
            seg = url.rstrip("/").split("/")[-1]
            seg = re.sub(r"(?i)(android[_-]?manifest|manifest|android|platform|vendor)",
                         "", seg).strip("-_.")
            seg = re.sub(r"(?i)aosp$", "", seg).strip("-_.")
            if seg:
                cleaned.append(seg)
    if not cleaned:
        return None
    if token:
        for seg in cleaned:
            if token in seg.lower():
                return seg
    return max(set(cleaned), key=cleaned.count)


def detect_rom_details(zip_path, props, source_root):
    filename = os.path.basename(zip_path)
    device_slug = os.path.basename(os.path.normpath(PRODUCT_DIR)) or "fogos"
    fname = parse_filename(filename, device_slug)
    token, token_version = detect_rom_token_version(props)

    name = ROM_NAME_OVERRIDE or guess_brand(props, token, device_slug, filename) \
        or manifest_brand(source_root, token) or fname["name"]

    version = ROM_VERSION_OVERRIDE or token_version \
        or extract_version_from_ids(props) or fname["version"]

    android = detect_android_version(props, filename)
    date = detect_build_date(props, zip_path, fname["date"])

    return {
        "filename": filename,
        "rom_name": name or "Unknown ROM",
        "version": version or "N/A",
        "type": BUILD_TYPE,
        "android": android or "N/A",
        "date": date,
        "size": human_size(os.path.getsize(zip_path)),
        "maintainer": MAINTAINER,
    }


def find_latest_zip(product_dir):
    zips = glob.glob(os.path.join(product_dir, "*.zip"))
    if not zips:
        fail(f"No .zip files found in {product_dir}")
    return max(zips, key=os.path.getmtime)


# ---------------------------------------------------------------- uploads

def upload_file_to_gofile(file_path):
    filename = os.path.basename(file_path)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.get("https://api.gofile.io/servers", timeout=15)
            res.raise_for_status()
            data = res.json()
            servers = data.get("data", {}).get("servers") or []
            if data.get("status") != "ok" or not servers:
                raise Exception("could not get a GoFile server")
            server = servers[0]["name"]
            with open(file_path, "rb") as fh:
                up = requests.post(
                    f"https://{server}.gofile.io/contents/uploadfile",
                    files={"file": fh}, timeout=600,
                )
            up.raise_for_status()
            up_data = up.json()
            if up_data.get("status") == "ok":
                return up_data["data"]["downloadPage"]
            raise Exception(f"server rejected: {up_data}")
        except Exception as exc:  # noqa: BLE001 - retried below
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5)


def upload_file_to_pixeldrain(file_path):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(file_path, "rb") as fh:
                res = requests.post(
                    "https://pixeldrain.com/api/file",
                    files={"file": fh},
                    auth=("", PIXELDRAIN_API_KEY),
                    timeout=600,
                )
            res.raise_for_status()
            data = res.json()
            if data.get("success") is True:
                return f"https://pixeldrain.com/u/{data.get('id')}"
            raise Exception(f"rejected: {data}")
        except Exception:  # noqa: BLE001 - retried below
            if attempt == MAX_RETRIES:
                raise
            time.sleep(5)


def upload_one(file_path, dash, key, dry_run=False):
    """Upload one file to both mirrors, updating the dashboard as we go."""
    name = os.path.basename(file_path)
    dash.update(key, "active", "uploading to GoFile + Pixeldrain…")

    if dry_run:
        time.sleep(1.5)
        dash.update(key, "done", "GoFile + Pixeldrain ✓ (dry run)")
        return name, {"gofile": "https://gofile.io/d/DRYRUN", "pixeldrain": "https://pixeldrain.com/u/DRYRUN"}

    result = {"gofile": "N/A", "pixeldrain": "N/A"}
    try:
        result["gofile"] = upload_file_to_gofile(file_path) or "N/A"
    except Exception as exc:  # noqa: BLE001
        log(f"GoFile failed for {name}: {exc}")
    try:
        result["pixeldrain"] = upload_file_to_pixeldrain(file_path) or "N/A"
    except Exception as exc:  # noqa: BLE001
        log(f"Pixeldrain failed for {name}: {exc}")

    mirrors = [m for m in ("gofile", "pixeldrain") if result[m] != "N/A"]
    if not mirrors:
        dash.update(key, "failed", "both mirrors failed")
    else:
        dash.update(key, "done", " + ".join(m.capitalize() for m in mirrors) + " ✓")
    return name, result


# ---------------------------------------------------------------- release image

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def load_font(size):
    """A bold sans font for the ROM name, falling back to whatever exists."""
    for path in ([FONT_PATH] if FONT_PATH else []) + FONT_CANDIDATES:
        if path and os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    for name in ("DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def pick_random_image(images_dir):
    """Return a random wallpaper from the folder, or None if there are none."""
    if not os.path.isdir(images_dir):
        return None
    files = [os.path.join(images_dir, f) for f in os.listdir(images_dir)
             if f.lower().endswith(IMAGE_EXTS)]
    return random.choice(files) if files else None


def make_release_image(bg_path, rom_name, out_path="release_image.jpg"):
    """Stamp the ROM name in white onto a copy of the wallpaper."""
    img = Image.open(bg_path).convert("RGBA")
    width, height = img.size
    size = max(28, int(width * 0.10))
    font = load_font(size)

    bbox = ImageDraw.Draw(img).textbbox((0, 0), rom_name, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2 - bbox[0]
    y = int(height * TEXT_Y_FRACTION) - th // 2 - bbox[1]

    # A soft drop shadow keeps the white text readable over bright artwork.
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (x + size * 0.05, y + size * 0.05), rom_name, font=font, fill=(0, 0, 0, 190)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(size * 0.035))
    img = Image.alpha_composite(img, shadow)

    ImageDraw.Draw(img).text((x, y), rom_name, font=font, fill=(255, 255, 255, 255))
    img.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


# ---------------------------------------------------------------- telegram

def get_links_str(links, target_name):
    keys = list(links.keys())
    if target_name == "boot.img":
        match = next((k for k in keys if "boot.img" in k and "vendor" not in k), None)
    else:
        match = next((k for k in keys if target_name in k), None)
    if not match:
        return "Not available"

    entry = links[match]
    parts = []
    if entry.get("gofile", "N/A") != "N/A":
        parts.append(f"[GoFile]({entry['gofile']})")
    if entry.get("pixeldrain", "N/A") != "N/A":
        parts.append(f"[Pixeldrain]({entry['pixeldrain']})")
    return " • ".join(parts) if parts else "Not available"


def build_message(details, links):
    rom = details["rom_name"]
    rom_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", rom)
    android_tag = f" #Android{details['android']}" if details["android"] != "N/A" else ""

    header = f"{rom} {details['version']}".strip()
    if details["type"]:
        header = f"{header} | {details['type']}"

    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        f"📦 *{header}*",
        f"📱 *Device*: {DEVICE_NAME}",
        "",
        f"🤖 Android: `{details['android']}`",
        f"📅 Build Date: `{details['date']}`",
        f"💾 Size: `{details['size']}`",
        f"👤 Maintainer: `{details['maintainer']}`",
        "",
        "⬇️ *Downloads*",
        "",
        f"*ROM*\n{get_links_str(links, details['filename'])}",
        "",
        f"*Boot Image*\n{get_links_str(links, 'boot.img')}",
        "",
        f"*DTBO Image*\n{get_links_str(links, 'dtbo.img')}",
        "",
        f"*Vendor Boot Image*\n{get_links_str(links, 'vendor_boot.img')}",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "⚠️ Flash at your own risk. Always take a backup before installing.",
        f"#fogos {rom_tag}{android_tag} #{MAINTAINER}",
    ]
    return "\n".join(lines)


def send_telegram_text(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    res = requests.post(url, json=payload, timeout=30)
    if res.status_code == 200:
        log("Telegram message sent.")
    else:
        log(paint(f"Telegram send failed: {res.text}", C.RED))


def send_telegram_photo(image_path, caption):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(image_path, "rb") as fh:
            res = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption,
                      "parse_mode": "Markdown"},
                files={"photo": fh},
                timeout=120,
            )
        if res.status_code == 200:
            return True
        log(paint(f"sendPhoto failed: {res.text}", C.RED))
    except Exception as exc:  # noqa: BLE001
        log(paint(f"sendPhoto error: {exc}", C.RED))
    return False


def send_telegram_notification(details, links, image_path=None):
    """Post the release. Attach the stamped image when we have one."""
    message = build_message(details, links)

    if image_path and os.path.isfile(image_path):
        if len(message) <= 1024:  # Telegram's photo-caption limit
            if send_telegram_photo(image_path, message):
                log("Telegram photo + release message sent.")
                return
        else:
            # Too long for a caption: photo first, full details as a message.
            short = (f"📦 *{details['rom_name']} {details['version']} | "
                     f"{details['type']}*\n📱 {DEVICE_NAME}\n"
                     f"🤖 Android {details['android']}")
            send_telegram_photo(image_path, short)
            log("Photo sent; posting the full details as text.")

    send_telegram_text(message)


# ---------------------------------------------------------------- main

def build_header(details, include_banner=True):
    stamp = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
    lines = [""]
    if include_banner:
        lines += [paint(BANNER.rstrip("\n"), C.CYAN, C.BOLD), ""]
    lines += [
        f"  {paint('🚀 Auto upload started', C.GREEN, C.BOLD)}  "
        f"{paint('•  ' + stamp, C.DIM)}",
        f"  📦 {paint(details['rom_name'], C.BOLD)} "
        f"{details['version']}  {paint('•', C.DIM)}  Android {details['android']}"
        f"  {paint('•', C.DIM)}  {details['type']}"
        f"  {paint('•', C.DIM)}  Maintainer: {details['maintainer']}",
        f"  📱 {DEVICE_NAME}",
        "",
        f"  {paint('Upload queue', C.BOLD)}",
    ]
    return lines


def main():
    dry_run = "--dry-run" in sys.argv

    print(paint(BANNER, C.CYAN, C.BOLD))
    print(f"  {paint('Scanning for the latest fogos build…', C.DIM)}")

    zip_path = find_latest_zip(PRODUCT_DIR)
    product_dir = os.path.dirname(zip_path)
    props = read_build_props(product_dir)
    details = detect_rom_details(zip_path, props, find_source_root())

    if not TELEGRAM_TOKEN and not dry_run:
        print(f"  {paint('⚠ TELEGRAM_TOKEN is empty — the post will fail.', C.YELLOW)}")
    if not PIXELDRAIN_API_KEY and not dry_run:
        print(f"  {paint('⚠ PIXELDRAIN_API_KEY is empty — Pixeldrain will fail.', C.YELLOW)}")

    # Which assets exist?
    candidates = [
        ("rom", "ROM package", zip_path),
        ("boot", "Boot image", os.path.join(product_dir, "boot.img")),
        ("dtbo", "DTBO image", os.path.join(product_dir, "dtbo.img")),
        ("vboot", "Vendor boot image", os.path.join(product_dir, "vendor_boot.img")),
    ]
    items = [(k, lbl, os.path.basename(p)) for k, lbl, p in candidates if os.path.exists(p)]
    paths = {k: p for k, _, p in candidates}

    if not items:
        fail("Nothing to upload.")

    dash = Dashboard(items)
    # In a real terminal the dashboard redraws the banner itself; when output is
    # piped/logged we already printed it above, so don't repeat it.
    dash.set_header(build_header(details, include_banner=sys.stdout.isatty()))
    dash.start()

    links = {}
    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        futures = {
            pool.submit(upload_one, paths[k], dash, k, dry_run): k for k, _, _ in items
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                name, result = future.result()
                links[name] = result
            except Exception as exc:  # noqa: BLE001
                log(f"upload crashed for {key}: {exc}")
                dash.update(key, "failed", "crashed")

    dash.stop()

    with open("release_links.txt", "w") as fh:
        for fname, mirrors in links.items():
            fh.write(f"{fname}:\n")
            fh.write(f"  GoFile: {mirrors.get('gofile', 'N/A')}\n")
            fh.write(f"  Pixeldrain: {mirrors.get('pixeldrain', 'N/A')}\n")
    log("Saved links to release_links.txt")

    # Pick a random wallpaper and stamp the ROM name onto it.
    image_path = None
    background = pick_random_image(IMAGES_DIR)
    if background is None:
        log(f"No wallpapers in '{IMAGES_DIR}/' — sending text only.")
    elif not _PIL_AVAILABLE:
        log("Pillow not installed — skipping the release image (pip install Pillow).")
    else:
        try:
            image_path = make_release_image(background, details["rom_name"])
            log(f"Release image: {os.path.basename(background)} "
                f"+ \"{details['rom_name']}\" -> {image_path}")
        except Exception as exc:  # noqa: BLE001
            log(f"Could not build the release image: {exc}")

    if dry_run:
        log("Dry run — Telegram post skipped. Message would be:\n")
        print(build_message(details, links))
        return

    send_telegram_notification(details, links, image_path)


if __name__ == "__main__":
    main()
