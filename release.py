#!/usr/bin/env python3
"""
Dynamic ROM release & upload script for Moto G34/G45 5G (fogos).

Features:
  1. Scans the server for the Android ROM source root and the latest
     out/target/product/fogos build output.
  2. Picks the newest FULL ROM zip (ignoring incremental, OTA, fastboot,
     and target-files zips) along with boot.img and dtbo.img.
  3. Detects ROM name, version, Android version, build date, and build time
     from build.prop, source manifest, and filename.
  4. Streams uploads at full server bandwidth (4 MB chunked streaming +
     parallel workers) to both Pixeldrain and GoFile, retrying any failed
     upload up to 3 times. If any upload still fails, stops immediately with
     "Something's wrong, try again later."
  5. Displays a terminal UI: ROM-UPLOAD ASCII banner, detected ROM info,
     loading screen, live per-file upload progress bars + speeds, and prints
     all final links.
  6. Posts a compact, minimal Telegram release message with:
       - Minimal ASCII art ROM banner (strictly <= 41 chars per line)
       - Build time, date, Unofficial tag, "by tiraq"
       - Short flashing warning
       - Download-only inline URL buttons (Pixeldrain & GoFile)

Usage:
  python3 release.py            # full scan, upload, and Telegram post
  python3 release.py --dry-run  # test detection, UI, and Telegram preview
  python3 release.py --debug    # print extra detection diagnostics
"""

import difflib
import html
import json
import os
import re
import tempfile
import urllib.parse
import zipfile
import sys
import glob
import time
import uuid
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter

# ==================== CONFIGURATION ====================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PIXELDRAIN_API_KEY = os.environ.get("PIXELDRAIN_API_KEY", "")

MAINTAINER = os.environ.get("MAINTAINER", "tiraq")
BUILD_TYPE = "Unofficial"
DEVICE_CODENAME = os.environ.get("DEVICE_CODENAME", "fogos")
DEVICE_NAME = os.environ.get("DEVICE_NAME", "Moto G34/G45 5G (fogos)")

# Optional explicit overrides
PRODUCT_DIR_OVERRIDE = os.environ.get("PRODUCT_DIR", "")
ROM_NAME_OVERRIDE = os.environ.get("ROM_NAME_OVERRIDE", "")
ROM_VERSION_OVERRIDE = os.environ.get("ROM_VERSION_OVERRIDE", "")
ANDROID_VERSION_OVERRIDE = os.environ.get("ANDROID_VERSION_OVERRIDE", "")

MAX_RETRIES = 3
CHUNK_SIZE = 4 * 1024 * 1024   # 4 MB streaming chunks for full bandwidth
UPLOAD_WORKERS = 6             # 3 files x 2 mirrors concurrently
BANNER_REPO = os.environ.get("BANNER_REPO", "tiraq-official/banner_repo")
# =======================================================

ROM_UPLOAD_BANNER = r"""
 _ __ ___  _ __ ___        _   _ _ __ | | ___   __ _  __| |
| '__/ _ \| '_ ` _ \ _____| | | | '_ \| |/ _ \ / _` |/ _` |
| | | (_) | | | | | |_____| |_| | |_) | | (_) | (_| | (_| |
|_|  \___/|_| |_| |_|      \__,_| .__/|_|\___/ \__,_|\__,_|
                                |_|
"""

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------------------------------------------------------------- terminal UI

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
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


def human_size(n):
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{int(val)} B" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024
    return f"{val:.2f} TB"


def fmt_time(sec):
    sec = max(0, int(sec))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def make_bar(frac, width=28):
    frac = max(0.0, min(1.0, frac))
    filled = int(round(frac * width))
    if use_color():
        bar = paint("█" * filled, C.CYAN) + paint("░" * (width - filled), C.DIM)
    else:
        bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


def show_loading_screen(rom_label, steps):
    """Brief animated loading screen while preparing files & upload servers."""
    tty = sys.stdout.isatty()
    for msg, fn in steps:
        if tty:
            done_evt = threading.Event()
            err_box = [None]
            res_box = [None]

            def runner():
                try:
                    res_box[0] = fn()
                except Exception as exc:  # noqa: BLE001
                    err_box[0] = exc
                finally:
                    done_evt.set()

            t = threading.Thread(target=runner, daemon=True)
            t.start()
            i = 0
            while not done_evt.is_set():
                sp = paint(SPINNER[i % len(SPINNER)], C.CYAN, C.BOLD)
                sys.stdout.write(f"\r  {sp} {msg}…{C.CLR_EOL}")
                sys.stdout.flush()
                i += 1
                time.sleep(0.08)
            t.join()
            if err_box[0]:
                sys.stdout.write(f"\r  {paint('✗', C.RED, C.BOLD)} {msg}{C.CLR_EOL}\n")
                sys.stdout.flush()
                raise err_box[0]
            sys.stdout.write(f"\r  {paint('✓', C.GREEN, C.BOLD)} {msg}{C.CLR_EOL}\n")
            sys.stdout.flush()
        else:
            print(f"  … {msg}")
            fn()


class UploadDashboard:
    """Live per-file upload progress dashboard with speed & mirror status."""

    def __init__(self, header_lines, tasks):
        # tasks: list of (task_id, file_label, mirror_label, total_bytes)
        self._lock = threading.RLock()
        self._header = list(header_lines)
        self._order = [tid for tid, _, _, _ in tasks]
        self._meta = {
            tid: {"file": fl, "mirror": ml, "total": max(1, tb)}
            for tid, fl, ml, tb in tasks
        }
        self._state = {
            tid: {
                "status": "pending",
                "sent": 0,
                "speed": 0.0,
                "attempt": 1,
                "note": "queued",
                "t0": None,
            }
            for tid in self._order
        }
        self._tty = sys.stdout.isatty()
        self._start = time.time()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._tty:
            sys.stdout.write(C.CLEAR + C.HOME + C.HIDE_CURSOR)
            sys.stdout.flush()
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        else:
            for line in self._header:
                print(line)

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                self._draw()
            time.sleep(0.1)

    def set_status(self, tid, status, note="", attempt=1):
        with self._lock:
            st = self._state[tid]
            st["status"] = status
            st["note"] = note
            st["attempt"] = attempt
            if status == "active" and st["t0"] is None:
                st["t0"] = time.time()
                st["sent"] = 0
                st["speed"] = 0.0
            if status == "done":
                st["sent"] = self._meta[tid]["total"]
            if not self._tty and status in ("done", "failed"):
                m = self._meta[tid]
                sym = "✓" if status == "done" else "✗"
                print(f"  {sym} {m['file']} [{m['mirror']}] {note}")

    def update_progress(self, tid, sent_bytes):
        with self._lock:
            st = self._state[tid]
            now = time.time()
            st["sent"] = min(sent_bytes, self._meta[tid]["total"])
            if st["t0"]:
                dt = max(0.05, now - st["t0"])
                st["speed"] = st["sent"] / dt

    def stop(self, failed=False):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        with self._lock:
            self._draw(final=True, failed=failed)
        if self._tty:
            sys.stdout.write(C.SHOW_CURSOR + "\n")
            sys.stdout.flush()

    def _draw(self, final=False, failed=False):
        if not self._tty:
            return
        now = time.time()
        lines = list(self._header)
        total_bytes_all = sum(self._meta[t]["total"] for t in self._order)
        sent_bytes_all = sum(self._state[t]["sent"] for t in self._order)

        for tid in self._order:
            m = self._meta[tid]
            st = self._state[tid]
            status = st["status"]
            if status == "pending":
                sym = paint("○", C.DIM)
            elif status == "active":
                sym = paint(SPINNER[int(now * 10) % len(SPINNER)], C.CYAN, C.BOLD)
            elif status == "done":
                sym = paint("✓", C.GREEN, C.BOLD)
            else:
                sym = paint("✗", C.RED, C.BOLD)

            frac = st["sent"] / m["total"]
            pct = int(round(frac * 100))
            bar = make_bar(frac, width=22)
            label = f"{m['file']:<14} {paint(m['mirror'], C.DIM):<19}"

            if status == "active":
                spd = f"{st['speed'] / (1024 * 1024):.1f} MB/s" if st["speed"] > 0 else "…"
                retry_tag = f" (try {st['attempt']}/{MAX_RETRIES})" if st["attempt"] > 1 else ""
                info = f"{pct:3d}%  {human_size(st['sent'])}/{human_size(m['total'])}  {spd}{retry_tag}"
            elif status == "done":
                info = paint(f"100%  {human_size(m['total'])}  done", C.GREEN)
            elif status == "failed":
                info = paint(st["note"] or "failed", C.RED)
            else:
                info = paint(st["note"], C.DIM)

            lines.append(f"  {sym} {label} {bar} {info}")

        overall_frac = sent_bytes_all / max(1, total_bytes_all)
        overall_pct = int(round(overall_frac * 100))
        done_cnt = sum(1 for t in self._order if self._state[t]["status"] == "done")
        lines.append("")
        lines.append(
            f"  Total: {make_bar(overall_frac, width=32)}  "
            f"{paint(f'{overall_pct}%', C.BOLD, C.CYAN)}  "
            f"({done_cnt}/{len(self._order)} uploads)  "
            f"{paint(fmt_time(now - self._start), C.DIM)}"
        )
        if final:
            lines.append("")
            if failed:
                lines.append("  " + paint("✗ Upload failed.", C.RED, C.BOLD))
            else:
                lines.append("  " + paint("✔ All uploads completed.", C.GREEN, C.BOLD))

        frame = C.HOME + "\n".join(l + C.CLR_EOL for l in lines) + C.CLR_DOWN
        sys.stdout.write(frame)
        sys.stdout.flush()


# ---------------------------------------------------------------- ROM discovery

def is_non_full_zip(path):
    """Return True for incremental, fastboot, or target-files zips."""
    name = os.path.basename(path).lower()
    return any(tok in name for tok in (
        "incremental", "target_files", "target-files",
        "-img.zip", "fastboot", "super_empty", "symbols",
    ))


def _has_ota_token(path):
    name = os.path.basename(path).lower()
    return bool(re.search(r"(?:^|[-_.])ota(?:[-_.]|$)", name))


def find_latest_full_zip(product_dir):
    zips = glob.glob(os.path.join(product_dir, "*.zip"))
    candidates = [z for z in zips if not is_non_full_zip(z)]
    if not candidates:
        return None
    # Prefer non-OTA-named zips (e.g. Lineage/Lunaris), but allow <rom>_<device>-ota.zip (e.g. AOSPA)
    non_ota = [z for z in candidates if not _has_ota_token(z)]
    return max(non_ota or candidates, key=os.path.getmtime)


def _walk_up_for_root(start_dir, max_levels=8):
    cur = os.path.abspath(start_dir)
    for _ in range(max_levels):
        if (
            os.path.isdir(os.path.join(cur, ".repo"))
            or os.path.isdir(os.path.join(cur, "build", "make"))
            or os.path.isfile(os.path.join(cur, "build", "envsetup.sh"))
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def scan_server_for_rom_build(codename=DEVICE_CODENAME):
    """Scan server directories to locate the ROM source root & fogos out dir."""
    if PRODUCT_DIR_OVERRIDE and os.path.isdir(PRODUCT_DIR_OVERRIDE):
        pdir = os.path.abspath(PRODUCT_DIR_OVERRIDE)
        sroot = _walk_up_for_root(pdir) or os.path.abspath(os.path.join(pdir, "../../../.."))
        return sroot, pdir

    candidates = []  # (zip_mtime, source_root, product_dir)
    seen_pdirs = set()

    def check_root(root_dir):
        pdir = os.path.join(root_dir, "out", "target", "product", codename)
        pdir_abs = os.path.abspath(pdir)
        if pdir_abs in seen_pdirs or not os.path.isdir(pdir_abs):
            return
        seen_pdirs.add(pdir_abs)
        z = find_latest_full_zip(pdir_abs)
        if z:
            candidates.append((os.path.getmtime(z), os.path.abspath(root_dir), pdir_abs))

    # 1. Check upward from cwd and script directory first
    for base in (os.getcwd(), SCRIPT_DIR):
        root = _walk_up_for_root(base)
        if root:
            check_root(root)
        # Also check direct relative ../out/target/product/fogos
        rel_pdir = os.path.abspath(os.path.join(base, "..", "out", "target", "product", codename))
        if os.path.isdir(rel_pdir) and rel_pdir not in seen_pdirs:
            z = find_latest_full_zip(rel_pdir)
            if z:
                seen_pdirs.add(rel_pdir)
                candidates.append((os.path.getmtime(z), os.path.abspath(os.path.join(rel_pdir, "../../../..")), rel_pdir))

    # 2. Scan common server build locations (bounded depth, fast)
    skip_dirs = {
        "proc", "sys", "dev", "run", "tmp", "var", "usr", "lib", "lib64",
        "bin", "sbin", "boot", "etc", "snap", "node_modules", ".git",
        "soong", "host", "target", "intermediates",
    }
    search_roots = [
        os.getcwd(),
        os.path.dirname(os.getcwd()),
        os.path.expanduser("~"),
        "/home", "/root", "/mnt", "/opt", "/srv", "/data", "/work", "/builds", "/rom",
    ]
    visited = set()

    def scan_dir(path, depth):
        if depth > 4:
            return
        try:
            real = os.path.realpath(path)
        except OSError:
            return
        if real in visited or not os.path.isdir(real):
            return
        visited.add(real)

        if (
            os.path.isdir(os.path.join(real, "out", "target", "product", codename))
        ):
            check_root(real)
            return

        if depth == 4:
            return
        try:
            with os.scandir(real) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith(".") or entry.name in skip_dirs:
                        continue
                    scan_dir(entry.path, depth + 1)
        except OSError:
            pass

    for sr in search_roots:
        if sr and os.path.isdir(sr):
            scan_dir(sr, 0)

    if not candidates:
        return None, None

    candidates.sort(key=lambda x: x[0], reverse=True)
    _, best_root, best_pdir = candidates[0]
    return best_root, best_pdir


# ---------------------------------------------------------------- metadata detection

BUILD_PROP_PATHS = [
    "system/build.prop",
    "system/system/build.prop",
    "system/etc/build.prop",
    "product/etc/build.prop",
    "vendor/build.prop",
    "product/build.prop",
    "odm/build.prop",
    "system_ext/build.prop",
    "build.prop",
]

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

_ID_KEYS = (
    "ro.build.display.id", "ro.build.flavor", "ro.product.build.flavor",
    "ro.build.id", "ro.modversion", "ro.build.description",
)


def read_build_props(product_dir):
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
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k and k not in props:
                        props[k] = v.strip()
        except OSError:
            pass
    return props


def parse_filename(filename, device_slug=DEVICE_CODENAME):
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

    cut = len(base)
    for pat in (r"[-_]?[vV]?\d+\.\d+", r"[-_][aA]?1[0-9](?=[-_]|$)", r"[-_]?20\d{6}"):
        for mm in re.finditer(pat, base):
            cut = min(cut, mm.start())
    if device_slug:
        idx = base.lower().find(device_slug.lower())
        if idx > 0:
            cut = min(cut, idx)
    name = re.sub(r"[_-]+", " ", base[:cut]).strip()
    name = re.sub(r"\b(?:ota|unofficial|official|vanilla|gapps)\b", "", name, flags=re.I).strip()
    return {"name": name or None, "version": version, "android": android, "date": date}


def detect_rom_token_version(props, filename=None):
    haystack = " ".join([props.get(k, "") for k in _ID_KEYS] + [filename or ""]).lower()
    best = None
    for key, value in props.items():
        m = re.match(r"^ro\.([a-z0-9_]+)\.(?:build\.|display\.)?version$", key)
        if not m:
            continue
        token = m.group(1)
        if token in _TOKEN_EXCLUDE:
            continue
        if not re.search(r"\d", value) and token not in ("aospa", "paranoid"):
            continue
        siblings = sum(1 for k in props if k.startswith(f"ro.{token}."))
        in_haystack = token in haystack
        if not in_haystack and siblings < 2:
            continue
        score = (2 if in_haystack else 0) + (1 if siblings >= 2 else 0) + (1 if re.match(r"^\d", value) else 0)
        if best is None or score > best[0]:
            best = (score, token, value)
    return (best[1], best[2]) if best else (None, None)


def _nice_brand(text):
    if text and text.lower() == "aospa":
        return "Paranoid Android"
    if text and text.islower() and " " not in text:
        return text.title()
    return text


def guess_brand(props, token, device_slug, filename=None):
    skip = set(_BRAND_SKIP)
    if device_slug:
        skip.add(device_slug.lower())
    fname_brand = parse_filename(filename, device_slug)["name"] if filename else None
    id_texts = [props.get(k, "") for k in _ID_KEYS]

    if token:
        for text in id_texts + [filename or ""]:
            for word in re.findall(r"[A-Za-z][A-Za-z0-9\.]*", text):
                wl = word.lower()
                if wl in skip or re.match(r"^v?\d", wl):
                    continue
                if wl.startswith(token):
                    return word if len(wl) > len(token) else _nice_brand(word)
        if fname_brand and fname_brand.lower().replace(" ", "").startswith(token):
            return _nice_brand(fname_brand)
        return _nice_brand(token)

    if fname_brand and fname_brand.lower() not in skip:
        return _nice_brand(fname_brand)
    for text in id_texts:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9\.]*", text):
            wl = word.lower()
            if wl in skip or re.match(r"^v?\d", wl):
                continue
            return _nice_brand(word)
    return None


def manifest_brand(source_root, token):
    if not source_root:
        return None
    paths = []
    mdir = os.path.join(source_root, ".repo", "manifests")
    if os.path.isdir(mdir):
        paths += [os.path.join(mdir, f) for f in os.listdir(mdir) if f.endswith(".xml")]
    mxml = os.path.join(source_root, ".repo", "manifest.xml")
    if os.path.exists(mxml):
        paths.append(mxml)
    cleaned = []
    for path in paths:
        try:
            with open(path, "r", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        for url in re.findall(r'fetch\s*=\s*"([^"]+)"', text):
            seg = url.rstrip("/").split("/")[-1]
            seg = re.sub(r"(?i)(android[_-]?manifest|manifest|android|platform|vendor)", "", seg).strip("-_.")
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


_SDK_TO_ANDROID = {
    "36": "16", "35": "15", "34": "14", "33": "13",
    "32": "12L", "31": "12", "30": "11", "29": "10",
}


def detect_android_version(props, zip_path, source_root, filename):
    if ANDROID_VERSION_OVERRIDE:
        return ANDROID_VERSION_OVERRIDE

    # 1. build.prop files in out/target/product/<device>
    for k in (
        "ro.build.version.release",
        "ro.system.build.version.release",
        "ro.build.version.release_or_codename",
        "ro.product.build.version.release",
        "ro.vendor.build.version.release",
    ):
        v = props.get(k)
        if v:
            m = re.match(r"^\d+", v.strip())
            if m:
                return m.group(0)

    # 2. Inspect metadata / build.prop inside the ROM zip (no extraction)
    if zip_path and os.path.isfile(zip_path):
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = set(zf.namelist())
                for target in (
                    "META-INF/com/android/metadata",
                    "system/build.prop",
                    "system/system/build.prop",
                    "system/etc/build.prop",
                    "product/etc/build.prop",
                    "build.prop",
                ):
                    if target not in names:
                        continue
                    content = zf.read(target).decode("utf-8", errors="ignore")
                    m = re.search(r"ro\.(?:system\.)?build\.version\.release\s*=\s*(\d+)", content)
                    if m:
                        return m.group(1)
                    m = re.search(r"post-build=[^:]+:(\d+)/", content)
                    if m:
                        return m.group(1)
                    m = re.search(r"sdk_version\s*=\s*(\d+)", content)
                    if m and m.group(1) in _SDK_TO_ANDROID:
                        return _SDK_TO_ANDROID[m.group(1)]
        except Exception:
            pass

    # 3. Inspect ROM source tree (version_defaults.mk, RELEASE_PLATFORM_VERSION, manifest)
    if source_root and os.path.isdir(source_root):
        for pat in (
            "build/release/flag_values/*/RELEASE_PLATFORM_VERSION.textproto",
            "build/make/core/version_defaults.mk",
        ):
            for fpath in glob.glob(os.path.join(source_root, pat)):
                try:
                    content = open(fpath, "r", errors="ignore").read()
                except OSError:
                    continue
                m = re.search(r'string_value:\s*"(\d+)"', content)
                if m:
                    return m.group(1)
                m = re.search(r"PLATFORM_VERSION(?:_LAST_STABLE)?\s*[:?]?=\s*(\d+)", content)
                if m:
                    return m.group(1)

        manifest_files = glob.glob(os.path.join(source_root, ".repo", "manifests", "*.xml"))
        mxml = os.path.join(source_root, ".repo", "manifest.xml")
        if os.path.isfile(mxml):
            manifest_files.append(mxml)
        for mf in manifest_files:
            try:
                content = open(mf, "r", errors="ignore").read()
            except OSError:
                continue
            m = re.search(r'revision="(?:refs/heads/)?(?:android-)?(1[0-9])(?:\.\d+)?', content)
            if m:
                return m.group(1)
            m = re.search(r'revision="(?:refs/heads/)?lineage-(2[0-9])', content)
            if m:
                return str(int(m.group(1)) - 7)

    # 4. Filename fallback
    return parse_filename(filename)["android"] or "N/A"


def detect_rom_details(zip_path, props, source_root):
    filename = os.path.basename(zip_path)
    fname = parse_filename(filename, DEVICE_CODENAME)
    token, token_version = detect_rom_token_version(props, filename)

    name = ROM_NAME_OVERRIDE or guess_brand(props, token, DEVICE_CODENAME, filename) \
        or manifest_brand(source_root, token) or fname["name"] or "Custom ROM"

    version = ROM_VERSION_OVERRIDE or token_version
    if not version:
        for k in ("ro.build.version.incremental", "ro.build.display.id", "ro.build.id", "ro.modversion"):
            v = props.get(k)
            if v:
                m = re.search(r"[vV]?(\d+\.\d+(?:\.\d+)?)", v)
                if m:
                    version = m.group(1)
                    break
    version = version or fname["version"] or ""

    android = detect_android_version(props, zip_path, source_root, filename)

    # Build date & time
    build_dt = None
    epoch = props.get("ro.build.date.utc")
    if epoch and epoch.isdigit():
        try:
            build_dt = datetime.fromtimestamp(int(epoch))
        except (ValueError, OSError, OverflowError):
            build_dt = None
    if build_dt is None:
        build_dt = datetime.fromtimestamp(os.path.getmtime(zip_path))

    return {
        "filename": filename,
        "rom_name": name,
        "version": version,
        "type": BUILD_TYPE,
        "android": android,
        "date": build_dt.strftime("%d %b %Y"),
        "time": build_dt.strftime("%H:%M"),
        "size": human_size(os.path.getsize(zip_path)),
        "maintainer": MAINTAINER,
    }


# ---------------------------------------------------------------- streaming uploads

class ProgressReader:
    """File-like wrapper that streams 4 MB chunks and reports byte progress."""

    def __init__(self, path, callback, abort_event):
        self._fh = open(path, "rb")
        self._size = os.path.getsize(path)
        self._sent = 0
        self._cb = callback
        self._abort = abort_event

    def __len__(self):
        return self._size

    def read(self, n=-1):
        if self._abort.is_set():
            raise InterruptedError("Upload aborted")
        if n is None or n < 0:
            n = CHUNK_SIZE
        chunk = self._fh.read(min(n, CHUNK_SIZE))
        if chunk:
            self._sent += len(chunk)
            self._cb(self._sent)
        return chunk

    def close(self):
        self._fh.close()


class MultipartFileStream:
    """Zero-RAM-buffering multipart/form-data stream for fast GoFile uploads."""

    def __init__(self, field_name, file_path, callback, abort_event):
        self.boundary = uuid.uuid4().hex
        filename = os.path.basename(file_path)
        self._head = (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        self._tail = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self._file_size = os.path.getsize(file_path)
        self._total = len(self._head) + self._file_size + len(self._tail)
        self._reader = ProgressReader(file_path, callback, abort_event)
        self._stage = 0

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return self._total

    def __iter__(self):
        yield self._head
        while True:
            chunk = self._reader.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
        yield self._tail

    def close(self):
        self._reader.close()


def make_session():
    sess = requests.Session()
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


def get_gofile_server(session):
    res = session.get("https://api.gofile.io/servers", timeout=15)
    res.raise_for_status()
    data = res.json()
    servers = data.get("data", {}).get("servers") or []
    if data.get("status") != "ok" or not servers:
        raise RuntimeError("Could not retrieve a GoFile upload server")
    return servers[0]["name"]


def upload_to_gofile(session, file_path, server, progress_cb, abort_event):
    stream = MultipartFileStream("file", file_path, progress_cb, abort_event)
    try:
        res = session.post(
            f"https://{server}.gofile.io/contents/uploadfile",
            data=stream,
            headers={
                "Content-Type": stream.content_type,
                "Content-Length": str(len(stream)),
            },
            timeout=(15, 1800),
        )
        res.raise_for_status()
        up_data = res.json()
        if up_data.get("status") == "ok":
            return up_data["data"]["downloadPage"]
        raise RuntimeError(f"GoFile rejected upload: {up_data}")
    finally:
        stream.close()


def upload_to_pixeldrain(session, file_path, progress_cb, abort_event):
    filename = os.path.basename(file_path)
    reader = ProgressReader(file_path, progress_cb, abort_event)
    try:
        kwargs = {
            "data": reader,
            "headers": {"Content-Length": str(len(reader))},
            "timeout": (15, 1800),
        }
        if PIXELDRAIN_API_KEY:
            kwargs["auth"] = ("", PIXELDRAIN_API_KEY)
        res = session.put(f"https://pixeldrain.com/api/file/{filename}", **kwargs)
        res.raise_for_status()
        data = res.json()
        if data.get("id"):
            return f"https://pixeldrain.com/u/{data['id']}"
        raise RuntimeError(f"Pixeldrain rejected upload: {data}")
    finally:
        reader.close()


def run_upload_task(session, tid, file_path, mirror, gofile_server, dash, abort_event):
    for attempt in range(1, MAX_RETRIES + 1):
        if abort_event.is_set():
            raise InterruptedError("Aborted")
        dash.set_status(tid, "active", "uploading", attempt=attempt)
        try:
            cb = lambda sent: dash.update_progress(tid, sent)
            if mirror == "gofile":
                url = upload_to_gofile(session, file_path, gofile_server, cb, abort_event)
            else:
                url = upload_to_pixeldrain(session, file_path, cb, abort_event)
            dash.set_status(tid, "done", "uploaded", attempt=attempt)
            return url
        except InterruptedError:
            raise
        except Exception as exc:  # noqa: BLE001
            if attempt < MAX_RETRIES and not abort_event.is_set():
                dash.set_status(tid, "active", f"retrying ({attempt}/{MAX_RETRIES})", attempt=attempt + 1)
                time.sleep(3 * attempt)
            else:
                dash.set_status(tid, "failed", str(exc)[:36], attempt=attempt)
                raise


# ---------------------------------------------------------------- banner repo & telegram

_BANNER_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_ROM_NOISE_WORDS = {
    "os", "aosp", "rom", "android", "project", "custom",
    "banner", "logo", "img", "image", "wallpaper", "unofficial", "official",
}


_ROM_ALIASES = {
    "aospa": ["paranoid"],
    "paranoid": ["aospa"],
}


def _normalize_rom_tokens(text):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    words = re.findall(r"[a-z0-9]+", spaced.lower())
    core = [w for w in words if w not in _ROM_NOISE_WORDS]
    return words, (core or words)


def _score_banner_candidate(rom_name, file_path):
    stem = os.path.splitext(os.path.basename(file_path))[0]
    rom_words, rom_core = _normalize_rom_tokens(rom_name)
    file_words, file_core = _normalize_rom_tokens(stem)

    if not rom_core or not file_core:
        return 0.0

    rom_full = " ".join(rom_words)
    file_full = " ".join(file_words)
    rom_core_str = "".join(rom_core)
    file_core_str = "".join(file_core)

    if rom_full == file_full:
        return 100.0
    if rom_core == file_core or rom_core_str == file_core_str:
        return 92.0
    if all(t in file_core for t in rom_core) or all(t in rom_core for t in file_core):
        return 80.0
    if rom_core_str in file_core_str or file_core_str in rom_core_str:
        return 68.0
    for tok in rom_core:
        for alias in _ROM_ALIASES.get(tok, ()):
            if alias in file_core or alias in file_core_str:
                return 85.0
    return difflib.SequenceMatcher(None, rom_core_str, file_core_str).ratio() * 60.0


def fetch_rom_banner(rom_name, session):
    """Fetch ONLY the matching ROM banner image from BANNER_REPO (not the whole repo)."""
    repo = BANNER_REPO.strip().removesuffix(".git")
    if repo.startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
    repo = repo.strip("/")

    tree_data = None
    branch = "main"
    for ref in ("HEAD", "main", "master"):
        try:
            res = session.get(
                f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1",
                headers={"Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if res.status_code == 200:
                tree_data = res.json()
                branch = "main" if ref == "HEAD" else ref
                break
        except Exception:
            continue

    if not tree_data:
        return None, None

    images = [
        item["path"]
        for item in tree_data.get("tree", [])
        if item.get("type") == "blob"
        and item.get("path", "").lower().endswith(_BANNER_IMAGE_EXTS)
    ]
    if not images:
        return None, None

    scored = sorted(
        ((_score_banner_candidate(rom_name, p), p) for p in images),
        key=lambda x: x[0],
        reverse=True,
    )
    best_score, best_path = scored[0]
    if best_score < 55.0:
        return None, None

    encoded_path = "/".join(urllib.parse.quote(seg) for seg in best_path.split("/"))
    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{encoded_path}"
    ext = os.path.splitext(best_path)[1].lower() or ".png"
    safe_slug = re.sub(r"[^a-z0-9]+", "_", rom_name.lower()).strip("_") or "rom"
    out_path = os.path.join(tempfile.gettempdir(), f"rom_banner_{safe_slug}{ext}")

    res = session.get(raw_url, timeout=30)
    if res.status_code != 200 and branch == "main":
        raw_url = f"https://raw.githubusercontent.com/{repo}/master/{encoded_path}"
        res = session.get(raw_url, timeout=30)
    res.raise_for_status()

    with open(out_path, "wb") as fh:
        fh.write(res.content)
    return out_path, best_path


def build_telegram_payload(details, links):
    ver_str = f" {details['version']}" if details["version"] else ""

    text = (
        f"<b>{html.escape(details['rom_name'])}{html.escape(ver_str)}</b> "
        f"• <code>{DEVICE_CODENAME}</code>\n"
        f"<b>Android:</b> {html.escape(details['android'])}  |  "
        f"<b>{html.escape(details['type'])}</b>\n"
        f"<b>Date:</b> {html.escape(details['date'])}  |  "
        f"<b>Time:</b> {html.escape(details['time'])}\n"
        f"<b>By:</b> {html.escape(details['maintainer'])}\n\n"
        f"⚠️ <i>Flash at your own risk. Always back up your data before flashing.</i>"
    )

    button_rows = []
    targets = [
        ("ROM", details["filename"]),
        ("Boot", "boot.img"),
        ("DTBO", "dtbo.img"),
    ]
    for label, fname in targets:
        entry = links.get(fname, {})
        row = []
        if entry.get("pixeldrain"):
            row.append({"text": f"{label} • Pixeldrain", "url": entry["pixeldrain"]})
        if entry.get("gofile"):
            row.append({"text": f"{label} • GoFile", "url": entry["gofile"]})
        if row:
            button_rows.append(row)

    return {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": button_rows},
    }


def send_telegram_post(details, links, banner_path=None):
    payload = build_telegram_payload(details, links)
    if banner_path and os.path.isfile(banner_path):
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        with open(banner_path, "rb") as fh:
            res = requests.post(
                url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": payload["text"],
                    "parse_mode": "HTML",
                    "reply_markup": json.dumps(payload["reply_markup"]),
                },
                files={"photo": fh},
                timeout=60,
            )
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        res = requests.post(url, json=payload, timeout=30)

    if res.status_code != 200:
        raise RuntimeError(f"Telegram API error ({res.status_code}): {res.text}")
    print(f"  {paint('✓', C.GREEN, C.BOLD)} Telegram release post sent.")


# ---------------------------------------------------------------- main

def main():
    dry_run = "--dry-run" in sys.argv
    debug = "--debug" in sys.argv

    if sys.stdout.isatty():
        sys.stdout.write(C.CLEAR + C.HOME)
        sys.stdout.flush()

    print(paint(ROM_UPLOAD_BANNER.rstrip("\n"), C.CYAN, C.BOLD))
    print()

    # 1. Scan server for ROM source root & fogos build output
    scan_box = {}

    def do_scan():
        sroot, pdir = scan_server_for_rom_build(DEVICE_CODENAME)
        if not pdir:
            raise FileNotFoundError(f"Could not locate out/target/product/{DEVICE_CODENAME} with a full ROM zip.")
        zpath = find_latest_full_zip(pdir)
        if not zpath:
            raise FileNotFoundError(f"No full ROM .zip found in {pdir}")
        props = read_build_props(pdir)
        details = detect_rom_details(zpath, props, sroot)
        scan_box["source_root"] = sroot
        scan_box["product_dir"] = pdir
        scan_box["zip_path"] = zpath
        scan_box["props"] = props
        scan_box["details"] = details

    try:
        show_loading_screen("Scanning", [("Scanning server for fogos build", do_scan)])
    except Exception as exc:
        print(f"\n  {paint('✗ ' + str(exc), C.RED, C.BOLD)}\n")
        sys.exit(1)

    details = scan_box["details"]
    product_dir = scan_box["product_dir"]
    zip_path = scan_box["zip_path"]

    if debug:
        print(f"  {paint('debug:', C.DIM)} root={scan_box['source_root']}  out={product_dir}")

    # 2. Show detected ROM
    ver_disp = f" {details['version']}" if details["version"] else ""
    print()
    print(
        f"  📦 {paint(details['rom_name'] + ver_disp, C.BOLD, C.WHITE)}  "
        f"{paint('•', C.DIM)}  Android {details['android']}  "
        f"{paint('•', C.DIM)}  {details['type']}  "
        f"{paint('•', C.DIM)}  by {details['maintainer']}"
    )
    print(
        f"  📱 {DEVICE_NAME}  {paint('•', C.DIM)}  "
        f"{details['date']} {details['time']}  {paint('•', C.DIM)}  {details['size']}"
    )
    print()

    # 3. Collect required files: rom.zip, boot.img, dtbo.img
    file_entries = [("ROM", zip_path)]
    for img_label, img_name in (("Boot", "boot.img"), ("DTBO", "dtbo.img")):
        img_path = os.path.join(product_dir, img_name)
        if os.path.isfile(img_path):
            file_entries.append((img_label, img_path))
        else:
            print(f"  {paint('⚠', C.YELLOW)} {img_name} not found in {product_dir}, skipping.")

    session = make_session()
    server_box = {"gofile_server": "store1"}

    banner_box = {"path": None, "repo_file": None}

    def prep_servers():
        if not dry_run:
            server_box["gofile_server"] = get_gofile_server(session)
        else:
            time.sleep(0.5)

    def prep_banner():
        try:
            bpath, rfile = fetch_rom_banner(details["rom_name"], session)
            banner_box["path"] = bpath
            banner_box["repo_file"] = rfile
        except Exception:
            banner_box["path"] = None

    try:
        show_loading_screen(
            details["rom_name"],
            [
                (f"Fetching {details['rom_name']} banner from GitHub", prep_banner),
                ("Connecting to Pixeldrain & GoFile servers", prep_servers),
            ],
        )
    except Exception:
        print("\n  " + paint("Something's wrong, try again later.", C.RED, C.BOLD) + "\n")
        sys.exit(1)

    # 4. Build upload task list & start dashboard
    tasks = []
    task_map = {}
    for label, fpath in file_entries:
        fsize = os.path.getsize(fpath)
        fname = os.path.basename(fpath)
        for mirror, mtitle in (("pixeldrain", "Pixeldrain"), ("gofile", "GoFile")):
            tid = f"{label.lower()}_{mirror}"
            tasks.append((tid, f"{label} ({human_size(fsize)})", mtitle, fsize))
            task_map[tid] = (fname, fpath, mirror)

    header = [""]
    if sys.stdout.isatty():
        header += [paint(ROM_UPLOAD_BANNER.rstrip("\n"), C.CYAN, C.BOLD), ""]
    header += [
        f"  📦 {paint(details['rom_name'] + ver_disp, C.BOLD, C.WHITE)}  "
        f"•  Android {details['android']}  •  {details['type']}  •  by {details['maintainer']}",
        f"  📱 {DEVICE_NAME}  •  {details['date']} {details['time']}",
        "",
        f"  {paint('Uploading release files…', C.BOLD)}",
        "",
    ]

    dash = UploadDashboard(header, tasks)
    dash.start()

    links = {os.path.basename(p): {} for _, p in file_entries}
    abort_event = threading.Event()
    failed_err = None

    if dry_run:
        def dry_worker(tid):
            fname, fpath, mirror = task_map[tid]
            total = os.path.getsize(fpath)
            dash.set_status(tid, "active", "uploading", attempt=1)
            for step in range(1, 11):
                time.sleep(0.12)
                dash.update_progress(tid, int(total * step / 10))
            dash.set_status(tid, "done", "uploaded", attempt=1)
            return fname, mirror, f"https://{'pixeldrain.com/u/DRYRUN' if mirror == 'pixeldrain' else 'gofile.io/d/DRYRUN'}"

        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            futs = [pool.submit(dry_worker, tid) for tid, _, _, _ in tasks]
            for fut in as_completed(futs):
                fname, mirror, url = fut.result()
                links[fname][mirror] = url
    else:
        with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
            futs = {
                pool.submit(
                    run_upload_task,
                    session, tid, task_map[tid][1], task_map[tid][2],
                    server_box["gofile_server"], dash, abort_event,
                ): tid
                for tid, _, _, _ in tasks
            }
            for fut in as_completed(futs):
                tid = futs[fut]
                fname, _, mirror = task_map[tid]
                try:
                    links[fname][mirror] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    failed_err = exc
                    abort_event.set()
                    break

    dash.stop(failed=failed_err is not None)

    if failed_err is not None:
        print("  " + paint("Something's wrong, try again later.", C.RED, C.BOLD))
        print(f"  {paint('Details: ' + str(failed_err), C.DIM)}\n")
        sys.exit(1)

    # 5. Print links in terminal & save to release_links.txt
    print(f"  {paint('Download Links:', C.BOLD)}")
    with open("release_links.txt", "w", encoding="utf-8") as fh:
        for label, fpath in file_entries:
            fname = os.path.basename(fpath)
            entry = links[fname]
            print(f"  • {paint(label, C.BOLD)} ({fname})")
            print(f"      Pixeldrain : {paint(entry['pixeldrain'], C.CYAN)}")
            print(f"      GoFile     : {paint(entry['gofile'], C.CYAN)}")
            fh.write(f"{fname}:\n  Pixeldrain: {entry['pixeldrain']}\n  GoFile: {entry['gofile']}\n")
    print()

    # 6. Send to Telegram
    if banner_box["repo_file"]:
        print(f"  {paint('Banner:', C.DIM)} {banner_box['repo_file']}")
    else:
        print(f"  {paint('Banner:', C.DIM)} none matched in {BANNER_REPO} (sending text post)")

    if dry_run:
        payload = build_telegram_payload(details, links)
        print(f"  {paint('Telegram Preview (--dry-run):', C.BOLD)}")
        print(payload["text"])
        print(f"\n  {paint('Buttons:', C.DIM)} {payload['reply_markup']['inline_keyboard']}\n")
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            try:
                send_telegram_post(details, links, banner_path=banner_box["path"])
            except Exception as exc:  # noqa: BLE001
                print("  " + paint("Something's wrong, try again later.", C.RED, C.BOLD))
                print(f"  {paint('Telegram error: ' + str(exc), C.DIM)}\n")
                sys.exit(1)
        return

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"  {paint('⚠ TELEGRAM_TOKEN or TELEGRAM_CHAT_ID is not set — skipping Telegram post.', C.YELLOW)}\n")
        return

    try:
        send_telegram_post(details, links, banner_path=banner_box["path"])
    except Exception as exc:  # noqa: BLE001
        print("  " + paint("Something's wrong, try again later.", C.RED, C.BOLD))
        print(f"  {paint('Telegram error: ' + str(exc), C.DIM)}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()