import os
import sys
import json
import time
import random
import string
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ----------  ANSI COLOURS  ----------
GREEN = '\033[92m'
YELLOW = '\033[93m'
RESET = '\033[0m'

# ----------  GLOBAL FLAGS / BUFFERS  ----------
success_found = threading.Event()
used_buffer, invalid_buffer, success_buffer = [], [], []
buffer_lock = threading.Lock()
stats = {"processed": 0, "invalid": 0}
start_sizes = {"used": 0, "invalid": 0, "success": 0}

# ----------  CONFIG & AUTH  ----------
def load_config() -> dict:
    cfg = {}
    for fname in ("config.txt", "params.txt"):
        if not os.path.exists(fname):
            continue
        with open(fname, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = [x.strip() for x in line.split("=", 1)]
                try:
                    v = float(v) if "." in v else int(v)
                except ValueError:
                    pass
                cfg[k] = v
    return cfg


def load_auth() -> str:
    if not os.path.exists("auth.txt"):
        print("ERROR: Missing auth.txt")
        sys.exit(1)
    token = open("auth.txt", "r", encoding="utf-8").read().strip()
    if token.startswith("Bearer "):
        token = token[7:]
    return token


# ----------  UTILITIES  ----------
def generate_code() -> str:
    chars = ['0', random.choice(string.ascii_uppercase)]
    for i in range(4):
        chars.append(random.choice(string.digits if i % 2 == 0 else string.ascii_uppercase))
    return ''.join(chars)


def color_print(msg: str, colour=RESET):
    print(f"{colour}{msg}{RESET}")


def file_linecount(path: str) -> int:
    """Count existing lines in a file, safely."""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return sum(1 for _ in f)


def flush_buffers():
    """Write buffered codes to disk."""
    with buffer_lock:
        if used_buffer:
            with open("used_codes.txt", "a", encoding="utf-8") as f:
                f.write("\n".join(used_buffer) + "\n")
            used_buffer.clear()
        if invalid_buffer:
            with open("invalid_codes.txt", "a", encoding="utf-8") as f:
                f.write("\n".join(invalid_buffer) + "\n")
            invalid_buffer.clear()
        if success_buffer:
            with open("success.txt", "a", encoding="utf-8") as f:
                f.write("\n".join(success_buffer) + "\n")
            success_buffer.clear()


# ----------  NETWORKING  ----------
def make_session() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=2, backoff_factor=0.1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10))
    return s


def submit_code(session: requests.Session, code: str, auth: str, config: dict) -> bool:
    if success_found.is_set():
        return False

    url = "https://sora.chatgpt.com/backend/project_y/invite/accept"
    headers = {
        "Authorization": f"Bearer {auth}",
        "User-Agent": config.get("User-Agent", ""),
        "OAI-Device-Id": config.get("OAI-Device-Id", ""),
        "Content-Type": "application/json",
        "Referer": "https://sora.chatgpt.com/explore",
    }

    try:
        r = session.post(url, json={"invite_code": code}, timeout=10)
        sc = r.status_code
        with buffer_lock:
            used_buffer.append(code)
            stats["processed"] += 1

        if sc == 200:
            with buffer_lock:
                success_buffer.append(code)
            color_print(f"\n🎉 SUCCESS! Valid invite code found: {code}\n", GREEN)
            success_found.set()
            return True

        elif sc == 403:
            with buffer_lock:
                invalid_buffer.append(code)
                stats["invalid"] += 1

        elif sc == 401:
            print("[AUTH ERROR] Invalid token – stopping.")
            success_found.set()

        return False

    except requests.RequestException:
        return False


# ----------  THREAD WORKER  ----------
def worker(auth: str, config: dict):
    session = make_session()
    delay = float(config.get("delay", 1.0))
    while not success_found.is_set():
        code = generate_code()
        ok = submit_code(session, code, auth, config)
        if ok:
            break
        if delay:
            time.sleep(delay * random.uniform(0.5, 1.5))


# ----------  BACKGROUND TASKS  ----------
def flusher_thread():
    """Periodically flush buffers every 5 s."""
    while not success_found.is_set():
        time.sleep(5)
        flush_buffers()
    flush_buffers()


def progress_thread():
    """Print a progress line every minute."""
    last = 0
    while not success_found.is_set():
        time.sleep(5)
        if time.time() - last >= 60:
            with buffer_lock:
                p, i = stats["processed"], stats["invalid"]
            color_print(f"⏱ Still running... {p} codes processed, {i} invalid so far.", YELLOW)
            last = time.time()


# ----------  MAIN  ----------
def main():
    config = load_config()
    auth = load_auth()

    threads = int(config.get("max_workers", 8))
    print(f"🚀 Starting fast invite-code scanner | Threads={threads}")

    # Capture file sizes before run
    start_sizes["used"] = file_linecount("used_codes.txt")
    start_sizes["invalid"] = file_linecount("invalid_codes.txt")
    start_sizes["success"] = file_linecount("success.txt")

    start = time.time()
    threading.Thread(target=flusher_thread, daemon=True).start()
    threading.Thread(target=progress_thread, daemon=True).start()

    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, auth, config) for _ in range(threads)]
        try:
            for f in as_completed(futures):
                if success_found.is_set():
                    break
        except KeyboardInterrupt:
            print("\nStopping on user interrupt...")
            success_found.set()

    # Ensure all buffers are saved
    flush_buffers()
    dur = time.time() - start

    # --- TRUE SUCCESS CHECK ---
    success_lines = file_linecount("success.txt")
    has_real_success = success_lines > start_sizes["success"]

    # Calculate new lines written
    used_added = file_linecount("used_codes.txt") - start_sizes["used"]
    invalid_added = file_linecount("invalid_codes.txt") - start_sizes["invalid"]

    print("\n====== Program Stopped ======")
    if has_real_success:
        color_print("🎉 SUCCESS – Valid invite code found and saved to success.txt", GREEN)
    else:
        print("No valid codes found this session.")
    print(f"Runtime: {dur:.1f}s | Processed: {stats['processed']} | Invalid: {stats['invalid']}")
    print(f"Added this run → used_codes.txt: +{used_added}, invalid_codes.txt: +{invalid_added}")
    if has_real_success:
        print(f"Total success codes stored: {success_lines}")


if __name__ == "__main__":
    main()