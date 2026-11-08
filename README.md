# SmartOS (Windows launcher)

TV-OS style launcher: Live TV (IPTV + VLC), YouTube TV and websites in an
in-app browser with ad blocker, Shinchan TV (S1-S15 x E1-E52) with background
stream extraction and in-app browser fallback, Settings with App Loader.

## Run from source
1. Python 3.10+ (64-bit) and VLC (64-bit): https://www.videolan.org/vlc/
2. `pip install -r requirements.txt`
3. `pythonw smartos.py`

## Get the Windows build
Open the **Actions** tab, pick the latest run, download `SmartOS-windows`,
unzip, run `SmartOS/SmartOS.exe`. VLC must still be installed.

## Keys
Arrows move, Enter opens/plays, Esc back, F11 fullscreen, F video fullscreen,
Live TV: Left/Right previous/next channel, Space pause.

## Notes
- Extra ad domains: one per line in `%APPDATA%\SmartOS\adblock_extra.txt`.
- If a channel fails, the player shows the reason (HTTP status / unreachable).
  Some streams need a Referer or User-Agent: set them in Settings, or add
  `#EXTVLCOPT:http-referrer=...` lines to the playlist.
- Shinchan limits (seasons, episodes per season, per-season overrides) are in Settings.
