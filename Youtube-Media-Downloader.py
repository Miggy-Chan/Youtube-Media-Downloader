import os
import sys
import json
import subprocess
import threading
import datetime
import concurrent.futures
from collections import OrderedDict
from tkinter import filedialog
from io import BytesIO

# --- DEPENDENCY AUTO-INSTALLER ---
def install_dependencies():
    packages = {
        "yt_dlp": "yt-dlp",
        "customtkinter": "customtkinter",
        "imageio_ffmpeg": "imageio-ffmpeg",
        "PIL": "Pillow",
        "requests": "requests"
    }
    missing = []
    for module_name, pip_name in packages.items():
        try:
            if module_name == "PIL":
                from PIL import Image
            else:
                __import__(module_name)
        except ImportError:
            missing.append(pip_name)
            
    if missing:
        print(f"[*] Missing dependencies detected. Installing: {', '.join(missing)}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
            os.execv(sys.executable, ['python'] + sys.argv)
        except Exception as e:
            print(f"[-] Auto-install failed: {e}")
            sys.exit(1)

install_dependencies()

import yt_dlp
import customtkinter as ctk
import imageio_ffmpeg
import requests
from PIL import Image

# --- YOUTUBE THEME CONSTANTS ---
YT_BG_MAIN = "#0F0F0F"
YT_BG_SIDEBAR = "#0F0F0F"
YT_BG_TOPBAR = "#0F0F0F"
YT_SEARCH_BG = "#121212"
YT_SEARCH_BORDER = "#303030"
YT_HOVER = "#272727"

YT_TEXT_PRIMARY = "#F1F1F1"
YT_TEXT_SECONDARY = "#AAAAAA"
YT_ACCENT_RED = "#FF0000"
YT_ACCENT_BLUE = "#3EA6FF"

CONFIG_FILE = "config.json"
DOWNLOAD_HISTORY_FILE = "download_history.json"

# Statuses a download can be in while it's still running / queued, vs. finished.
ACTIVE_STATUSES = {"Queued", "Downloading", "Merging"}
FINISHED_STATUSES = {"Completed", "Failed", "Cancelled"}
STATUS_COLORS = {"Completed": "#3EA6FF", "Failed": "#FF0000", "Cancelled": "#AAAAAA"}

class YouTubeCloneApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("YouTube Media Pro")
        self.geometry("1200x800")
        self.minsize(900, 600)
        
        self.ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        self.config = self.load_config()

        # --- PERFORMANCE PRIMITIVES ---
        # One reusable HTTP connection pool instead of a fresh connection per thumbnail.
        self.http_session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=1)
        self.http_session.mount("https://", adapter)
        self.http_session.mount("http://", adapter)

        # A small bounded worker pool for thumbnails (reused across searches) instead of
        # spawning a raw OS thread per image, which is wasteful and unbounded.
        self.thumb_executor = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="thumb")

        # Decoded-image cache (video_id -> CTkImage) so re-seeing a video costs nothing.
        self.thumb_cache = OrderedDict()
        self.thumb_cache_lock = threading.Lock()
        self.THUMB_CACHE_MAX = 200

        # Raw search-results cache (query -> entries) so repeat searches are instant.
        self.search_cache = OrderedDict()
        self.SEARCH_CACHE_MAX = 40

        # Bumped on every new search; lets in-flight thumbnail jobs from an older,
        # already-abandoned search detect they're stale and skip wasted work.
        self.search_generation = 0

        # --- DOWNLOAD MANAGER ---
        # Single source of truth for every download (active or finished), keyed by an
        # incrementing id. Drives both the inline per-card progress UI and the Downloads page.
        self.downloads_lock = threading.Lock()
        self.ui_trackers = {}          # download_id -> inline progress widgets (search/direct card)
        self.active_card_widgets = {}  # download_id -> Downloads-page widgets (only while that page is open)
        self.current_view_name = "search"
        self.load_download_history()   # populates self.downloads + self.download_id_counter

        # Bounded worker pool so multiple downloads can run concurrently without spawning
        # an unbounded number of raw threads. Size is user-configurable in Settings.
        self.download_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.config.get("max_concurrent_downloads", 3), thread_name_prefix="dl"
        )

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Apply Customizations
        ctk.set_appearance_mode(self.config.get("theme", "Dark"))
        ctk.set_widget_scaling(self.config.get("ui_scale", 1.0))
        self.corner_rad = self.config.get("corner_radius", 8)
        
        self.configure(fg_color=YT_BG_MAIN)
        self.setup_layout()
        self.switch_view("search")

    def on_close(self):
        try:
            self.thumb_executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self.thumb_executor.shutdown(wait=False)
        try:
            with self.downloads_lock:
                for rec in self.downloads.values():
                    if rec["status"] in ACTIVE_STATUSES:
                        rec["cancel_event"].set()
            try:
                self.download_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.download_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self.http_session.close()
        except Exception:
            pass
        self.destroy()

    def load_config(self):
        default_config = {
            "download_path": os.path.expanduser(os.path.join("~", "Downloads")),
            "theme": "Dark",
            "ui_scale": 1.0,
            "corner_radius": 12,
            "search_results_count": 20,
            "max_concurrent_downloads": 3
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)
                    default_config.update(data)
            except: pass
        return default_config

    def save_config(self):
        with open(CONFIG_FILE, "w") as f:
            json.dump(self.config, f, indent=4)

    def setup_layout(self):
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(1, weight=1)

        # --- YOUTUBE TOP NAVIGATION BAR ---
        self.top_bar = ctk.CTkFrame(self, height=60, corner_radius=0, fg_color=YT_BG_TOPBAR)
        self.top_bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.top_bar.pack_propagate(False)

        # Logo Area
        self.logo_frame = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        self.logo_frame.pack(side="left", padx=20)
        ctk.CTkLabel(self.logo_frame, text="▶", font=ctk.CTkFont(size=24), text_color=YT_ACCENT_RED).pack(side="left")
        ctk.CTkLabel(self.logo_frame, text=" YouTube", font=ctk.CTkFont(size=20, weight="bold", family="Arial"), text_color=YT_TEXT_PRIMARY).pack(side="left", padx=(5,0))

        # Center Search Area (Pill Shaped)
        self.search_container = ctk.CTkFrame(self.top_bar, fg_color="transparent")
        self.search_container.pack(side="left", expand=True)
        
        self.search_entry = ctk.CTkEntry(
            self.search_container, width=500, height=40, placeholder_text="Search YouTube...",
            fg_color=YT_SEARCH_BG, text_color=YT_TEXT_PRIMARY, border_color=YT_SEARCH_BORDER, 
            corner_radius=20, font=ctk.CTkFont(size=15)
        )
        self.search_entry.pack(side="left")
        self.search_entry.bind("<Return>", lambda e: self.perform_search())

        self.btn_search = ctk.CTkButton(
            self.search_container, text="🔍", width=60, height=40, corner_radius=20,
            fg_color=YT_SEARCH_BORDER, text_color=YT_TEXT_PRIMARY, hover_color="#3D3D3D",
            command=self.perform_search
        )
        self.btn_search.pack(side="left", padx=(5, 0))

        # --- LEFT SIDEBAR ---
        self.sidebar = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=YT_BG_SIDEBAR)
        self.sidebar.grid(row=1, column=0, sticky="ns")
        self.sidebar.pack_propagate(False)

        self.nav_buttons = {}
        self.create_nav_button("🏠  Home (Search)", "search")
        self.create_nav_button("🔗  Direct URL", "direct_url")
        self.create_nav_button("📥  Downloads", "downloads")

        ctk.CTkFrame(self.sidebar, height=1, fg_color=YT_SEARCH_BORDER).pack(fill="x", pady=15, padx=20)
        self.create_nav_button("⚙️  Settings", "settings")

        # --- MAIN CONTENT AREA ---
        self.main_container = ctk.CTkFrame(self, corner_radius=0, fg_color=YT_BG_MAIN)
        self.main_container.grid(row=1, column=1, sticky="nsew")

        self.views = {
            "search": self.create_search_view(),
            "direct_url": self.create_direct_url_view(),
            "downloads": self.create_downloads_view(),
            "settings": self.create_settings_view()
        }

    def create_nav_button(self, text, view_key):
        btn = ctk.CTkButton(
            self.sidebar, text=text, anchor="w", height=45, corner_radius=10,
            fg_color="transparent", text_color=YT_TEXT_PRIMARY, hover_color=YT_HOVER, 
            font=ctk.CTkFont(size=14, weight="bold"),
            command=lambda: self.switch_view(view_key)
        )
        btn.pack(fill="x", padx=10, pady=2)
        self.nav_buttons[view_key] = btn

    def switch_view(self, view_name):
        for key, btn in self.nav_buttons.items():
            btn.configure(fg_color="transparent", text_color=YT_TEXT_PRIMARY)
            
        for view in self.views.values():
            view.pack_forget()

        if view_name in self.nav_buttons:
            self.nav_buttons[view_name].configure(fg_color=YT_HOVER)

        self.current_view_name = view_name
        if view_name == "downloads":
            self.render_downloads_view()

        self.views[view_name].pack(fill="both", expand=True)

    # --- VIEWS ---
    def create_search_view(self):
        frame = ctk.CTkScrollableFrame(self.main_container, fg_color="transparent")
        self.search_results_container = frame
        
        welcome_frame = ctk.CTkFrame(frame, fg_color="transparent")
        welcome_frame.pack(pady=100)
        ctk.CTkLabel(welcome_frame, text="Search for a video to begin.", font=ctk.CTkFont(size=20, weight="bold"), text_color=YT_TEXT_PRIMARY).pack()
        ctk.CTkLabel(welcome_frame, text="Type any keyword, topic, or tag above and press Enter.", font=ctk.CTkFont(size=14), text_color=YT_TEXT_SECONDARY).pack(pady=5)
        return frame

    def create_direct_url_view(self):
        frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        content = ctk.CTkFrame(frame, fg_color="transparent", width=800)
        content.pack(pady=40, padx=40, anchor="n", fill="x")

        ctk.CTkLabel(content, text="Direct Downloader", font=ctk.CTkFont(size=24, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w")
        
        self.direct_url_entry = ctk.CTkEntry(content, height=45, placeholder_text="Paste YouTube URL here...", fg_color=YT_SEARCH_BG, border_color=YT_SEARCH_BORDER, corner_radius=self.corner_rad)
        self.direct_url_entry.pack(fill="x", pady=(20, 15))

        self.btn_analyze = ctk.CTkButton(
            content, text="Analyze Media", height=40, width=150, corner_radius=self.corner_rad,
            fg_color=YT_TEXT_PRIMARY, text_color=YT_BG_MAIN, hover_color="#D1D1D1", font=ctk.CTkFont(weight="bold"), 
            command=self.analyze_direct_url
        )
        self.btn_analyze.pack(anchor="w")

        self.direct_embed_holder = ctk.CTkFrame(content, fg_color="transparent")
        self.direct_embed_holder.pack(fill="x", pady=20)
        return frame

    def create_settings_view(self):
        frame = ctk.CTkScrollableFrame(self.main_container, fg_color="transparent")
        content = ctk.CTkFrame(frame, fg_color="transparent", width=700)
        content.pack(pady=40, padx=60, anchor="nw", fill="both")

        ctk.CTkLabel(content, text="Settings", font=ctk.CTkFont(size=28, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w", pady=(0, 30))

        # Search Results
        ctk.CTkLabel(content, text="Search Results Count", font=ctk.CTkFont(size=16, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w")
        ctk.CTkLabel(content, text="How many videos to fetch per search (more = slower).", font=ctk.CTkFont(size=13), text_color=YT_TEXT_SECONDARY).pack(anchor="w", pady=(0, 10))
        self.results_count_var = ctk.StringVar(value=str(self.config.get("search_results_count", 20)))
        ctk.CTkOptionMenu(content, variable=self.results_count_var, values=["10", "20", "30", "50"], fg_color=YT_SEARCH_BG, button_color=YT_SEARCH_BORDER).pack(anchor="w", pady=(0, 30))

        # Simultaneous Downloads
        ctk.CTkLabel(content, text="Simultaneous Downloads", font=ctk.CTkFont(size=16, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w")
        ctk.CTkLabel(content, text="How many downloads are allowed to run at the same time.", font=ctk.CTkFont(size=13), text_color=YT_TEXT_SECONDARY).pack(anchor="w", pady=(0, 10))
        self.max_concurrent_var = ctk.StringVar(value=str(self.config.get("max_concurrent_downloads", 3)))
        ctk.CTkOptionMenu(content, variable=self.max_concurrent_var, values=["1", "2", "3", "4", "5"], fg_color=YT_SEARCH_BG, button_color=YT_SEARCH_BORDER).pack(anchor="w", pady=(0, 30))

        # Path
        ctk.CTkLabel(content, text="Download Location", font=ctk.CTkFont(size=16, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w")
        path_frame = ctk.CTkFrame(content, fg_color="transparent")
        path_frame.pack(fill="x", pady=(5, 30))
        self.settings_path_entry = ctk.CTkEntry(path_frame, height=40, fg_color=YT_SEARCH_BG, border_color=YT_SEARCH_BORDER, corner_radius=self.corner_rad)
        self.settings_path_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.settings_path_entry.insert(0, self.config["download_path"])
        ctk.CTkButton(path_frame, text="Browse", width=80, height=40, corner_radius=self.corner_rad, fg_color=YT_HOVER, hover_color=YT_SEARCH_BORDER, command=self.browse_download_path).pack(side="right")

        # Visuals
        ctk.CTkLabel(content, text="Appearance", font=ctk.CTkFont(size=16, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w")
        vis_frame = ctk.CTkFrame(content, fg_color="transparent")
        vis_frame.pack(fill="x", pady=(5, 30))
        
        ctk.CTkLabel(vis_frame, text="Base Theme:", width=100, anchor="w").pack(side="left")
        self.theme_var = ctk.StringVar(value=self.config["theme"])
        ctk.CTkOptionMenu(vis_frame, variable=self.theme_var, values=["Dark", "Light", "System"], fg_color=YT_SEARCH_BG, button_color=YT_SEARCH_BORDER).pack(side="left", padx=(0, 20))

        ctk.CTkLabel(vis_frame, text="UI Scale:", width=80, anchor="w").pack(side="left")
        self.scale_var = ctk.StringVar(value=str(self.config["ui_scale"]))
        ctk.CTkOptionMenu(vis_frame, variable=self.scale_var, values=["0.8", "0.9", "1.0", "1.1", "1.2"], fg_color=YT_SEARCH_BG, button_color=YT_SEARCH_BORDER).pack(side="left")

        # Save Button
        ctk.CTkButton(content, text="Save Settings", height=45, fg_color=YT_ACCENT_BLUE, corner_radius=22, font=ctk.CTkFont(weight="bold"), command=self.save_settings).pack(anchor="w")
        return frame

    def browse_download_path(self):
        path = filedialog.askdirectory(initialdir=self.config["download_path"])
        if path:
            self.settings_path_entry.delete(0, "end")
            self.settings_path_entry.insert(0, path)

    def save_settings(self):
        self.config["download_path"] = self.settings_path_entry.get()
        self.config["theme"] = self.theme_var.get()
        self.config["ui_scale"] = float(self.scale_var.get())
        self.config["search_results_count"] = int(self.results_count_var.get())

        new_max = int(self.max_concurrent_var.get())
        if new_max != self.config.get("max_concurrent_downloads", 3):
            old_executor = self.download_executor
            self.download_executor = concurrent.futures.ThreadPoolExecutor(max_workers=new_max, thread_name_prefix="dl")
            old_executor.shutdown(wait=False)
        self.config["max_concurrent_downloads"] = new_max

        self.save_config()
        
        ctk.set_appearance_mode(self.config["theme"])
        ctk.set_widget_scaling(self.config["ui_scale"])
        self.switch_view("settings")

    # --- YOUTUBE SEARCH LOGIC (no API key required, uses yt-dlp) ---
    def perform_search(self):
        query = self.search_entry.get().strip()
        if not query: return

        self.search_generation += 1
        gen = self.search_generation

        self.switch_view("search")
        for w in self.search_results_container.winfo_children(): w.destroy()

        # Instant path: we already have this query's results decoded/cached.
        cache_key = query.lower()
        cached_entries = self.search_cache.get(cache_key)
        if cached_entries is not None:
            self.search_cache.move_to_end(cache_key)
            self.render_search_results(cached_entries, gen)
            return

        ctk.CTkLabel(self.search_results_container, text=f"Searching YouTube for '{query}'...", font=ctk.CTkFont(size=16), text_color=YT_TEXT_SECONDARY).pack(pady=50)
        self.btn_search.configure(state="disabled")
        threading.Thread(target=self.search_worker, args=(query, gen), daemon=True).start()

    def search_worker(self, query, gen):
        result_count = int(self.config.get("search_results_count", 20))
        search_target = f"ytsearch{result_count}:{query}"

        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True,
            'noplaylist': True,
            'socket_timeout': 10,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_target, download=False)
                entries = list(info.get("entries", []) or []) if info else []
                entries = [e for e in entries if e]

            # Cache raw results for instant re-search later (bounded LRU).
            self.search_cache[query.lower()] = entries
            self.search_cache.move_to_end(query.lower())
            while len(self.search_cache) > self.SEARCH_CACHE_MAX:
                self.search_cache.popitem(last=False)

            if gen == self.search_generation:
                self.after(0, lambda: self.render_search_results(entries, gen))
        except Exception as e:
            if gen == self.search_generation:
                self.after(0, lambda: self.show_search_error("SEARCH FAILED", str(e)))
        finally:
            if gen == self.search_generation:
                self.after(0, lambda: self.btn_search.configure(state="normal"))

    def show_search_error(self, title, description):
        for w in self.search_results_container.winfo_children(): w.destroy()
        err_frame = ctk.CTkFrame(self.search_results_container, fg_color="transparent")
        err_frame.pack(pady=50)
        ctk.CTkLabel(err_frame, text="⚠️ " + title, font=ctk.CTkFont(size=20, weight="bold"), text_color=YT_ACCENT_RED).pack()
        ctk.CTkLabel(err_frame, text=description, font=ctk.CTkFont(size=14), text_color=YT_TEXT_PRIMARY, wraplength=600).pack(pady=10)

    def format_duration(self, seconds):
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            return ""
        if seconds <= 0:
            return ""
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def format_views(self, count):
        try:
            count = int(count)
        except (TypeError, ValueError):
            return ""
        if count >= 1_000_000_000:
            return f"{count/1_000_000_000:.1f}B views"
        if count >= 1_000_000:
            return f"{count/1_000_000:.1f}M views"
        if count >= 1_000:
            return f"{count/1_000:.1f}K views"
        return f"{count} views"

    def thumbnail_url_for(self, video_id):
        # mqdefault is YouTube's 320x180 thumbnail — exactly our display size, so most
        # images need zero resizing and the download itself is small (fast + light).
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"

    def render_search_results(self, items, gen):
        if gen != self.search_generation:
            return  # a newer search already superseded this one

        for w in self.search_results_container.winfo_children(): w.destroy()

        if not items:
            ctk.CTkLabel(self.search_results_container, text="No results found for this query.", text_color=YT_TEXT_SECONDARY).pack(pady=20)
            return

        for item in items:
            video_id = item.get("id", "")
            if not video_id: continue

            video_url = item.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            title = item.get("title") or "Unknown Title"
            channel = item.get("channel") or item.get("uploader") or "Unknown Channel"

            duration_str = self.format_duration(item.get("duration"))
            views_str = self.format_views(item.get("view_count"))
            meta_line = " • ".join([p for p in [channel, views_str] if p])

            # Result Card (Borderless YT Style)
            card = ctk.CTkFrame(self.search_results_container, fg_color="transparent")
            card.pack(fill="x", padx=40, pady=10)

            # Thumbnail Holder (Left) - fixed 16:9 box like a YouTube card
            thumb_holder = ctk.CTkFrame(card, width=320, height=180, fg_color=YT_HOVER, corner_radius=12)
            thumb_holder.pack(side="left", padx=(0, 20), pady=10)
            thumb_holder.pack_propagate(False)

            thumb_lbl = ctk.CTkLabel(thumb_holder, text="...", fg_color="transparent", text_color=YT_TEXT_SECONDARY)
            thumb_lbl.pack(expand=True, fill="both")

            if duration_str:
                dur_badge = ctk.CTkLabel(
                    thumb_holder, text=f" {duration_str} ", fg_color="#000000", text_color="#FFFFFF",
                    font=ctk.CTkFont(size=11, weight="bold"), corner_radius=4
                )
                dur_badge.place(relx=0.96, rely=0.90, anchor="se")

            # Instant path: already-decoded image sitting in the LRU cache — no network,
            # no decode, no resize, just an image swap.
            cached_img = self.thumb_cache.get(video_id)
            if cached_img is not None:
                self.thumb_cache.move_to_end(video_id)
                thumb_lbl.configure(image=cached_img, text="")
            else:
                thumb_url = self.thumbnail_url_for(video_id)
                self.thumb_executor.submit(self.load_thumbnail, thumb_url, thumb_lbl, video_id, gen)

            # Metadata Holder (Right)
            info_frame = ctk.CTkFrame(card, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, pady=10)

            ctk.CTkLabel(info_frame, text=title, font=ctk.CTkFont(size=18, weight="normal"), text_color=YT_TEXT_PRIMARY, anchor="w", justify="left", wraplength=550).pack(fill="x", anchor="w")
            ctk.CTkLabel(info_frame, text=meta_line, font=ctk.CTkFont(size=13), text_color=YT_TEXT_SECONDARY, anchor="w").pack(fill="x", anchor="w", pady=(5, 15))

            # Controls
            ctrl_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
            ctrl_frame.pack(anchor="w", fill="x")

            ui_tracker = self.create_download_ui(ctrl_frame)
            dl_btn = ctk.CTkButton(
                ctrl_frame, text="Download Video", width=120, height=32, corner_radius=16,
                fg_color=YT_TEXT_PRIMARY, text_color=YT_BG_MAIN, hover_color="#D1D1D1", font=ctk.CTkFont(weight="bold"),
                command=lambda u=video_url, t=ui_tracker, ttl=title: self.start_download(u, False, t, ttl)
            )
            dl_btn.pack(side="left")
            ui_tracker["btn"] = dl_btn

    def load_thumbnail(self, url, label_widget, video_id, gen):
        # Runs on a pooled worker thread. Bail out early if this search is already stale —
        # saves a network round trip and a JPEG decode for work nobody will ever see.
        if gen != self.search_generation:
            return
        try:
            resp = self.http_session.get(url, timeout=4)
            if resp.status_code != 200:
                return

            img = Image.open(BytesIO(resp.content))
            # draft() lets the JPEG decoder downsample *while* decoding instead of decoding
            # full-size then resizing — much cheaper when a fallback image is larger than 320x180.
            try:
                img.draft("RGB", (320, 180))
            except Exception:
                pass
            img.load()

            if img.mode != "RGB":
                img = img.convert("RGB")
            if img.size != (320, 180):
                img = img.resize((320, 180), Image.Resampling.LANCZOS)

            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(320, 180))

            with self.thumb_cache_lock:
                self.thumb_cache[video_id] = ctk_img
                self.thumb_cache.move_to_end(video_id)
                while len(self.thumb_cache) > self.THUMB_CACHE_MAX:
                    self.thumb_cache.popitem(last=False)

            self.after(0, lambda: self._apply_thumbnail(label_widget, ctk_img, gen))
        except Exception:
            pass

    def _apply_thumbnail(self, label_widget, ctk_img, gen):
        if gen != self.search_generation:
            return  # search moved on; widget may already be gone
        try:
            label_widget.configure(image=ctk_img, text="")
        except Exception:
            pass  # widget was destroyed mid-flight; nothing to do

    # --- DIRECT URL LOGIC ---
    def analyze_direct_url(self):
        url = self.direct_url_entry.get().strip()
        if not url: return

        self.btn_analyze.configure(state="disabled")
        for w in self.direct_embed_holder.winfo_children(): w.destroy()
        ctk.CTkLabel(self.direct_embed_holder, text="Pulling metadata...", text_color=YT_TEXT_SECONDARY).pack(pady=10)
        threading.Thread(target=self.analyze_direct_worker, args=(url,), daemon=True).start()

    def analyze_direct_worker(self, url):
        ydl_opts = {'quiet': True, 'extract_flat': True}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                self.after(0, lambda: self.render_direct_embed(info, url))
        except Exception as e:
            self.after(0, lambda: self.show_direct_error(f"Failed: {str(e)}"))
        finally:
            self.after(0, lambda: self.btn_analyze.configure(state="normal"))

    def show_direct_error(self, msg):
        for w in self.direct_embed_holder.winfo_children(): w.destroy()
        ctk.CTkLabel(self.direct_embed_holder, text=msg, text_color=YT_ACCENT_RED, wraplength=600).pack(pady=10)

    def render_direct_embed(self, info, url):
        for w in self.direct_embed_holder.winfo_children(): w.destroy()
        
        is_playlist = 'entries' in info
        title = info.get('title', 'Unknown Media')
        
        ctk.CTkLabel(self.direct_embed_holder, text=title, font=ctk.CTkFont(size=18, weight="bold"), text_color=YT_TEXT_PRIMARY, wraplength=600).pack(anchor="w", pady=(20, 5))
        ctk.CTkLabel(self.direct_embed_holder, text=f"Playlist • {len(list(info.get('entries', [])))} videos" if is_playlist else "Single Video", font=ctk.CTkFont(size=13), text_color=YT_TEXT_SECONDARY).pack(anchor="w", pady=(0, 20))

        tracker_frame = ctk.CTkFrame(self.direct_embed_holder, fg_color="transparent")
        tracker_frame.pack(fill="x", pady=(0, 20))
        
        ui_tracker = self.create_download_ui(tracker_frame)
        dl_btn = ctk.CTkButton(
            tracker_frame, text="Download Entire Playlist" if is_playlist else "Download Media", 
            width=150, height=36, corner_radius=18, fg_color=YT_TEXT_PRIMARY, text_color=YT_BG_MAIN, hover_color="#D1D1D1", font=ctk.CTkFont(weight="bold"),
            command=lambda: self.start_download(url, is_playlist, ui_tracker, title)
        )
        dl_btn.pack(side="left")
        ui_tracker["btn"] = dl_btn

    # --- SHARED DOWNLOAD ENGINE ---
    def create_download_ui(self, parent_frame):
        # We start hidden, packed only when download begins
        prog_frame = ctk.CTkFrame(parent_frame, fg_color="transparent")
        bar = ctk.CTkProgressBar(prog_frame, height=4, progress_color=YT_ACCENT_RED, fg_color=YT_SEARCH_BORDER)
        bar.set(0)
        lbl = ctk.CTkLabel(prog_frame, text="Ready.", text_color=YT_TEXT_SECONDARY, font=ctk.CTkFont(size=12))
        return {"frame": prog_frame, "bar": bar, "lbl": lbl, "btn": None}

    def start_download(self, url, is_playlist, ui_tracker, title="Unknown Title"):
        save_dir = self.config["download_path"]
        if not os.path.isdir(save_dir):
            ui_tracker["lbl"].configure(text="Invalid save path in Settings.", text_color=YT_ACCENT_RED)
            ui_tracker["frame"].pack(side="left", fill="x", expand=True, padx=(15, 0))
            ui_tracker["lbl"].pack(anchor="w")
            return

        ui_tracker["btn"].configure(state="disabled")
        ui_tracker["frame"].pack(side="left", fill="x", expand=True, padx=(15, 0))
        ui_tracker["lbl"].pack(anchor="w")
        ui_tracker["bar"].pack(fill="x", pady=(5, 0))
        
        ui_tracker["bar"].set(0)
        ui_tracker["lbl"].configure(text="Queued...", text_color=YT_TEXT_PRIMARY)

        # Register with the central download manager (drives the Downloads page) and hand
        # the actual work off to the bounded pool, so several downloads can run at once.
        did = self.register_download(url, title, is_playlist, save_dir, ui_tracker)
        self.download_executor.submit(self.download_worker, did)

    # --- DOWNLOAD MANAGER (multi-download tracking, history, cancellation) ---
    def register_download(self, url, title, is_playlist, save_dir, ui_tracker):
        with self.downloads_lock:
            did = self.download_id_counter
            self.download_id_counter += 1
            rec = {
                "id": did,
                "title": title or "Unknown Title",
                "url": url,
                "is_playlist": is_playlist,
                "status": "Queued",
                "progress": 0.0,
                "status_text": "Queued...",
                "save_dir": save_dir,
                "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": None,
                "error": None,
                "cancel_event": threading.Event(),
            }
            self.downloads[did] = rec
        if ui_tracker is not None:
            self.ui_trackers[did] = ui_tracker
        self.update_downloads_nav_badge()
        self.refresh_downloads_page(structural=True)
        return did

    def download_worker(self, did):
        with self.downloads_lock:
            rec = self.downloads.get(did)
        if not rec:
            return

        if rec["cancel_event"].is_set():
            self.finish_download(did, "Cancelled", "Cancelled by user")
            return

        url, is_playlist, save_dir = rec["url"], rec["is_playlist"], rec["save_dir"]
        cancel_event = rec["cancel_event"]

        self.update_download_progress(did, status="Downloading", status_text="Connecting to media server...")

        def progress_hook(d):
            # Raising inside a yt-dlp progress hook halts the download in progress —
            # this is how Cancel actually takes effect, not just a UI label change.
            if cancel_event.is_set():
                raise yt_dlp.utils.DownloadCancelled()
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                progress = (d.get('downloaded_bytes', 0) / total) if total else None

                pct, spd = d.get('_percent_str', '0%').strip(), d.get('_speed_str', 'N/A')
                status_text = f"[{d.get('info_dict', {}).get('playlist_index', '?')}/{d.get('info_dict', {}).get('n_entries', '?')}] - {pct} ({spd})" if is_playlist else f"Downloading: {pct} at {spd}"
                self.update_download_progress(did, progress=progress, status_text=status_text)
            elif d['status'] == 'finished':
                self.update_download_progress(did, status_text="Merging video and audio layers...")

        ydl_opts = {
            'ffmpeg_location': self.ffmpeg_path,
            'progress_hooks': [progress_hook],
            'quiet': True, 'noprogress': True,
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'merge_output_format': 'mp4',
            'noplaylist': not is_playlist,
            'outtmpl': os.path.join(save_dir, '%(playlist_title)s', '%(playlist_index)03d - %(title)s.%(ext)s') if is_playlist else os.path.join(save_dir, '%(title)s.%(ext)s')
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl: ydl.download([url])
            self.finish_download(did, "Completed", "Download Complete")
        except yt_dlp.utils.DownloadCancelled:
            self.finish_download(did, "Cancelled", "Cancelled by user")
        except Exception as e:
            if cancel_event.is_set():
                self.finish_download(did, "Cancelled", "Cancelled by user")
            else:
                self.finish_download(did, "Failed", "Download Failed.", error=str(e))

    def update_download_progress(self, did, progress=None, status_text=None, status=None):
        # Runs on a worker thread: update the shared record, then hop to the main thread for UI.
        with self.downloads_lock:
            rec = self.downloads.get(did)
            if not rec:
                return
            if progress is not None: rec["progress"] = progress
            if status_text is not None: rec["status_text"] = status_text
            if status is not None: rec["status"] = status
        self.after(0, lambda: self._apply_inplace_update(did))

    def _apply_inplace_update(self, did):
        # Cheap, frequent updates (percent/speed ticks) just mutate existing widgets in
        # place rather than rebuilding the page, so progress stays smooth.
        with self.downloads_lock:
            rec = self.downloads.get(did)
        if not rec:
            return
        ui = self.ui_trackers.get(did)
        if ui:
            try:
                if rec.get("progress") is not None:
                    ui["bar"].set(rec["progress"])
                ui["lbl"].configure(text=rec.get("status_text", ""), text_color=YT_TEXT_PRIMARY)
            except Exception:
                pass  # widget may have been destroyed (e.g. user re-searched)
        widgets = self.active_card_widgets.get(did)
        if widgets:
            try:
                if rec.get("progress") is not None:
                    widgets["bar"].set(rec["progress"])
                widgets["status_lbl"].configure(text=rec.get("status_text", ""))
            except Exception:
                pass

    def cancel_download(self, did):
        with self.downloads_lock:
            rec = self.downloads.get(did)
            if rec and rec["status"] in ACTIVE_STATUSES:
                rec["cancel_event"].set()
                rec["status_text"] = "Cancelling..."
        self._apply_inplace_update(did)

    def finish_download(self, did, status, status_text, error=None):
        with self.downloads_lock:
            rec = self.downloads.get(did)
            if rec:
                rec["status"] = status
                rec["status_text"] = status_text
                if status == "Completed":
                    rec["progress"] = 1.0
                rec["error"] = error
                rec["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_download_history()
        self.after(0, lambda: self._finish_download_ui(did, status, status_text))

    def _finish_download_ui(self, did, status, status_text):
        ui = self.ui_trackers.pop(did, None)
        if ui:
            try:
                color = STATUS_COLORS.get(status, YT_TEXT_PRIMARY)
                ui["lbl"].configure(text=status_text, text_color=color)
                ui["btn"].configure(state="normal", text="Download Again")
            except Exception:
                pass
        self.update_downloads_nav_badge()
        # The download just moved from the Active section to History — needs a full rebuild.
        self.refresh_downloads_page(structural=True)

    def refresh_downloads_page(self, structural=False):
        if self.current_view_name != "downloads":
            return  # don't waste cycles rebuilding a page nobody is looking at
        if structural:
            self.render_downloads_view()

    def update_downloads_nav_badge(self):
        with self.downloads_lock:
            active_count = sum(1 for r in self.downloads.values() if r["status"] in ACTIVE_STATUSES)
        btn = self.nav_buttons.get("downloads")
        if btn:
            btn.configure(text=f"📥  Downloads ({active_count})" if active_count else "📥  Downloads")

    # --- DOWNLOAD HISTORY PERSISTENCE ---
    def load_download_history(self):
        self.downloads = OrderedDict()
        max_id = 0
        if os.path.exists(DOWNLOAD_HISTORY_FILE):
            try:
                with open(DOWNLOAD_HISTORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for entry in data:
                    did = entry.get("id")
                    if did is None:
                        continue
                    entry["cancel_event"] = threading.Event()
                    self.downloads[did] = entry
                    max_id = max(max_id, int(did))
            except Exception:
                pass
        self.download_id_counter = max_id + 1

    def save_download_history(self):
        try:
            with self.downloads_lock:
                data = [
                    {k: v for k, v in rec.items() if k != "cancel_event"}
                    for rec in self.downloads.values()
                    if rec.get("status") in FINISHED_STATUSES
                ]
            data = data[-300:]  # bounded history file
            with open(DOWNLOAD_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # --- DOWNLOADS PAGE ---
    def create_downloads_view(self):
        frame = ctk.CTkScrollableFrame(self.main_container, fg_color="transparent")
        self.downloads_view_frame = frame
        return frame

    def render_downloads_view(self):
        frame = self.downloads_view_frame
        for w in frame.winfo_children(): w.destroy()
        self.active_card_widgets = {}

        content = ctk.CTkFrame(frame, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=40, pady=30)

        ctk.CTkLabel(content, text="Downloads", font=ctk.CTkFont(size=28, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w", pady=(0, 25))

        with self.downloads_lock:
            records = list(self.downloads.values())
        active = sorted([r for r in records if r["status"] in ACTIVE_STATUSES], key=lambda r: r["id"], reverse=True)
        finished = sorted([r for r in records if r["status"] in FINISHED_STATUSES], key=lambda r: r["id"], reverse=True)

        # --- Active Downloads ---
        ctk.CTkLabel(content, text=f"Active Downloads ({len(active)})", font=ctk.CTkFont(size=18, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(anchor="w", pady=(0, 10))
        if not active:
            ctk.CTkLabel(content, text="No active downloads.", text_color=YT_TEXT_SECONDARY, font=ctk.CTkFont(size=13)).pack(anchor="w", pady=(0, 25))
        else:
            for rec in active:
                self._build_active_card(content, rec)
            ctk.CTkFrame(content, height=1, fg_color="transparent").pack(pady=(0, 15))

        ctk.CTkFrame(content, height=1, fg_color=YT_SEARCH_BORDER).pack(fill="x", pady=10)

        # --- History ---
        hist_header = ctk.CTkFrame(content, fg_color="transparent")
        hist_header.pack(fill="x", pady=(15, 10))
        ctk.CTkLabel(hist_header, text=f"History ({len(finished)})", font=ctk.CTkFont(size=18, weight="bold"), text_color=YT_TEXT_PRIMARY).pack(side="left")
        if finished:
            ctk.CTkButton(
                hist_header, text="Clear History", width=110, height=28, corner_radius=14,
                fg_color=YT_HOVER, hover_color=YT_SEARCH_BORDER, text_color=YT_TEXT_PRIMARY,
                font=ctk.CTkFont(size=12), command=self.clear_download_history
            ).pack(side="right")

        if not finished:
            ctk.CTkLabel(content, text="No downloads yet.", text_color=YT_TEXT_SECONDARY, font=ctk.CTkFont(size=13)).pack(anchor="w")
        else:
            for rec in finished[:200]:
                self._build_history_row(content, rec)

    def _build_active_card(self, parent, rec):
        did = rec["id"]
        card = ctk.CTkFrame(parent, fg_color=YT_SEARCH_BG, corner_radius=self.corner_rad)
        card.pack(fill="x", pady=6)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=15, pady=12)

        top_row = ctk.CTkFrame(inner, fg_color="transparent")
        top_row.pack(fill="x")
        ctk.CTkLabel(top_row, text=rec["title"], font=ctk.CTkFont(size=14, weight="bold"), text_color=YT_TEXT_PRIMARY, anchor="w", justify="left", wraplength=600).pack(side="left", fill="x", expand=True)

        cancel_btn = ctk.CTkButton(
            top_row, text="Cancel", width=70, height=26, corner_radius=13,
            fg_color="transparent", border_width=1, border_color=YT_ACCENT_RED, text_color=YT_ACCENT_RED,
            hover_color=YT_HOVER, font=ctk.CTkFont(size=12), command=lambda d=did: self.cancel_download(d)
        )
        cancel_btn.pack(side="right", padx=(10, 0))

        bar = ctk.CTkProgressBar(inner, height=4, progress_color=YT_ACCENT_RED, fg_color=YT_SEARCH_BORDER)
        bar.set(rec.get("progress") or 0)
        bar.pack(fill="x", pady=(10, 5))

        status_lbl = ctk.CTkLabel(inner, text=rec.get("status_text", rec["status"]), font=ctk.CTkFont(size=12), text_color=YT_TEXT_SECONDARY, anchor="w")
        status_lbl.pack(fill="x", anchor="w")

        self.active_card_widgets[did] = {"bar": bar, "status_lbl": status_lbl, "cancel_btn": cancel_btn}

    def _build_history_row(self, parent, rec):
        did = rec["id"]
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)

        status = rec.get("status", "")
        color = STATUS_COLORS.get(status, YT_TEXT_SECONDARY)
        ctk.CTkLabel(
            row, text=status, width=80, height=22, fg_color=YT_HOVER, text_color=color,
            corner_radius=11, font=ctk.CTkFont(size=11, weight="bold")
        ).pack(side="left", padx=(0, 12))

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(info, text=rec.get("title", "Unknown"), font=ctk.CTkFont(size=13), text_color=YT_TEXT_PRIMARY, anchor="w", justify="left", wraplength=520).pack(fill="x", anchor="w")
        sub = rec.get("finished_at") or rec.get("started_at") or ""
        if status == "Failed" and rec.get("error"):
            sub = f"{sub}  •  {rec['error'][:80]}"
        ctk.CTkLabel(info, text=sub, font=ctk.CTkFont(size=11), text_color=YT_TEXT_SECONDARY, anchor="w").pack(fill="x", anchor="w")

        btns = ctk.CTkFrame(row, fg_color="transparent")
        btns.pack(side="right")
        if status == "Completed":
            ctk.CTkButton(
                btns, text="Open Folder", width=90, height=26, corner_radius=13,
                fg_color=YT_HOVER, hover_color=YT_SEARCH_BORDER, text_color=YT_TEXT_PRIMARY,
                font=ctk.CTkFont(size=11), command=lambda p=rec.get("save_dir"): self.open_in_file_manager(p)
            ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btns, text="✕", width=28, height=26, corner_radius=13, fg_color="transparent",
            hover_color=YT_HOVER, text_color=YT_TEXT_SECONDARY, command=lambda d=did: self.remove_history_entry(d)
        ).pack(side="left")

    def clear_download_history(self):
        with self.downloads_lock:
            for did in [d for d, r in self.downloads.items() if r["status"] in FINISHED_STATUSES]:
                del self.downloads[did]
        self.save_download_history()
        self.render_downloads_view()

    def remove_history_entry(self, did):
        with self.downloads_lock:
            self.downloads.pop(did, None)
        self.save_download_history()
        self.render_downloads_view()

    def open_in_file_manager(self, path):
        if not path or not os.path.isdir(path):
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

if __name__ == "__main__":
    app = YouTubeCloneApp()
    app.mainloop()