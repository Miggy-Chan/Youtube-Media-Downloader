[app]
title = YouTube Media Pro
package.name = ytmediapro
package.domain = org.example

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 1.0.0

requirements = python3,kivy==2.3.0,kivymd==1.2.0,yt-dlp,certifi,pyjnius,plyer,android,chardet,brotli,websockets

orientation = portrait
fullscreen = 0

# --- Android target range: 11 (API 30) through current ---
android.minapi = 30
android.api = 34
android.ndk_api = 30
android.archs = arm64-v8a,armeabi-v7a

android.permissions = INTERNET

# Scoped-storage-friendly: we intentionally do NOT request
# READ/WRITE_EXTERNAL_STORAGE or MANAGE_EXTERNAL_STORAGE. The app writes only to
# its own app-specific external directory, which needs no runtime permission on
# Android 10+. See README.md if you want public Downloads-folder access instead.

android.allow_backup = True
android.gradle_dependencies =

p4a.branch = master

[buildozer]
log_level = 2
warn_on_root = 1
