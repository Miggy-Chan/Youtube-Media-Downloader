# -*- coding: utf-8 -*-
"""
YouTube Media Pro -- Android Edition
=====================================
A Kivy/KivyMD rewrite of the original desktop tkinter/customtkinter app, targeting
Android 11-16 (API 30+).

WHY A REWRITE WAS NECESSARY
----------------------------
The original app used tkinter/customtkinter for its UI. There is no Tk runtime for
Android, so that UI layer cannot run on a phone at all -- this is a from-scratch UI
built on Kivy/KivyMD instead, which DOES compile for Android via python-for-android.

DESIGN TRADE-OFFS (read this before you build)
------------------------------------------------
1. NO FFMPEG. Bundling ffmpeg for Android via python-for-android adds major build
   complexity, build time, and APK size. To avoid it, downloads are restricted to
   "progressive" formats -- single files that already contain both audio and video,
   muxed by YouTube. No merging or transcoding is needed. This caps quality at
   whatever YouTube serves as a combined stream (commonly up to 720p, occasionally
   1080p depending on the video). Audio-only downloads save the native container
   (m4a/webm) rather than converting to mp3, since mp3 conversion also needs ffmpeg.
2. STORAGE. Files save to this app's own external storage directory
   (Android/data/<package>/files/Downloads). On Android 10+ this requires NO runtime
   storage permission at all. A "Share" button on each finished download hands the
   file to Android's normal share sheet so you can move/export it anywhere you like.
3. NOT RUNTIME-TESTED. Kivy/KivyMD/Android build tooling isn't available in the
   sandbox this was written in (no network access to install them). The build is
   validated by GitHub Actions (see the included workflow), not by me. Expect to
   debug a few rounds of build errors -- that's normal for a first Android build.
"""

import os
import sys
import json
import threading
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.screenmanager import ScreenManager, Screen, NoTransition
from kivy.uix.image import AsyncImage
from kivy.core.window import Window

from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.card import MDCard
from kivymd.uix.label import MDLabel, MDIcon
from kivymd.uix.button import MDRaisedButton, MDFlatButton, MDIconButton
from kivymd.uix.textfield import MDTextField
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.menu import MDDropdownMenu
from kivymd.uix.dialog import MDDialog
from kivymd.uix.snackbar import Snackbar
from kivymd.uix.spinner import MDSpinner

import yt_dlp

# ---------------------------------------------------------------------------
# Platform detection / Android storage helpers
# ---------------------------------------------------------------------------
ON_ANDROID = False
try:
    from jnius import autoclass  # noqa: F401
    ON_ANDROID = True
except Exception:
    ON_ANDROID = False


def get_app_private_dir():
    """Internal app storage for config/history. Never needs a permission."""
    if ON_ANDROID:
        try:
            from android.storage import app_storage_path
            path = app_storage_path()
        except Exception:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            path = PythonActivity.mActivity.getFilesDir().getAbsolutePath()
    else:
        path = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(path, exist_ok=True)
    return path


def get_download_dir():
    """App-specific external storage. Visible to most file managers under
    Android/data/<package>/files/Downloads. No runtime permission required on
    Android 10+. Falls back to ~/Downloads when running off-device."""
    if ON_ANDROID:
        try:
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            context = PythonActivity.mActivity
            ext_dir = context.getExternalFilesDir(None)
            base = ext_dir.getAbsolutePath() if ext_dir else get_app_private_dir()
        except Exception:
            base = get_app_private_dir()
    else:
        base = os.path.expanduser("~")
    path = os.path.join(base, "Downloads")
    os.makedirs(path, exist_ok=True)
    return path


def share_file(path):
    """Hand a finished file to Android's share sheet so it can be moved/exported
    anywhere -- another folder, cloud storage, another app."""
    if not ON_ANDROID:
        return
    try:
        from plyer import share
        share.share(filepath=path)
    except Exception:
        traceback.print_exc()


def open_downloads_folder():
    """Best-effort: open a file manager pointed at the app's download folder."""
    if not ON_ANDROID:
        return
    try:
        Intent = autoclass('android.content.Intent')
        Uri = autoclass('android.net.Uri')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        File = autoclass('java.io.File')
        f = File(get_download_dir())
        intent = Intent(Intent.ACTION_VIEW)
        intent.setDataAndType(Uri.fromFile(f), "resource/folder")
        PythonActivity.mActivity.startActivity(intent)
    except Exception:
        traceback.print_exc()


CONFIG_FILE = lambda: os.path.join(get_app_private_dir(), "config.json")
HISTORY_FILE = lambda: os.path.join(get_app_private_dir(), "download_history.json")

ACTIVE_STATUSES = {"Queued", "Downloading"}
FINISHED_STATUSES = {"Completed", "Failed", "Cancelled"}
STATUS_COLOR = {
    "Completed": (0.243, 0.651, 1, 1),   # blue
    "Failed": (1, 0.2, 0.2, 1),          # red
    "Cancelled": (0.667, 0.667, 0.667, 1),  # grey
    "Queued": (0.667, 0.667, 0.667, 1),
    "Downloading": (1, 0.2, 0.2, 1),
}

# ---------------------------------------------------------------------------
# Theme colors (mirrors the original dark YouTube look)
# ---------------------------------------------------------------------------
def hx(h, a=1.0):
    h = h.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return [r, g, b, a]


C_BG = hx("0F0F0F")
C_CARD = hx("1A1A1A")
C_BORDER = hx("303030")
C_TEXT = hx("F1F1F1")
C_TEXT_DIM = hx("AAAAAA")
C_RED = hx("FF0000")
C_BLUE = hx("3EA6FF")

KV = """
#:import dp kivy.metrics.dp

<RootNav>:
    orientation: "vertical"
    canvas.before:
        Color:
            rgba: app.c_bg
        Rectangle:
            pos: self.pos
            size: self.size

    MDBoxLayout:
        id: topbar
        size_hint_y: None
        height: dp(56)
        padding: dp(12), 0
        spacing: dp(8)
        canvas.before:
            Color:
                rgba: app.c_bg
            Rectangle:
                pos: self.pos
                size: self.size
        MDLabel:
            text: "[color=FF0000]\u25B6[/color] YouTube Media Pro"
            markup: True
            bold: True
            font_style: "H6"
            color: app.c_text

    ScreenManager:
        id: sm

    MDBoxLayout:
        id: navbar
        size_hint_y: None
        height: dp(58)
        canvas.before:
            Color:
                rgba: app.c_card
            Rectangle:
                pos: self.pos
                size: self.size
            Color:
                rgba: app.c_border
            Line:
                points: [self.x, self.top, self.right, self.top]
                width: 1

        MDBoxLayout:
            id: nav_search
            orientation: "vertical"
            on_touch_down: if self.collide_point(*args[1].pos): app.switch_screen("search")
            MDIcon:
                icon: "magnify"
                halign: "center"
                theme_text_color: "Custom"
                text_color: app.c_red if app.current_screen == "search" else app.c_text_dim
            MDLabel:
                text: "Search"
                halign: "center"
                font_style: "Caption"
                color: app.c_red if app.current_screen == "search" else app.c_text_dim

        MDBoxLayout:
            id: nav_downloads
            orientation: "vertical"
            on_touch_down: if self.collide_point(*args[1].pos): app.switch_screen("downloads")
            MDIcon:
                icon: "tray-arrow-down"
                halign: "center"
                theme_text_color: "Custom"
                text_color: app.c_red if app.current_screen == "downloads" else app.c_text_dim
            MDLabel:
                text: "Downloads"
                halign: "center"
                font_style: "Caption"
                color: app.c_red if app.current_screen == "downloads" else app.c_text_dim

        MDBoxLayout:
            id: nav_settings
            orientation: "vertical"
            on_touch_down: if self.collide_point(*args[1].pos): app.switch_screen("settings")
            MDIcon:
                icon: "cog"
                halign: "center"
                theme_text_color: "Custom"
                text_color: app.c_red if app.current_screen == "settings" else app.c_text_dim
            MDLabel:
                text: "Settings"
                halign: "center"
                font_style: "Caption"
                color: app.c_red if app.current_screen == "settings" else app.c_text_dim
"""


class RootNav(MDBoxLayout):
    pass


# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------
class ResultCard(MDCard):
    def __init__(self, app, entry, **kwargs):
        super().__init__(
            orientation="vertical",
            size_hint_y=None,
            height=dp(230),
            md_bg_color=C_CARD,
            radius=[dp(10)],
            padding=dp(10),
            spacing=dp(6),
            **kwargs,
        )
        self.app = app
        self.entry = entry

        thumb_url = self._best_thumb(entry)
        img = AsyncImage(
            source=thumb_url or "",
            size_hint_y=None,
            height=dp(130),
            allow_stretch=True,
            keep_ratio=True,
        )
        self.add_widget(img)

        title = entry.get("title") or "Untitled"
        self.add_widget(MDLabel(
            text=title, color=C_TEXT, bold=True, font_style="Subtitle2",
            size_hint_y=None, height=dp(40), shorten=True, shorten_from="right",
        ))

        meta = self._meta_text(entry)
        self.add_widget(MDLabel(
            text=meta, color=C_TEXT_DIM, font_style="Caption",
            size_hint_y=None, height=dp(18),
        ))

        btn_row = MDBoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
        dl_btn = MDRaisedButton(
            text="Download", md_bg_color=C_RED, text_color=(1, 1, 1, 1),
            size_hint_x=1,
        )
        dl_btn.bind(on_release=self.open_quality_menu)
        btn_row.add_widget(dl_btn)
        self.add_widget(btn_row)

        self._menu = None

    @staticmethod
    def _best_thumb(entry):
        thumbs = entry.get("thumbnails") or []
        if thumbs:
            return thumbs[-1].get("url")
        return entry.get("thumbnail")

    @staticmethod
    def _meta_text(entry):
        parts = []
        ch = entry.get("channel") or entry.get("uploader")
        if ch:
            parts.append(ch)
        dur = entry.get("duration")
        if isinstance(dur, (int, float)):
            m, s = divmod(int(dur), 60)
            h, m = divmod(m, 60)
            parts.append(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")
        return "  \u2022  ".join(parts) if parts else ""

    def open_quality_menu(self, button):
        items = [
            {"text": "Best available (video+audio)", "viewclass": "OneLineListItem",
             "on_release": lambda: self._pick("best")},
            {"text": "Audio only", "viewclass": "OneLineListItem",
             "on_release": lambda: self._pick("audio")},
        ]
        self._menu = MDDropdownMenu(caller=button, items=items, width_mult=4)
        self._menu.open()

    def _pick(self, quality):
        if self._menu:
            self._menu.dismiss()
        self.app.start_download(self.entry, quality)


class ActiveDownloadCard(MDCard):
    def __init__(self, app, record, **kwargs):
        super().__init__(
            orientation="vertical", size_hint_y=None, height=dp(96),
            md_bg_color=C_CARD, radius=[dp(10)], padding=dp(12), spacing=dp(6),
            **kwargs,
        )
        self.app = app
        self.did = record["id"]

        top = MDBoxLayout(size_hint_y=None, height=dp(24), spacing=dp(8))
        top.add_widget(MDLabel(
            text=record["title"], color=C_TEXT, bold=True, font_style="Caption",
            shorten=True, shorten_from="right",
        ))
        cancel = MDIconButton(icon="close", theme_text_color="Custom", text_color=C_RED)
        cancel.bind(on_release=lambda *_: app.cancel_download(self.did))
        top.add_widget(cancel)
        self.add_widget(top)

        self.bar = MDProgressBar(value=(record.get("progress") or 0) * 100, max=100)
        self.add_widget(self.bar)

        self.status_lbl = MDLabel(
            text=record.get("status_text", record["status"]), color=C_TEXT_DIM,
            font_style="Caption", size_hint_y=None, height=dp(18),
        )
        self.add_widget(self.status_lbl)

    def refresh(self, record):
        self.bar.value = (record.get("progress") or 0) * 100
        self.status_lbl.text = record.get("status_text", record["status"])


class HistoryRow(MDBoxLayout):
    def __init__(self, app, record, **kwargs):
        super().__init__(
            size_hint_y=None, height=dp(64), spacing=dp(10), padding=(dp(4), 0),
            **kwargs,
        )
        status = record.get("status", "")
        chip = MDLabel(
            text=status, size_hint=(None, None), size=(dp(80), dp(24)),
            halign="center", font_style="Caption", bold=True,
            color=STATUS_COLOR.get(status, C_TEXT_DIM),
        )
        self.add_widget(chip)

        info = MDBoxLayout(orientation="vertical")
        info.add_widget(MDLabel(
            text=record.get("title", "Unknown"), color=C_TEXT, font_style="Body2",
            shorten=True, shorten_from="right",
        ))
        sub = record.get("finished_at") or record.get("started_at") or ""
        if status == "Failed" and record.get("error"):
            sub = f"{sub}  \u2022  {str(record['error'])[:60]}"
        info.add_widget(MDLabel(text=sub, color=C_TEXT_DIM, font_style="Caption"))
        self.add_widget(info)

        btns = MDBoxLayout(size_hint_x=None, width=dp(88))
        if status == "Completed" and record.get("save_path"):
            share_btn = MDIconButton(icon="share-variant", theme_text_color="Custom", text_color=C_BLUE)
            share_btn.bind(on_release=lambda *_: share_file(record["save_path"]))
            btns.add_widget(share_btn)
        remove_btn = MDIconButton(icon="trash-can-outline", theme_text_color="Custom", text_color=C_TEXT_DIM)
        remove_btn.bind(on_release=lambda *_: app.remove_history_entry(record["id"]))
        btns.add_widget(remove_btn)
        self.add_widget(btns)


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------
class SearchScreen(Screen):
    def __init__(self, app, **kwargs):
        super().__init__(name="search", **kwargs)
        self.app = app
        root = MDBoxLayout(orientation="vertical")

        bar = MDBoxLayout(size_hint_y=None, height=dp(56), padding=dp(10), spacing=dp(8))
        self.field = MDTextField(hint_text="Search YouTube or paste a video URL", multiline=False)
        self.field.bind(on_text_validate=lambda *_: self.do_search())
        bar.add_widget(self.field)
        go_btn = MDIconButton(icon="magnify", theme_text_color="Custom", text_color=C_RED)
        go_btn.bind(on_release=lambda *_: self.do_search())
        bar.add_widget(go_btn)
        root.add_widget(bar)

        self.status_lbl = MDLabel(
            text="Search for a video to get started.", color=C_TEXT_DIM,
            halign="center", size_hint_y=None, height=dp(30),
        )
        root.add_widget(self.status_lbl)

        self.scroll = MDScrollView()
        self.results_box = MDBoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(10), padding=dp(10))
        self.results_box.bind(minimum_height=self.results_box.setter("height"))
        self.scroll.add_widget(self.results_box)
        root.add_widget(self.scroll)

        self.add_widget(root)

    def do_search(self):
        query = self.field.text.strip()
        if not query:
            return
        self.status_lbl.text = "Searching\u2026"
        self.results_box.clear_widgets()
        threading.Thread(target=self._search_thread, args=(query,), daemon=True).start()

    def _search_thread(self, query):
        try:
            if query.startswith("http://") or query.startswith("https://"):
                ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(query, download=False)
                entries = [info]
            else:
                n = self.app.config.get("search_results_count", 15)
                ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
                entries = info.get("entries") or []
        except Exception as e:
            Clock.schedule_once(lambda dt: self._search_failed(str(e)))
            return
        Clock.schedule_once(lambda dt: self._search_done(entries))

    def _search_failed(self, msg):
        self.status_lbl.text = f"Search failed: {msg[:80]}"

    def _search_done(self, entries):
        self.results_box.clear_widgets()
        if not entries:
            self.status_lbl.text = "No results found."
            return
        self.status_lbl.text = f"{len(entries)} result(s)"
        for entry in entries:
            self.results_box.add_widget(ResultCard(self.app, entry))


class DownloadsScreen(Screen):
    def __init__(self, app, **kwargs):
        super().__init__(name="downloads", **kwargs)
        self.app = app
        self.active_cards = {}

        root = MDBoxLayout(orientation="vertical")
        self.scroll = MDScrollView()
        self.content = MDBoxLayout(orientation="vertical", size_hint_y=None, spacing=dp(10), padding=dp(14))
        self.content.bind(minimum_height=self.content.setter("height"))
        self.scroll.add_widget(self.content)
        root.add_widget(self.scroll)
        self.add_widget(root)

        self.active_label = MDLabel(color=C_TEXT, bold=True, font_style="H6", size_hint_y=None, height=dp(30))
        self.active_empty = MDLabel(text="No active downloads.", color=C_TEXT_DIM, size_hint_y=None, height=dp(24))
        self.history_header = MDBoxLayout(size_hint_y=None, height=dp(36))
        self.history_label = MDLabel(color=C_TEXT, bold=True, font_style="H6")
        clear_btn = MDFlatButton(text="Clear history", text_color=C_TEXT_DIM)
        clear_btn.bind(on_release=lambda *_: app.clear_download_history())
        self.history_header.add_widget(self.history_label)
        self.history_header.add_widget(clear_btn)
        self.history_empty = MDLabel(text="No downloads yet.", color=C_TEXT_DIM, size_hint_y=None, height=dp(24))

        self.rebuild()

    def on_pre_enter(self, *args):
        self.rebuild()

    def rebuild(self):
        self.content.clear_widgets()
        self.active_cards = {}

        records = list(self.app.downloads.values())
        active = sorted([r for r in records if r["status"] in ACTIVE_STATUSES], key=lambda r: r["id"], reverse=True)
        finished = sorted([r for r in records if r["status"] in FINISHED_STATUSES], key=lambda r: r["id"], reverse=True)

        self.active_label.text = f"Active Downloads ({len(active)})"
        self.content.add_widget(self.active_label)
        if not active:
            self.content.add_widget(self.active_empty)
        else:
            for rec in active:
                card = ActiveDownloadCard(self.app, rec)
                self.active_cards[rec["id"]] = card
                self.content.add_widget(card)

        self.history_label.text = f"History ({len(finished)})"
        self.content.add_widget(self.history_header)
        if not finished:
            self.content.add_widget(self.history_empty)
        else:
            for rec in finished[:150]:
                self.content.add_widget(HistoryRow(self.app, rec))

    def refresh_active(self, did, record):
        card = self.active_cards.get(did)
        if card:
            card.refresh(record)


class SettingsScreen(Screen):
    def __init__(self, app, **kwargs):
        super().__init__(name="settings", **kwargs)
        self.app = app
        root = MDBoxLayout(orientation="vertical", padding=dp(20), spacing=dp(18))

        root.add_widget(MDLabel(text="Settings", color=C_TEXT, bold=True, font_style="H5",
                                 size_hint_y=None, height=dp(40)))

        root.add_widget(MDLabel(text="Download folder", color=C_TEXT_DIM, font_style="Caption",
                                 size_hint_y=None, height=dp(20)))
        self.path_lbl = MDLabel(text=get_download_dir(), color=C_TEXT, font_style="Body2",
                                 size_hint_y=None, height=dp(40))
        root.add_widget(self.path_lbl)
        if ON_ANDROID:
            open_btn = MDFlatButton(text="Open folder", text_color=C_BLUE, size_hint_y=None, height=dp(36))
            open_btn.bind(on_release=lambda *_: open_downloads_folder())
            root.add_widget(open_btn)

        root.add_widget(MDLabel(text="Search results count", color=C_TEXT_DIM, font_style="Caption",
                                 size_hint_y=None, height=dp(20)))
        count_row = MDBoxLayout(size_hint_y=None, height=dp(40), spacing=dp(10))
        self.count_lbl = MDLabel(text=str(app.config.get("search_results_count", 15)), color=C_TEXT)
        minus = MDIconButton(icon="minus", theme_text_color="Custom", text_color=C_RED)
        plus = MDIconButton(icon="plus", theme_text_color="Custom", text_color=C_RED)
        minus.bind(on_release=lambda *_: self._adjust("search_results_count", -5, 5, 50))
        plus.bind(on_release=lambda *_: self._adjust("search_results_count", 5, 5, 50))
        count_row.add_widget(minus)
        count_row.add_widget(self.count_lbl)
        count_row.add_widget(plus)
        root.add_widget(count_row)

        root.add_widget(MDLabel(text="Max concurrent downloads", color=C_TEXT_DIM, font_style="Caption",
                                 size_hint_y=None, height=dp(20)))
        conc_row = MDBoxLayout(size_hint_y=None, height=dp(40), spacing=dp(10))
        self.conc_lbl = MDLabel(text=str(app.config.get("max_concurrent_downloads", 2)), color=C_TEXT)
        minus2 = MDIconButton(icon="minus", theme_text_color="Custom", text_color=C_RED)
        plus2 = MDIconButton(icon="plus", theme_text_color="Custom", text_color=C_RED)
        minus2.bind(on_release=lambda *_: self._adjust("max_concurrent_downloads", -1, 1, 5))
        plus2.bind(on_release=lambda *_: self._adjust("max_concurrent_downloads", 1, 1, 5))
        conc_row.add_widget(minus2)
        conc_row.add_widget(self.conc_lbl)
        conc_row.add_widget(plus2)
        root.add_widget(conc_row)

        note = (
            "Note: this build has no ffmpeg, so quality is capped at YouTube's "
            "single-file (progressive) streams, and audio downloads save as the "
            "original m4a/webm rather than mp3."
        )
        root.add_widget(MDLabel(text=note, color=C_TEXT_DIM, font_style="Caption", size_hint_y=None, height=dp(60)))

        root.add_widget(MDBoxLayout())  # spacer
        self.add_widget(root)

    def _adjust(self, key, delta, lo, hi):
        val = self.app.config.get(key, lo)
        val = max(lo, min(hi, val + delta))
        self.app.config[key] = val
        self.app.save_config()
        if key == "search_results_count":
            self.count_lbl.text = str(val)
        else:
            self.conc_lbl.text = str(val)
            self.app.resize_download_pool(val)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class YTDownloaderApp(MDApp):
    c_bg = C_BG
    c_card = C_CARD
    c_border = C_BORDER
    c_text = C_TEXT
    c_text_dim = C_TEXT_DIM
    c_red = C_RED
    c_blue = C_BLUE
    current_screen = "search"

    def build(self):
        Builder.load_string(KV)
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Red"
        Window.clearcolor = tuple(C_BG)

        self.config_data = self.load_config()
        self.config = self.config_data  # alias used throughout
        self.downloads = {}
        self.download_id_counter = 1
        self.downloads_lock = threading.Lock()
        self.load_download_history()

        self.download_executor = ThreadPoolExecutor(
            max_workers=self.config.get("max_concurrent_downloads", 2)
        )

        self.root_nav = RootNav()
        self.sm = self.root_nav.ids.sm
        self.sm.transition = NoTransition()

        self.search_screen = SearchScreen(self)
        self.downloads_screen = DownloadsScreen(self)
        self.settings_screen = SettingsScreen(self)
        self.sm.add_widget(self.search_screen)
        self.sm.add_widget(self.downloads_screen)
        self.sm.add_widget(self.settings_screen)
        self.sm.current = "search"

        if ON_ANDROID:
            try:
                from android.permissions import request_permissions, Permission
                request_permissions([Permission.INTERNET])
            except Exception:
                pass

        return self.root_nav

    def switch_screen(self, name):
        self.current_screen = name
        self.sm.current = name
        # Force nav icon colors to refresh (kv bindings re-evaluate on property change
        # via the bound on_touch_down branches above; this property itself triggers it)

    # ---------------- Config / history persistence ----------------
    def load_config(self):
        default = {
            "search_results_count": 15,
            "max_concurrent_downloads": 2,
        }
        try:
            if os.path.exists(CONFIG_FILE()):
                with open(CONFIG_FILE(), "r") as f:
                    default.update(json.load(f))
        except Exception:
            pass
        return default

    def save_config(self):
        try:
            with open(CONFIG_FILE(), "w") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            traceback.print_exc()

    def load_download_history(self):
        max_id = 0
        try:
            if os.path.exists(HISTORY_FILE()):
                with open(HISTORY_FILE(), "r", encoding="utf-8") as f:
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
            data = data[-300:]
            with open(HISTORY_FILE(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            traceback.print_exc()

    def resize_download_pool(self, n):
        try:
            self.download_executor.shutdown(wait=False)
        except Exception:
            pass
        self.download_executor = ThreadPoolExecutor(max_workers=n)

    # ---------------- Downloads ----------------
    def start_download(self, entry, quality):
        url = entry.get("webpage_url") or entry.get("url")
        if url and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={entry.get('id') or url}"
        if not url:
            Snackbar(text="Couldn't resolve a URL for that result.").open()
            return

        with self.downloads_lock:
            did = self.download_id_counter
            self.download_id_counter += 1
            record = {
                "id": did,
                "title": entry.get("title") or "Untitled",
                "url": url,
                "quality": quality,
                "status": "Queued",
                "status_text": "Queued",
                "progress": 0.0,
                "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "finished_at": None,
                "error": None,
                "save_path": None,
                "save_dir": get_download_dir(),
                "cancel_event": threading.Event(),
            }
            self.downloads[did] = record

        Snackbar(text=f"Queued: {record['title'][:40]}").open()
        if self.downloads_screen.parent is not None or True:
            self.downloads_screen.rebuild()
        self.download_executor.submit(self._run_download, did)

    def cancel_download(self, did):
        with self.downloads_lock:
            rec = self.downloads.get(did)
            if rec:
                rec["cancel_event"].set()

    def _run_download(self, did):
        with self.downloads_lock:
            rec = self.downloads.get(did)
        if not rec:
            return

        def hook(d):
            if rec["cancel_event"].is_set():
                raise yt_dlp.utils.DownloadError("Cancelled by user")
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                done = d.get("downloaded_bytes") or 0
                pct = (done / total) if total else 0.0
                rec["progress"] = pct
                speed = d.get("_speed_str", "").strip()
                eta = d.get("_eta_str", "").strip()
                rec["status_text"] = f"Downloading {int(pct*100)}%  {speed}  ETA {eta}".strip()
                rec["status"] = "Downloading"
                Clock.schedule_once(lambda dt: self._push_update(did))
            elif d.get("status") == "finished":
                rec["status_text"] = "Finalizing\u2026"
                Clock.schedule_once(lambda dt: self._push_update(did))

        fmt = ("bestaudio[ext=m4a]/bestaudio"
               if rec["quality"] == "audio"
               else "best[acodec!=none][vcodec!=none]/best")

        outtmpl = os.path.join(get_download_dir(), "%(title).150B [%(id)s].%(ext)s")
        ydl_opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [hook],
            "retries": 3,
        }

        rec["status"] = "Downloading"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(rec["url"], download=True)
                filename = ydl.prepare_filename(info)
            rec["status"] = "Completed"
            rec["status_text"] = "Completed"
            rec["progress"] = 1.0
            rec["save_path"] = filename
            rec["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            if rec["cancel_event"].is_set():
                rec["status"] = "Cancelled"
                rec["status_text"] = "Cancelled"
            else:
                rec["status"] = "Failed"
                rec["status_text"] = "Failed"
                rec["error"] = str(e)
            rec["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

        self.save_download_history()
        Clock.schedule_once(lambda dt: self._finish_update(did))

    def _push_update(self, did):
        with self.downloads_lock:
            rec = self.downloads.get(did)
        if rec:
            self.downloads_screen.refresh_active(did, rec)

    def _finish_update(self, did):
        self.downloads_screen.rebuild()

    def clear_download_history(self):
        with self.downloads_lock:
            for d in [d for d, r in self.downloads.items() if r["status"] in FINISHED_STATUSES]:
                del self.downloads[d]
        self.save_download_history()
        self.downloads_screen.rebuild()

    def remove_history_entry(self, did):
        with self.downloads_lock:
            self.downloads.pop(did, None)
        self.save_download_history()
        self.downloads_screen.rebuild()

    def on_stop(self):
        try:
            with self.downloads_lock:
                for rec in self.downloads.values():
                    if rec["status"] in ACTIVE_STATUSES:
                        rec["cancel_event"].set()
            self.download_executor.shutdown(wait=False)
        except Exception:
            pass
        self.save_download_history()
        self.save_config()


if __name__ == "__main__":
    YTDownloaderApp().run()
