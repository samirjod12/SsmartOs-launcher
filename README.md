# SmartOS (Windows launcher)

1. Install Python 3.9+ (tick "Add to PATH") and VLC: https://www.videolan.org/vlc/
2. Optional, embeds VLC inside the app: `pip install python-vlc`
   (use the same 64/32-bit build for Python and VLC)
3. Run: `pythonw smartos.py`  (or `python smartos.py`)

Keys: arrows move, Enter opens/plays, Esc goes back, F11 fullscreen.
Live TV: Left/Right = previous/next channel.

Settings: playlist path or URL (.m3u), VLC path, App Loader
(add programs/websites; they show as tiles on Home), desktop shortcut.

Build a single .exe: `pip install pyinstaller` then
`pyinstaller --onefile --noconsole smartos.py`

Shinchan TV: the site blocked automated access when I tried it, so the
episode list could not be tested live. If "Fetch" fails, use "Open site",
save the page (Ctrl+S, HTML only) and click "Load saved page".
