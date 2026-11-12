import streamlit as st
import threading
import time
import json
import os
import hashlib
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# Timezone for accurate timestamps (IST)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

CONFIG_FILE = "config.json"
SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

PAGE_WAIT_SECONDS = 5 * 60  # Fixed 5 minutes (300 seconds) per link

DEFAULT_CONFIG = {
    "links": ["https://autoupdater-thiyagu.streamlit.app/"],
    "interval_minutes": 120,  # Default: 2 hours (120 minutes)
}


def now_str():
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %I:%M:%S %p")


# --- Ensure Chromium is Installed ---
@st.cache_resource
def ensure_playwright_browser():
    try:
        subprocess.run(["playwright", "install", "chromium"], check=True)
        return True
    except Exception as e:
        return str(e)


# --- Helper: Unique File Path per Link ---
def get_screenshot_path(url):
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
    return os.path.join(SCREENSHOT_DIR, f"snap_{url_hash}.png")


def delete_screenshot(url):
    path = get_screenshot_path(url)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


# --- Persistent Config Storage ---
def load_config():
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            if not data.get("links"):
                data["links"] = DEFAULT_CONFIG["links"]
            if "interval_minutes" not in data:
                data["interval_minutes"] = DEFAULT_CONFIG["interval_minutes"]
            return data
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# --- Calculate Minimum Allowed Cycle Minutes ---
def get_min_cycle_minutes(num_links):
    return (max(1, num_links) * 5) + 1


# --- Browser Open, Wait 5 Min, Scroll & Screenshot Logic ---
def open_scroll_and_screenshot(url, worker_state):
    start_ts = now_str()
    worker_state["logs"].insert(0, f"[{start_ts}] Opening: {url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.goto(url, timeout=60000, wait_until="domcontentloaded")

                worker_state["logs"].insert(
                    0, f"[{now_str()}] Opened! Waiting 5 minutes on: {url}"
                )

                # Wait 5 minutes (checks Stop button every second)
                for remaining_wait in range(PAGE_WAIT_SECONDS, 0, -1):
                    if not worker_state["running"]:
                        return
                    mins_left, secs_left = divmod(remaining_wait, 60)
                    worker_state["next_run"] = f"Waiting on page ({mins_left}m {secs_left:02d}s left)..."
                    time.sleep(1)

                # Smoothly scroll down the page
                worker_state["next_run"] = "Scrolling & taking screenshot..."
                total_height = page.evaluate("document.body.scrollHeight")
                for pos in range(0, total_height, 400):
                    if not worker_state["running"]:
                        return
                    page.evaluate(f"window.scrollTo(0, {pos})")
                    time.sleep(0.5)

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(2)

                # Delete old screenshot & save new one
                shot_path = get_screenshot_path(url)
                delete_screenshot(url)
                page.screenshot(path=shot_path, full_page=True)

                end_ts = now_str()
                worker_state["link_last_run"][url] = end_ts
                worker_state["logs"].insert(0, f"[{end_ts}] Waited 5 min, scrolled & saved screenshot: {url}")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as e:
        err_ts = now_str()
        worker_state["link_last_run"][url] = f"Error at {err_ts}"
        worker_state["logs"].insert(0, f"[{err_ts}] ERROR on {url}: {str(e)}")

    worker_state["logs"] = worker_state["logs"][:40]


# --- Background Worker Thread ---
def background_loop(worker_state):
    while True:
        try:
            if worker_state["running"]:
                cfg = load_config()
                min_allowed = get_min_cycle_minutes(len(cfg["links"]))

                if cfg["interval_minutes"] < min_allowed:
                    worker_state["running"] = False
                    worker_state["next_run"] = "Stopped (Invalid interval)"
                    worker_state["logs"].insert(
                        0,
                        f"[{now_str()}] Stopped: Interval ({cfg['interval_minutes']} min) must be >= {min_allowed} min."
                    )
                    continue

                cycle_start_dt = datetime.now(LOCAL_TZ)
                worker_state["last_run"] = cycle_start_dt.strftime("%Y-%m-%d %I:%M:%S %p")
                worker_state["next_run"] = "Starting browser..."

                for link in cfg["links"]:
                    if not worker_state["running"]:
                        break
                    open_scroll_and_screenshot(link, worker_state)

                if not worker_state["running"]:
                    worker_state["next_run"] = "Stopped"
                    continue

                while worker_state["running"]:
                    target_seconds = load_config()["interval_minutes"] * 60
                    next_dt = cycle_start_dt + timedelta(seconds=target_seconds)
                    remaining = (next_dt - datetime.now(LOCAL_TZ)).total_seconds()

                    if remaining <= 0:
                        break

                    worker_state["next_run"] = next_dt.strftime("%Y-%m-%d %I:%M:%S %p")
                    time.sleep(1)
            else:
                worker_state["next_run"] = "Stopped"
                time.sleep(1)
        except Exception as thread_err:
            worker_state["logs"].insert(0, f"[{now_str()}] Worker thread error: {thread_err}")
            time.sleep(2)


# --- Singleton State Across Sessions ---
@st.cache_resource
def get_worker_state():
    state = {
        "running": False,
        "last_run": "Not yet run",
        "next_run": "Stopped",
        "link_last_run": {},
        "logs": []
    }
    thread = threading.Thread(target=background_loop, args=(state,), daemon=True)
    thread.start()
    return state


# --- Streamlit Frontend UI ---
st.set_page_config(page_title="Auto Link Opener & Scroller", layout="centered")
browser_status = ensure_playwright_browser()
worker = get_worker_state()
config = load_config()
links = config["links"]
min_required_mins = get_min_cycle_minutes(len(links))

st.title("🔄 Auto Link Opener, Scroller & Screenshot")

if browser_status is not True:
    st.warning(f"Browser setup notice: {browser_status}")


# 1. Live Status, Timestamps & Start/Stop Controls
@st.fragment(run_every=3)
def render_status_panel():
    st.subheader("1. Runner Status & Schedule")
    col_status, col_btn = st.columns([2.5, 1])

    current_cfg = load_config()
    req_mins = get_min_cycle_minutes(len(current_cfg["links"]))
    is_valid_time = current_cfg["interval_minutes"] >= req_mins

    with col_status:
        if worker["running"]:
            st.success(
                f"🟢 **STATUS: ACTIVE** (Cycle: every **{current_cfg['interval_minutes']} min** | **5 min** wait/link)"
            )
        else:
            st.error("🔴 **STATUS: INACTIVE / STOPPED**")

        st.markdown(
            f"- **Last Cycle Started:** `{worker['last_run']}`\n"
            f"- **Current / Next Action:** `{worker['next_run']}`"
        )

    with col_btn:
        if not worker["running"]:
            if st.button("▶️ Activate", type="primary", width="stretch"):
                if not is_valid_time:
                    st.error(
                        f"Cannot start! {len(current_cfg['links'])} link(s) take {len(current_cfg['links']) * 5} min. "
                        f"Set cycle time to at least **{req_mins} min** first."
                    )
                else:
                    worker["running"] = True
                    worker["next_run"] = "Initializing..."
                    worker["logs"].insert(0, f"[{now_str()}] Activated by user.")
                    st.rerun()
        else:
            if st.button("⏹️ Stop", type="secondary", width="stretch"):
                worker["running"] = False
                worker["next_run"] = "Stopped"
                worker["logs"].insert(0, f"[{now_str()}] Stopped by user.")
                st.rerun()


render_status_panel()

st.divider()

# 2. Change Schedule Interval
st.subheader("2. Change Run Interval")
st.info(
    f"⏱️ **{len(links)} link(s) × 5 min = {len(links) * 5} min process time.** "
    f"Minimum allowed cycle time is **{min_required_mins} minutes** (so at least 1 min idle time remains)."
)

with st.form("interval_form"):
    c_val, c_unit, c_save = st.columns([2, 2, 1.5])
    current_mins = config["interval_minutes"]

    default_unit = "Hours" if current_mins % 60 == 0 else "Minutes"
    default_val = (current_mins // 60) if default_unit == "Hours" else current_mins

    with c_val:
        time_value = st.number_input("Run cycle every", min_value=1, value=int(default_val), step=1)
    with c_unit:
        time_unit = st.selectbox("Unit", ["Hours", "Minutes"], index=0 if default_unit == "Hours" else 1)
    with c_save:
        st.write("")
        st.write("")
        save_time = st.form_submit_button("💾 Update Time", width="stretch")

    if save_time:
        new_minutes = int(time_value * 60 if time_unit == "Hours" else time_value)
        if new_minutes < min_required_mins:
            st.error(
                f"❌ **Not Allowed!** `{new_minutes} min` is less than or equal to the `{len(links) * 5} min` process time. "
                f"Please enter at least **{min_required_mins} minutes** (e.g., `6` minutes for 1 link)."
            )
        else:
            config["interval_minutes"] = new_minutes
            save_config(config)
            worker["logs"].insert(0, f"[{now_str()}] Cycle interval updated to every {new_minutes} min.")
            st.success(f"✅ Schedule updated: runs every **{new_minutes} min** ({len(links) * 5} min process + {new_minutes - len(links) * 5} min rest).")
            st.rerun()

st.divider()

# 3. Add New Link
st.subheader("3. Add Links")
with st.form("add_link_form", clear_on_submit=True):
    new_url = st.text_input("Enter URL (include https://)", placeholder="https://example.com")
    submitted = st.form_submit_button("➕ Add Link")
    if submitted:
        clean_url = new_url.strip()
        if clean_url.startswith(("http://", "https://")):
            if clean_url not in links:
                links.append(clean_url)
                config["links"] = links
                new_min_req = get_min_cycle_minutes(len(links))
                if config["interval_minutes"] < new_min_req:
                    config["interval_minutes"] = new_min_req
                save_config(config)
                st.success(f"Added: {clean_url}")
                st.rerun()
            else:
                st.warning("This link is already in your list.")
        else:
            st.error("Please enter a valid URL starting with http:// or https://")

st.divider()


# 4. Active Link Queue, Latest Screenshots & Remove Buttons
@st.fragment(run_every=3)
def render_links_and_logs():
    st.subheader(f"4. Active Links Queue & Screenshots ({len(links)})")
    for idx, link in enumerate(links):
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            last_processed = worker["link_last_run"].get(link, "Pending")
            with c1:
                st.code(link, language=None)
                st.caption(f"Last processed & captured: **{last_processed}**")
            with c2:
                if st.button("🗑️ Remove", key=f"del_{idx}", width="stretch"):
                    removed_link = links.pop(idx)
                    delete_screenshot(removed_link)
                    config["links"] = links
                    save_config(config)
                    st.rerun()

            shot_path = get_screenshot_path(link)
            if os.path.exists(shot_path):
                with st.expander("📸 View Latest Screenshot", expanded=False):
                    st.image(shot_path, caption=f"Captured at {last_processed}", width="stretch")
                    with open(shot_path, "rb") as img_file:
                        st.download_button(
                            label="⬇️ Download Screenshot",
                            data=img_file,
                            file_name=f"screenshot_{idx + 1}.png",
                            mime="image/png",
                            key=f"dl_{idx}"
                        )
            else:
                st.caption("No screenshot captured yet.")

    st.divider()

    # 5. Live Execution Logs
    st.subheader("5. Execution Logs")
    if worker["logs"]:
        st.text_area("Recent Activity", value="\n".join(worker["logs"]), height=220, disabled=True)
    else:
        st.info("No activity yet. Click **Activate** to start.")


render_links_and_logs()