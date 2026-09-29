import flet as ft
from pathlib import Path
import hashlib
import random
import json
import asyncio
import sqlite3
import shutil
import os
import time
import threading
import queue
import re
import requests
import pylast
import yt_dlp
from datetime import datetime
from mutagen import File as MutagenFile
from mutagen.id3 import (
    APIC,
    ID3,
    ID3NoHeaderError,
    TALB,
    TIT2,
    TPE1,
)

# ============================================================
#                        НАСТРОЙКИ
# ============================================================
CONFIG_DIR = Path.home() / ".config" / "musicapp"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
LEGACY_MUSIC_DIR = Path("/home/didearaw/Музыка")
DEFAULT_MUSIC_DIR = Path.home() / "Music"

CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_music_dir():
    """Возвращает сохранённую папку музыки или подходящий путь по умолчанию."""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            saved_path = data.get("music_dir")
            if saved_path:
                return Path(saved_path).expanduser()
    except Exception:
        pass

    # Сохраняем совместимость со старой установкой приложения.
    if LEGACY_MUSIC_DIR.exists():
        return LEGACY_MUSIC_DIR
    return DEFAULT_MUSIC_DIR


def save_music_dir(path):
    try:
        SETTINGS_FILE.write_text(
            json.dumps({"music_dir": str(path)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


MUSIC_DIR = load_music_dir()
COVERS_DIR = Path.home() / ".cache" / "musicapp" / "covers"
ARTISTS_DIR = Path.home() / ".cache" / "musicapp" / "artists"
PLAYLISTS_FILE = CONFIG_DIR / "playlists.json"
STATS_DB = CONFIG_DIR / "stats.db"

LASTFM_API_KEY = os.getenv("MUSICAPP_LASTFM_API_KEY", "393f13eb103a86262543730159e64a81")
LASTFM_API_SECRET = os.getenv("MUSICAPP_LASTFM_API_SECRET", "2d92b111ec1ac877ceceda8741fafda4")

COVERS_DIR.mkdir(parents=True, exist_ok=True)
ARTISTS_DIR.mkdir(parents=True, exist_ok=True)
PLAYLISTS_FILE.parent.mkdir(parents=True, exist_ok=True)
STATS_DB.parent.mkdir(parents=True, exist_ok=True)
MUSIC_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".opus"}

_metadata_cache = {}

UNKNOWN_ARTIST = "Неизвестный исполнитель"
UNKNOWN_ALBUM = "Без альбома"

GREEN = "#1DB954"
INACTIVE = "white"
PANEL_BG = "#181818"
BACKDROP = "#00000099"
PANEL_ANIM_MS = 380
BACKDROP_ANIM_MS = 320

SKIP_THRESHOLD_SEC = 30

RECENTLY_ADDED_LIMIT = 100
COVER_MISS_TTL_SEC = 24 * 60 * 60
LOCAL_INDEX_TTL_SEC = 60

# ============================================================
#                        БАЗА ДАННЫХ
# ============================================================
def init_stats_db():
    conn = sqlite3.connect(str(STATS_DB))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            title TEXT,
            artist TEXT,
            album TEXT,
            timestamp REAL NOT NULL,
            duration_ms INTEGER DEFAULT 0,
            listened_ms INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_path ON plays(path)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON plays(timestamp)")
    conn.commit()
    conn.close()


def log_play_start(path_str, title, artist, album):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO plays (path, title, artist, album, timestamp) VALUES (?,?,?,?,?)",
            (path_str, title, artist, album, time.time()),
        )
        play_id = cur.lastrowid
        conn.commit()
        conn.close()
        return play_id
    except Exception as e:
        print(f"Ошибка log_play_start: {e}")
        return None


def update_play_finish(play_id, duration_ms, listened_ms, skipped):
    if play_id is None:
        return
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute(
            "UPDATE plays SET duration_ms=?, listened_ms=?, skipped=? WHERE id=?",
            (duration_ms, listened_ms, 1 if skipped else 0, play_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка update_play_finish: {e}")


def get_top_tracks(limit=10):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT path, title, artist, COUNT(*) as cnt,
                   SUM(listened_ms) as total_ms
            FROM plays
            WHERE skipped = 0
            GROUP BY path
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def get_top_artists(limit=10):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT artist, COUNT(*) as cnt, SUM(listened_ms) as total_ms
            FROM plays
            WHERE skipped = 0 AND artist != ''
            GROUP BY artist
            ORDER BY cnt DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def get_top_albums(limit=10):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT album, artist, COUNT(*) as cnt
            FROM plays
            WHERE skipped = 0 AND album != '' AND album != ?
            GROUP BY album, artist
            ORDER BY cnt DESC
            LIMIT ?
        """, (UNKNOWN_ALBUM, limit))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def get_recent_plays(limit=20):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT path, title, artist, timestamp, skipped
            FROM plays
            ORDER BY timestamp DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


def get_summary():
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM plays WHERE skipped = 0")
        total_plays = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(DISTINCT path) FROM plays WHERE skipped = 0")
        unique_tracks = cur.fetchone()[0] or 0
        cur.execute("SELECT COALESCE(SUM(listened_ms), 0) FROM plays WHERE skipped = 0")
        total_ms = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(DISTINCT artist) FROM plays WHERE skipped = 0 AND artist != ''")
        unique_artists = cur.fetchone()[0] or 0
        conn.close()
        return {
            "total_plays": total_plays,
            "unique_tracks": unique_tracks,
            "total_ms": total_ms,
            "unique_artists": unique_artists,
        }
    except Exception:
        return {"total_plays": 0, "unique_tracks": 0, "total_ms": 0, "unique_artists": 0}


def get_all_track_play_counts():
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("""
            SELECT path, COUNT(*) FROM plays
            WHERE skipped = 0
            GROUP BY path
        """)
        rows = cur.fetchall()
        conn.close()
        return {str(p): int(c) for p, c in rows}
    except Exception:
        return {}


def reset_stats():
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("DELETE FROM plays")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка reset_stats: {e}")


init_stats_db()
def init_likes_db():
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS likes (
                path TEXT PRIMARY KEY,
                liked INTEGER NOT NULL DEFAULT 1,
                ts REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка init_likes_db: {e}")


_LIKES_CACHE = None


def _load_likes_cache():
    global _LIKES_CACHE
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("SELECT path FROM likes WHERE liked = 1")
        _LIKES_CACHE = {row[0] for row in cur.fetchall()}
        conn.close()
    except Exception:
        _LIKES_CACHE = set()
    return _LIKES_CACHE


def is_liked(path_str):
    cache = _LIKES_CACHE if _LIKES_CACHE is not None else _load_likes_cache()
    return path_str in cache
def get_liked_paths():
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        cur.execute("SELECT path FROM likes WHERE liked = 1 ORDER BY ts DESC")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def set_like(path_str, liked):
    try:
        conn = sqlite3.connect(str(STATS_DB))
        cur = conn.cursor()
        if liked:
            cur.execute(
                "INSERT INTO likes (path, liked, ts) VALUES (?, 1, ?) "
                "ON CONFLICT(path) DO UPDATE SET liked=1, ts=excluded.ts",
                (path_str, time.time()),
            )
        else:
            cur.execute("DELETE FROM likes WHERE path = ?", (path_str,))
        conn.commit()
        conn.close()
        if _LIKES_CACHE is not None:
            if liked:
                _LIKES_CACHE.add(path_str)
            else:
                _LIKES_CACHE.discard(path_str)
        return True
    except Exception as e:
        print(f"Ошибка set_like: {e}")
        return False


init_likes_db()



# ============================================================
#                        LAST.FM
# ============================================================
lastfm_network = None
try:
    lastfm_network = pylast.LastFMNetwork(
        api_key=LASTFM_API_KEY,
        api_secret=LASTFM_API_SECRET,
    )
    print("Last.fm: сеть инициализирована.")
except Exception as e:
    print(f"Last.fm: ошибка инициализации: {e}")


def get_similar_tracks(artist, title, limit=10):
    if not lastfm_network:
        return []
    try:
        return lastfm_network.get_track(artist, title).get_similar(limit=limit)
    except Exception as e:
        print(f"Last.fm: ошибка похожих треков: {e}")
        return []


def get_similar_artists(artist, limit=10):
    if not lastfm_network:
        return []
    try:
        return lastfm_network.get_artist(artist).get_similar(limit=limit)
    except Exception as e:
        print(f"Last.fm: ошибка похожих исполнителей: {e}")
        return []


# ============================================================
#                ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def artist_color(name):
    h = int(hashlib.md5(name.lower().encode()).hexdigest(), 16)
    colors = ["#1DB954", "#E91E63", "#2196F3", "#FF9800",
              "#9C27B0", "#00BCD4", "#F44336", "#795548",
              "#3F51B5", "#8BC34A"]
    return colors[h % len(colors)]


def load_playlists():
    if PLAYLISTS_FILE.exists():
        try:
            data = json.loads(PLAYLISTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): [str(p) for p in v] for k, v in data.items()}
        except Exception:
            pass
    return {}


def save_playlists(playlists):
    try:
        PLAYLISTS_FILE.write_text(
            json.dumps(playlists, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"Не удалось сохранить плейлисты: {e}")


def find_audio_files(folder):
    if not folder.exists():
        return []
    files = []
    for f in folder.rglob("*"):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
            files.append(f)
    files.sort(key=lambda p: p.name.lower())
    return files


def find_recently_added_files(folder, limit=RECENTLY_ADDED_LIMIT):
    files = find_audio_files(folder)

    def get_mtime(f):
        try:
            return f.stat().st_mtime
        except Exception:
            return 0

    files.sort(key=get_mtime, reverse=True)
    return files[:limit]


def get_cover_from_file(path):
    try:
        audio = MutagenFile(path)
        if audio is None:
            return None
        tags = audio.tags
        cover_data = None
        if tags is not None and hasattr(tags, "getall"):
            apics = tags.getall("APIC")
            for apic in apics:
                if getattr(apic, "data", None):
                    cover_data = apic.data
                    break
        if cover_data is None and tags is not None and "covr" in tags:
            cover_data = bytes(tags["covr"][0])
        if cover_data is None and hasattr(audio, "pictures") and audio.pictures:
            for picture in audio.pictures:
                if getattr(picture, "data", None):
                    cover_data = picture.data
                    break
        if not cover_data:
            return None

        # Хэш содержимого не даёт показать старую обложку после повторной
        # обработки того же файла.
        path_key = hashlib.md5(str(path).encode()).hexdigest()
        data_key = hashlib.md5(cover_data).hexdigest()
        out = COVERS_DIR / f"{path_key}_{data_key}.jpg"
        if not out.exists():
            out.write_bytes(cover_data)
        return str(out)
    except Exception:
        return None


def get_cover_from_sidecar(path):
    """Ищет обложку рядом с треком или в папке альбома."""
    path = Path(path)
    stems = [
        path.stem,
        "cover",
        "folder",
        "front",
        "albumart",
        "album-art",
    ]
    extensions = (".jpg", ".jpeg", ".png", ".webp")

    for stem in stems:
        for extension in extensions:
            candidate = path.parent / f"{stem}{extension}"
            if candidate == path:
                continue
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return str(candidate)
            except Exception:
                continue

    # На Linux имя файла чувствительно к регистру: Cover.JPG и cover.jpg
    # должны обрабатываться одинаково.
    wanted_stems = {stem.lower() for stem in stems}
    try:
        for candidate in path.parent.iterdir():
            if (
                candidate.is_file()
                and candidate.suffix.lower() in extensions
                and candidate.stem.lower() in wanted_stems
                and candidate != path
                and candidate.stat().st_size > 0
            ):
                return str(candidate)
    except Exception:
        pass
    return None


def find_mp3_files_without_cover(folder, stop_event=None):
    """Возвращает только MP3 без встроенной или локальной обложки."""
    files = []
    try:
        for path in folder.rglob("*"):
            if stop_event is not None and stop_event.is_set():
                break
            if path.is_file() and path.suffix.lower() == ".mp3":
                files.append(path)
    except Exception:
        pass

    files.sort(key=lambda p: p.name.lower())

    result = []
    for path in files:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            has_cover = bool(
                get_cover_from_file(path) or get_cover_from_sidecar(path)
            )
        except Exception:
            has_cover = False
        if not has_cover:
            result.append(path)
    return result

def _miss_marker_is_fresh(marker):
    try:
        return time.time() - marker.stat().st_mtime < COVER_MISS_TTL_SEC
    except Exception:
        return False


def get_cover_from_itunes(artist, title, album="", force=False):
    if not artist or not title:
        return None

    cache_key = hashlib.md5(
        f"{artist}|{title}|{album}".lower().encode()
    ).hexdigest()
    cached_cover = COVERS_DIR / f"it_{cache_key}.jpg"
    miss_marker = COVERS_DIR / f"it_{cache_key}.none"

    if cached_cover.exists():
        return str(cached_cover)
    if miss_marker.exists():
        if not force and _miss_marker_is_fresh(miss_marker):
            return None
        try:
            miss_marker.unlink()
        except Exception:
            pass

    try:
        url = "https://itunes.apple.com/search"
        query = f"{artist} {title}"
        if album:
            query = f"{query} {album}"
        params = {"term": query, "entity": "song", "limit": 10}
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code != 200:
            miss_marker.touch()
            return None
        data = resp.json()
        results = data.get("results", [])
        if not results:
            miss_marker.touch()
            return None
        for item in results:
            art_url = item.get("artworkUrl100")
            if not art_url:
                continue
            art_url_big = art_url.replace("100x100bb", "600x600bb")
            try:
                img_resp = requests.get(art_url_big, timeout=8)
                if img_resp.status_code == 200 and img_resp.content:
                    cached_cover.write_bytes(img_resp.content)
                    return str(cached_cover)
            except requests.RequestException:
                continue
        miss_marker.touch()
        return None
    except Exception:
        return None


def get_cover_from_deezer(artist, title, album="", force=False):
    """Резервный поиск обложки через Deezer."""
    if not artist or not title:
        return None

    cache_key = hashlib.md5(
        f"{artist}|{title}|{album}".lower().encode()
    ).hexdigest()
    cached_cover = COVERS_DIR / f"dz_{cache_key}.jpg"
    miss_marker = COVERS_DIR / f"dz_{cache_key}.none"

    if cached_cover.exists():
        return str(cached_cover)
    if miss_marker.exists():
        if not force and _miss_marker_is_fresh(miss_marker):
            return None
        try:
            miss_marker.unlink()
        except Exception:
            pass

    try:
        query = f"{artist} {title}"
        if album:
            query = f"{query} {album}"
        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": query, "limit": 10},
            timeout=8,
        )
        if resp.status_code != 200:
            miss_marker.touch()
            return None

        results = resp.json().get("data", [])
        for item in results:
            album_data = item.get("album") or {}
            cover_url = (
                album_data.get("cover_xl")
                or album_data.get("cover_big")
                or album_data.get("cover_medium")
            )
            if not cover_url:
                continue
            try:
                image = requests.get(cover_url, timeout=8)
                if image.status_code == 200 and image.content:
                    cached_cover.write_bytes(image.content)
                    return str(cached_cover)
            except requests.RequestException:
                continue

        miss_marker.touch()
        return None
    except Exception:
        return None


def get_artist_photo(name):
    if not name:
        return None

    cache_key = hashlib.md5(f"artist_{name.lower()}".encode()).hexdigest()
    cached = ARTISTS_DIR / f"{cache_key}.jpg"
    miss = ARTISTS_DIR / f"{cache_key}.none"

    if cached.exists():
        return str(cached)

    if miss.exists():
        if _miss_marker_is_fresh(miss):
            return None
        try:
            miss.unlink()
        except Exception:
            pass

    try:
        url = "https://api.deezer.com/search/artist"
        params = {"q": name, "limit": 3}
        resp = requests.get(url, params=params, timeout=8)
        if resp.status_code != 200:
            miss.touch()
            return None

        data = resp.json()
        results = data.get("data", [])
        if not results:
            miss.touch()
            return None

        chosen = None
        for r in results:
            if r.get("name", "").lower() == name.lower():
                chosen = r
                break

        if chosen is None:
            chosen = results[0]

        pic_url = (
            chosen.get("picture_xl")
            or chosen.get("picture_big")
            or chosen.get("picture_medium")
        )
        if not pic_url:
            miss.touch()
            return None

        img = requests.get(pic_url, timeout=8)
        if img.status_code == 200 and img.content:
            cached.write_bytes(img.content)
            return str(cached)

        miss.touch()
        return None
    except Exception:
        return None

def get_cover_from_cache_only(artist, title, album=""):
    """Быстрая проверка кэша обложек без сети."""
    if not artist or not title:
        return None
    cache_key = hashlib.md5(
        f"{artist}|{title}|{album}".lower().encode()
    ).hexdigest()
    for prefix in ("it_", "dz_"):
        cached = COVERS_DIR / f"{prefix}{cache_key}.jpg"
        if cached.exists():
            return str(cached)
    return None


def get_artist_photo_cache_only(name):
    """Быстрая проверка кэша фото исполнителя без сети."""
    if not name:
        return None
    cache_key = hashlib.md5(f"artist_{name.lower()}".encode()).hexdigest()
    cached = ARTISTS_DIR / f"{cache_key}.jpg"
    if cached.exists():
        return str(cached)
    return None


def artist_photo_key(name):
    return f"artist:{name.lower()}"


def remote_cover_key(artist, title):
    return f"remote:{artist.lower()}|{title.lower()}"


_cover_subscribers = {}
_cover_queue = queue.Queue()
_cover_ready = queue.Queue()
_cover_in_flight = set()
_cover_worker_started = False


def subscribe_cover(key, callback):
    _cover_subscribers.setdefault(key, []).append(callback)


def clear_cover_subscribers():
    _cover_subscribers.clear()


def take_ready_covers():
    items = []
    while True:
        try:
            items.append(_cover_ready.get_nowait())
        except queue.Empty:
            break
    return items


def request_cover_search(path):
    key = str(path)
    if key in _cover_in_flight:
        return
    _cover_in_flight.add(key)
    _cover_queue.put(("path", key))


def request_remote_cover(artist, title):
    key = remote_cover_key(artist, title)
    if key in _cover_in_flight:
        return
    _cover_in_flight.add(key)
    _cover_queue.put(("remote", artist, title))


def request_artist_photo(name):
    key = artist_photo_key(name)
    if key in _cover_in_flight:
        return
    _cover_in_flight.add(key)
    _cover_queue.put(("artist", name))


def _apply_cover_to_placeholder(container, cover_path, size, radius):
    container.content = ft.Image(
        src=cover_path, width=size, height=size,
        fit=ft.ImageFit.COVER, border_radius=radius,
    )


def make_cover_placeholder(keys, size, radius, icon=None):
    placeholder = ft.Container(
        width=size, height=size, bgcolor="#2a2a2a",
        border_radius=radius, alignment=ft.alignment.center,
        content=ft.Icon(icon or ft.Icons.MUSIC_NOTE, color="grey",
                        size=int(size * 0.45)),
    )

    def apply(cover_path):
        _apply_cover_to_placeholder(placeholder, cover_path, size, radius)

    for key in keys:
        subscribe_cover(key, apply)
    return placeholder


def _cover_worker_loop():
    while True:
        job = _cover_queue.get()
        try:
            kind = job[0]
            if kind == "path":
                path_str = job[1]
                meta = _metadata_cache.get(path_str)
                if not meta or meta.get("cover"):
                    continue
                artist = meta.get("artist") or ""
                title = meta.get("title") or ""
                album = meta.get("album") or ""
                cover = get_cover_from_itunes(artist, title, album)
                if cover is None:
                    cover = get_cover_from_deezer(artist, title, album)
                if cover:
                    meta["cover"] = cover
                    _cover_ready.put((path_str, cover))
            elif kind == "remote":
                _, artist, title = job
                cover = get_cover_from_itunes(artist, title)
                if cover is None:
                    cover = get_cover_from_deezer(artist, title)
                if cover:
                    _cover_ready.put((remote_cover_key(artist, title), cover))
            elif kind == "artist":
                name = job[1]
                photo = get_artist_photo(name)
                if photo:
                    _cover_ready.put((artist_photo_key(name), photo))
        except Exception:
            pass


def _cover_scanner_loop():
    while True:
        time.sleep(2.0)
        try:
            for path_str, meta in list(_metadata_cache.items()):
                if (meta.get("cover") is None
                        and meta.get("artist") and meta.get("title")):
                    request_cover_search(path_str)
                if meta.get("artist"):
                    request_artist_photo(meta["artist"])
        except Exception:
            pass


def start_cover_worker():
    global _cover_worker_started
    if _cover_worker_started:
        return
    _cover_worker_started = True
    threading.Thread(target=_cover_worker_loop, daemon=True).start()
    threading.Thread(target=_cover_scanner_loop, daemon=True).start()


def read_metadata(path):
    path_str = str(path)
    if path_str in _metadata_cache:
        return _metadata_cache[path_str]
    result = {"title": path.stem, "artist": "", "album": "", "cover": None}
    try:
        audio = MutagenFile(path)
        tags = audio.tags if audio is not None and audio.tags is not None else {}

        def get_tag(*keys):
            for k in keys:
                if k in tags:
                    v = tags[k]
                    if isinstance(v, list) and v:
                        return str(v[0])
                    return str(v)
            return None

        title = (get_tag("TIT2", "title", "\xa9nam") or "").strip()
        artist = (get_tag("TPE1", "artist", "\xa9ART", "albumartist") or "").strip()
        album = (get_tag("TALB", "album", "\xa9alb") or "").strip()
        if title:
            result["title"] = title
        if artist:
            result["artist"] = artist
        if album:
            result["album"] = album

        # У скачанных или старых файлов теги могут отсутствовать.
        # В этом случае используем формат "Исполнитель - Название".
        filename_artist, filename_title = parse_title(path.stem)
        if not artist and filename_artist:
            result["artist"] = filename_artist
        if not title and filename_title:
            result["title"] = filename_title

        result["cover"] = get_cover_from_file(path)
        if result["cover"] is None:
            result["cover"] = get_cover_from_sidecar(path)
        if result["cover"] is None and result["artist"]:
            result["cover"] = get_cover_from_cache_only(
                result["artist"], result["title"], result["album"]
            )
    except Exception as e:
        print(f"Ошибка чтения метаданных {path.name}: {e}")
    _metadata_cache[path_str] = result
    return result


def parse_title(filename_stem):
    """Пытается получить исполнителя и название из имени файла."""
    text = str(filename_stem).strip()
    if not text:
        return "", ""

    # Наиболее распространённые варианты: Artist - Title и Artist — Title.
    parts = None
    for separator in (" — ", " – ", " - ", "—", "–"):
        if separator in text:
            candidate = text.split(separator, 1)
            if len(candidate) == 2:
                parts = candidate
                break

    if parts:
        artist = parts[0].strip(" _.-")
        title = parts[1].strip(" _.-")
        if artist and title:
            return artist, title

    # Если в имени нет разделителя, считаем всё имя названием.
    return "", text


def extract_artist_title_from_file(path: Path) -> tuple:
    """Читает теги MP3, а при их отсутствии разбирает имя файла."""
    tag_artist = ""
    tag_title = ""

    try:
        audio = MutagenFile(path)
        if audio is not None and audio.tags is not None:
            tags = audio.tags

            def get_tag(*keys):
                for key in keys:
                    if key in tags:
                        value = tags[key]
                        if isinstance(value, list) and value:
                            return str(value[0]).strip()
                        return str(value).strip()
                return ""

            tag_title = get_tag("TIT2", "title", "\xa9nam")
            tag_artist = get_tag("TPE1", "artist", "\xa9ART", "albumartist")
    except Exception:
        pass

    if tag_title and tag_artist:
        return tag_artist, tag_title

    file_artist, file_title = parse_title(path.stem)

    if tag_title:
        return file_artist or tag_artist, tag_title

    if file_title:
        return file_artist or tag_artist, file_title

    return tag_artist, tag_title or path.stem

def download_cover_bytes(artist: str, title: str):
    """Ищет обложку в нескольких источниках и возвращает bytes."""
    if not artist or not title:
        return None
    try:
        # Для кнопки обработки разрешаем повторную попытку после прошлого
        # неудачного поиска.
        cover_path = get_cover_from_itunes(artist, title, force=True)
        if not cover_path:
            cover_path = get_cover_from_deezer(
                artist, title, force=True
            )
        if cover_path:
            cover_file = Path(cover_path)
            if cover_file.exists():
                return cover_file.read_bytes()
    except Exception:
        pass
    return None


def set_mp3_metadata(
    file_path: Path,
    title: str,
    artist: str,
    album: str = "",
    cover_bytes: bytes = None,
) -> bool:
    """Записывает основные ID3-теги и обложку в MP3."""
    if not file_path.exists() or file_path.suffix.lower() != ".mp3":
        return False

    try:
        try:
            tags = ID3(str(file_path))
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags.delall("TIT2")
        if artist:
            tags.delall("TPE1")
        if album:
            tags.delall("TALB")
        if cover_bytes:
            tags.delall("APIC")

        if title:
            tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if album:
            tags.add(TALB(encoding=3, text=album))
        if cover_bytes:
            tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=cover_bytes,
                )
            )

        tags.save(str(file_path))
        return True
    except Exception as e:
        print(f"Ошибка записи метаданных {file_path.name}: {e}")
        return False

def process_one_file(path: Path, stop_event=None) -> tuple:
    """Обрабатывает один MP3 и возвращает статус и описание результата."""
    if stop_event is not None and stop_event.is_set():
        return "cancelled", "обработка остановлена"
    if not path.exists() or path.suffix.lower() != ".mp3":
        return "skip", "не MP3"

    artist, title = extract_artist_title_from_file(path)
    if not title:
        return "skip", "не удалось определить название"

    cover_bytes = None
    local_cover_path = get_cover_from_file(path) or get_cover_from_sidecar(path)
    if local_cover_path:
        try:
            cover_bytes = Path(local_cover_path).read_bytes()
        except Exception:
            cover_bytes = None

    if stop_event is not None and stop_event.is_set():
        return "cancelled", "обработка остановлена"

    if cover_bytes is None:
        cover_bytes = download_cover_bytes(artist, title)

    if stop_event is not None and stop_event.is_set():
        return "cancelled", "обработка остановлена"

    ok = set_mp3_metadata(
        path,
        title=title,
        artist=artist,
        cover_bytes=cover_bytes,
    )
    if not ok:
        return "skip", "ошибка записи метаданных"

    label = f"{artist} — {title}" if artist else title
    if cover_bytes:
        return "ok", label
    return "no_cover", label


def process_all_files(on_progress, on_log, on_done, stop_event=None):
    """Обрабатывает только MP3 без обложек."""
    files = find_mp3_files_without_cover(MUSIC_DIR, stop_event)
    if stop_event is not None and stop_event.is_set():
        on_log("⏹ Обработка остановлена до начала.", "#ffa94d")
        on_done(0, 0, 0, True)
        return

    total = len(files)

    if total == 0:
        on_log("Все MP3 уже имеют встроенную или локальную обложку.", GREEN)
        on_done(0, 0, 0, False)
        return

    on_log(f"Найдено MP3 без обложки: {total}")
    ok_count = 0
    no_cover_count = 0
    skipped_count = 0
    cancelled = False

    for index, path in enumerate(files, start=1):
        if stop_event is not None and stop_event.is_set():
            cancelled = True
            break
        try:
            status, info = process_one_file(path, stop_event)
        except Exception as e:
            status, info = "skip", str(e)

        if status == "cancelled":
            cancelled = True
            break
        elif status == "ok":
            ok_count += 1
            on_log(f"Готово: {info}", GREEN)
        elif status == "no_cover":
            no_cover_count += 1
            on_log(f"Без обложки: {info}", "#ffa94d")
        else:
            skipped_count += 1
            on_log(f"Пропущен: {path.name} ({info})", "grey")

        on_progress(index, total, path.name)

    if cancelled:
        on_log(
            f"⏹ Остановлено. Обработано: {ok_count}; "
            f"без обложки: {no_cover_count}; пропущено: {skipped_count}",
            "#ffa94d",
        )
    else:
        on_log("━" * 30)
        on_log(
            f"Готово! Обработано: {ok_count}; "
            f"без обложки: {no_cover_count}; пропущено: {skipped_count}",
            GREEN,
        )
    on_done(ok_count, no_cover_count, skipped_count, cancelled)


def format_time(ms):
    if not ms or ms <= 0:
        return "0:00"
    total_sec = int(ms // 1000)
    m = total_sec // 60
    s = total_sec % 60
    return f"{m}:{s:02d}"


def format_duration_human(ms):
    if not ms or ms <= 0:
        return "0 сек"
    total_sec = int(ms // 1000)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    if hours > 0:
        return f"{hours} ч {minutes} мин"
    elif minutes > 0:
        return f"{minutes} мин"
    return f"{seconds} сек"


def format_relative_time(ts):
    diff = time.time() - ts
    if diff < 60:
        return "только что"
    elif diff < 3600:
        return f"{int(diff // 60)} мин назад"
    elif diff < 86400:
        return f"{int(diff // 3600)} ч назад"
    elif diff < 86400 * 2:
        return "вчера"
    elif diff < 86400 * 7:
        return f"{int(diff // 86400)} дн назад"
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%d.%m.%Y")


def human_size(n):
    try:
        n = int(n)
    except Exception:
        return "0 Б"
    if n < 1024:
        return f"{n} Б"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} КБ"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} МБ"
    return f"{n / 1024 ** 3:.2f} ГБ"


def group_by_album(files):
    groups = {}
    for f in files:
        meta = read_metadata(f)
        album = meta["album"] or UNKNOWN_ALBUM
        artist = meta["artist"] or UNKNOWN_ARTIST
        key = (album.lower(), artist.lower())
        if key not in groups:
            groups[key] = {"album": album, "artist": artist, "tracks": [], "cover": None}
        groups[key]["tracks"].append(f)
        if groups[key]["cover"] is None and meta["cover"]:
            groups[key]["cover"] = meta["cover"]
    return groups


def group_by_artist(files):
    groups = {}
    for f in files:
        meta = read_metadata(f)
        artist = meta["artist"] or UNKNOWN_ARTIST
        album = meta["album"] or UNKNOWN_ALBUM
        key = artist.lower()
        if key not in groups:
            groups[key] = {"artist": artist, "tracks": [], "albums": set()}
        groups[key]["tracks"].append(f)
        groups[key]["albums"].add(album)
    return groups


# ============================================================
#             ФУНКЦИИ СИНХРОНИЗАЦИИ С ФЛЕШКОЙ
# ============================================================
def list_removable_drives():
    user = os.environ.get("USER", "")
    candidates = [
        Path(f"/media/{user}"),
        Path(f"/run/media/{user}"),
        Path("/media"),
    ]
    drives = []
    seen = set()
    for base in candidates:
        if not base.exists() or not base.is_dir():
            continue
        try:
            for entry in base.iterdir():
                if not entry.is_dir():
                    continue
                if entry.name.startswith("."):
                    continue
                try:
                    resolved = str(entry.resolve())
                except Exception:
                    continue
                if resolved in seen:
                    continue
                if resolved in ("/", "/home", "/boot", "/usr", "/var"):
                    continue
                seen.add(resolved)
                drives.append(entry)
        except Exception:
            continue
    return drives


def find_audio_files_on_drive(drive):
    result = []
    try:
        for f in drive.rglob("*"):
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
                result.append(f)
    except Exception:
        pass
    return result


def sanitize_filename(name):
    if not name:
        return "Unknown"
    bad = '<>:"/\\|?*'
    for ch in bad:
        name = name.replace(ch, "_")
    name = "".join(c for c in name if ord(c) >= 32)
    name = name.strip().rstrip(".")
    if not name:
        name = "Unknown"
    return name[:140]


def get_track_base_name(meta, path):
    artist = sanitize_filename(meta.get("artist") or "Unknown Artist")
    title = sanitize_filename(meta.get("title") or path.stem)
    ext = path.suffix.lower() or ".mp3"
    return f"{artist} - {title}{ext}"


def load_sync_manifest(drive):
    f = drive / ".musicapp_sync.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_sync_manifest(drive, manifest):
    f = drive / ".musicapp_sync.json"
    try:
        f.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception as e:
        print(f"Ошибка сохранения манифеста: {e}")


def scan_library():
    result = {}
    files = find_audio_files(MUSIC_DIR)
    play_counts = get_all_track_play_counts()

    entries = []
    for f in files:
        meta = read_metadata(f)
        count = play_counts.get(str(f), 0)
        entries.append((f, meta, count))

    entries.sort(key=lambda x: (
        -x[2],
        (x[1].get("artist") or "").lower(),
        (x[1].get("title") or "").lower(),
        str(x[0]).lower(),
    ))

    used_names_lower = set()
    total = len(entries)
    digits = max(3, len(str(total)))

    for idx, (f, meta, count) in enumerate(entries, start=1):
        base_name = get_track_base_name(meta, f)
        numbered = f"{idx:0{digits}d} - {base_name}"
        key_lower = numbered.lower()

        if key_lower in used_names_lower:
            stem = Path(numbered).stem
            ext = Path(numbered).suffix
            counter = 2
            while f"{stem} ({counter}){ext}".lower() in used_names_lower:
                counter += 1
            numbered = f"{stem} ({counter}){ext}"
            key_lower = numbered.lower()

        used_names_lower.add(key_lower)

        try:
            st = f.stat()
            result[numbered] = (f, st.st_size, int(st.st_mtime))
        except Exception:
            continue

    return result


def compute_sync_plan(drive):
    library = scan_library()
    manifest = load_sync_manifest(drive)

    to_copy = []
    to_delete = []

    for filename, (src, size, mtime) in library.items():
        dest = drive / filename
        entry = manifest.get(filename)
        need = False
        if not dest.exists():
            need = True
        elif entry is None:
            need = True
        elif entry.get("size") != size or entry.get("mtime") != mtime:
            need = True
        if need:
            to_copy.append((filename, src, size))

    library_filenames = set(library.keys())

    for f in find_audio_files_on_drive(drive):
        try:
            rel_str = str(f.relative_to(drive))
        except ValueError:
            continue
        if rel_str not in library_filenames:
            to_delete.append(rel_str)

    new_manifest = {}
    for k, v in manifest.items():
        if k in library_filenames and (drive / k).exists():
            new_manifest[k] = v

    try:
        free = shutil.disk_usage(str(drive)).free
    except Exception:
        free = 0

    total_copy_size = sum(s for _, _, s in to_copy)

    return {
        "library_count": len(library),
        "to_copy": to_copy,
        "to_delete": to_delete,
        "free_space": free,
        "total_copy_size": total_copy_size,
        "manifest": new_manifest,
    }


# ============================================================
#             ФУНКЦИИ СКАЧИВАНИЯ С YOUTUBE
# ============================================================
def is_url(s: str) -> bool:
    s = s.strip().lower()
    return ("youtube.com" in s or "youtu.be" in s or
            s.startswith("http://") or s.startswith("https://"))


def build_ydl_opts(progress_hook):
    return {
        "format": "bestaudio/best",
        "outtmpl": str(MUSIC_DIR / "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "retries": 3,
        "socket_timeout": 30,
        "extractor_args": {
            "youtube": {"player_client": ["web_music", "web"]},
        },
    }


_YOUTUBE_SUFFIX_PATTERNS = [
    r"\s*[\(\[]\s*official\s*(music\s*)?video\s*[\)\]]",
    r"\s*[\(\[]\s*official\s*audio\s*[\)\]]",
    r"\s*[\(\[]\s*official\s*lyric\s*video\s*[\)\]]",
    r"\s*[\(\[]\s*official\s*visualizer\s*[\)\]]",
    r"\s*[\(\[]\s*lyric\s*video\s*[\)\]]",
    r"\s*[\(\[]\s*music\s*video\s*[\)\]]",
    r"\s*[\(\[]\s*audio\s*[\)\]]",
    r"\s*[\(\[]\s*official\s*[\)\]]",
    r"\s*[\(\[]\s*hd\s*[\)\]]",
    r"\s*[\(\[]\s*hq\s*[\)\]]",
    r"\s*[\(\[]\s*4k\s*[\)\]]",
    r"\s*[\(\[]\s*remaster(ed)?\s*[\)\]]",
    r"\s*[\(\[]\s*\d{4}\s*(remaster(ed)?)?\s*[\)\]]",
    r"\s*[\(\[]\s*explicit\s*[\)\]]",
]


def clean_youtube_suffix(text: str) -> str:
    if not text:
        return ""

    for pattern in _YOUTUBE_SUFFIX_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = text.strip().strip('"\'«»')
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_download_title(raw_title: str, uploader: str = "") -> tuple:
    title = clean_youtube_suffix(raw_title or "")
    uploader = (uploader or "").replace(" - Topic", "").strip()

    for separator in (" — ", " – ", " - ", "—", "–"):
        if separator in title:
            artist, track = title.split(separator, 1)
            artist = artist.strip()
            track = track.strip()
            if artist and track:
                return artist, track

    if uploader:
        return uploader, title

    return "", title


def find_newest_mp3_since(folder: Path, since_ts: float):
    candidates = []
    try:
        for f in folder.iterdir():
            if not f.is_file() or f.suffix.lower() != ".mp3":
                continue
            try:
                mtime = f.stat().st_mtime
            except Exception:
                continue
            if mtime >= since_ts - 5:
                candidates.append((mtime, f))
    except Exception:
        return None

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


def run_download(url_or_query: str, on_progress, on_log, on_done):
    try:
        if not shutil.which("ffmpeg"):
            on_log("❌ ffmpeg не найден. Установи: sudo apt install ffmpeg")
            on_done(False, "", "ffmpeg не найден")
            return

        if is_url(url_or_query):
            url = url_or_query.strip()
            if "&list=" in url:
                url = url.split("&list=", 1)[0]
            on_log("🔗 Скачиваю по ссылке...")
        else:
            url = f"ytsearch1:{url_or_query.strip()}"
            on_log(f"🔍 Ищу: {url_or_query.strip()}")

        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = downloaded / total
                    on_progress(pct, downloaded, total)
            elif d.get("status") == "finished":
                on_log("⬇ Скачано, конвертирую в MP3...")

        ydl_opts = build_ydl_opts(hook)
        download_started_at = time.time() - 2

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title = ""
        uploader = ""
        thumbnail_url = ""
        duration = 0

        if isinstance(info, dict):
            entries = info.get("entries") or []
            if entries:
                info = entries[0] or {}
            title = info.get("title") or ""
            uploader = info.get("uploader") or ""
            thumbnail_url = info.get("thumbnail") or ""
            duration = info.get("duration") or 0

        if not title:
            on_log("❌ Не удалось получить название трека")
            on_done(False, "", "Пустой результат поиска или скачивания")
            return

        on_log(f"✅ Готово: {title}")
        if uploader:
            on_log(f"   Канал: {uploader}")
        if duration:
            mins = duration // 60
            secs = duration % 60
            on_log(f"   Длительность: {mins}:{secs:02d}")

        parsed_artist, parsed_title = parse_download_title(title, uploader)
        if not parsed_title:
            parsed_title = title

        on_log("🏷 Обрабатываю метаданные...")
        on_log(f"   → Артист: {parsed_artist or '—'}")
        on_log(f"   → Название: {parsed_title}")

        newest_mp3 = None
        for _ in range(10):
            newest_mp3 = find_newest_mp3_since(MUSIC_DIR, download_started_at)
            if newest_mp3:
                break
            time.sleep(0.2)

        if newest_mp3 is None:
            on_log("❌ Не нашёл MP3 после скачивания и конвертации")
            on_done(False, title, "Файл не найден после конвертации")
            return

        cover_bytes = download_cover_bytes(parsed_artist, parsed_title)

        if cover_bytes is None and thumbnail_url:
            try:
                resp = requests.get(thumbnail_url, timeout=10)
                if resp.status_code == 200 and resp.content:
                    cover_bytes = resp.content
                    on_log("   → Использую превью с YouTube")
            except Exception:
                pass

        if cover_bytes is None:
            on_log("   → Обложка не найдена, сохраняю без обложки")

        ok = set_mp3_metadata(
            newest_mp3,
            title=parsed_title,
            artist=parsed_artist,
            cover_bytes=cover_bytes,
        )

        if ok:
            on_log(f"✅ Метаданные сохранены: {newest_mp3.name}")
        else:
            on_log("⚠ Не удалось записать метаданные")

        display_title = (
            f"{parsed_artist} — {parsed_title}"
            if parsed_artist
            else parsed_title
        )
        on_done(True, display_title, "")

    except Exception as e:
        err_msg = str(e)
        on_log(f"❌ Ошибка: {err_msg}")
        on_done(False, "", err_msg)

# ============================================================
#                        MAIN
# ============================================================
def main(page: ft.Page):
    page.title = "Моя Музыка"
    page.window_width = 1100
    page.window_height = 700
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.bgcolor = "#121212"

    audio = None
    is_dragging = False
    last_position_ui_update = [0.0]
    is_playing = False
    current_groups = {}
    current_artist_groups = {}

    playlists = load_playlists()

    current_playlist = []
    current_index = -1
    currently_playing_path = ""

    shuffle_played = set()
    play_history = []
    shuffle_playlist_id = None

    current_scrollable = None
    current_section = None
    current_playlist_name = None
    search_query = ""

    equalizer_run_id = 0
    equalizer_enabled = True
    equalizer_mode = "scale" if hasattr(getattr(ft, "transform", None), "Scale") else "height"
    track_rows = {}
    play_queue = []
    queue_panel_open = False
    heart_widgets = {}
    last_highlighted = [None]

    shuffle_on = False
    repeat_mode = 0

    current_play_id = None
    current_play_start_time = 0.0
    current_play_duration_ms = 0

    selected_drive = None
    sync_in_progress = False
    sync_plan_cache = {"drive": None, "plan": None, "computing": False, "error": None}
    sync_progress_text = ft.Text("", color="white", size=13)
    sync_progress_bar = ft.ProgressBar(value=0, color=GREEN, bgcolor="#333333")

    download_in_progress = False
    process_in_progress = False
    process_stop_event = threading.Event()
    download_log_items = []
    rec_cache = {"key": None, "data": None, "computing": False, "error": None}
    local_index_cache = {"index": None, "built_at": 0.0}

    search_field = ft.TextField(
        hint_text="Поиск...",
        prefix_icon=ft.Icons.SEARCH,
        bgcolor="#2a2a2a",
        border_color="#333333",
        focused_border_color=GREEN,
        color="white",
        cursor_color=GREEN,
        width=260,
        height=42,
        text_size=13,
    )

    view_title_text = ft.Text("", size=32, weight=ft.FontWeight.BOLD, color="white")

    play_all_paths = []

    def on_play_all_clicked(e):
        if play_all_paths:
            play_track(play_all_paths[0], play_all_paths)

    play_all_button = ft.ElevatedButton(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.PLAY_ARROW, color="black", size=22),
                ft.Text("Играть всё", color="black",
                        weight=ft.FontWeight.BOLD, size=14),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=6,
        ),
        bgcolor=GREEN,
        color="black",
        on_click=on_play_all_clicked,
    )

    refresh_button = ft.IconButton(
        ft.Icons.REFRESH, icon_color="white", tooltip="Обновить",
    )

    settings_button = ft.IconButton(
        ft.Icons.SETTINGS,
        icon_color="white",
        tooltip="Настройки",
    )

    create_playlist_button = ft.ElevatedButton(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.ADD, color="black", size=20),
                ft.Text("Создать", color="black",
                        weight=ft.FontWeight.BOLD, size=14),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=4,
        ),
        bgcolor=GREEN,
        color="black",
        visible=False,
    )

    body_container = ft.Container(expand=True)

    header_row = ft.Row(
        controls=[
            view_title_text,
            ft.Container(expand=True),
            search_field,
            play_all_button,
            create_playlist_button,
            refresh_button,
            settings_button,
        ],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        spacing=10,
    )

    # ============================================================
    #              ВЫЕЗЖАЮЩАЯ ПАНЕЛЬ СПРАВА
    # ============================================================
    slide_backdrop = ft.Container(
        bgcolor=BACKDROP,
        left=0, top=0, right=0, bottom=0,
        opacity=0,
        animate_opacity=ft.Animation(BACKDROP_ANIM_MS,
                                     ft.AnimationCurve.EASE_OUT),
        visible=False,
    )

    slide_panel = ft.Container(
        width=380,
        bgcolor=PANEL_BG,
        padding=20,
        right=0, top=0, bottom=0,
        offset=ft.Offset(1, 0),
        animate_offset=ft.Animation(PANEL_ANIM_MS,
                                    ft.AnimationCurve.EASE_OUT_CUBIC),
        visible=False,
        border=ft.border.only(left=ft.BorderSide(2, GREEN)),
    )

    def hide_slide_panel(e=None):
        nonlocal queue_panel_open
        queue_panel_open = False
        slide_panel.offset = ft.Offset(1, 0)
        slide_backdrop.opacity = 0
        page.update()

        async def _delayed_hide():
            await asyncio.sleep(PANEL_ANIM_MS / 1000)
            slide_panel.visible = False
            slide_backdrop.visible = False
            slide_panel.content = None
            page.update()
        page.run_task(_delayed_hide)

    slide_backdrop.on_click = hide_slide_panel

    def show_slide_panel(content):
        slide_panel.content = content
        slide_panel.offset = ft.Offset(1, 0)
        slide_panel.visible = True
        slide_backdrop.opacity = 0
        slide_backdrop.visible = True
        page.update()

        async def _animate_in():
            await asyncio.sleep(0.016)
            slide_panel.offset = ft.Offset(0, 0)
            slide_backdrop.opacity = 1
            page.update()
        page.run_task(_animate_in)

    def make_panel_header(title, subtitle=""):
        return ft.Row(
            controls=[
                ft.Container(expand=True, content=ft.Column(
                    controls=[
                        ft.Text(title, color="white", size=20,
                                weight=ft.FontWeight.BOLD),
                        ft.Text(subtitle, color="grey", size=12, max_lines=2)
                        if subtitle else ft.Container(),
                    ],
                    spacing=2,
                )),
                ft.IconButton(
                    ft.Icons.CLOSE, icon_color="grey",
                    tooltip="Закрыть",
                    on_click=lambda e: hide_slide_panel(),
                ),
            ],
            spacing=4,
        )

    def make_styled_button(label, color_bg, color_fg, on_click):
        return ft.ElevatedButton(
            label, bgcolor=color_bg, color=color_fg,
            on_click=on_click, height=40,
        )

    def show_add_to_playlist_panel(track_path_str):
        meta = read_metadata(Path(track_path_str))
        track_label = meta["title"]
        if meta["artist"]:
            track_label = f"{meta['artist']} — {track_label}"

        new_name_field = ft.TextField(
            hint_text="Название нового плейлиста",
            bgcolor="#2a2a2a", color="white",
            cursor_color=GREEN, border_color="#333333",
            focused_border_color=GREEN, dense=True, text_size=13,
        )
        error_text = ft.Text("", color="#ff6b6b", size=11)
        wire_typing(new_name_field)

        def create_and_add(ev):
            name = (new_name_field.value or "").strip()
            if not name:
                error_text.value = "Введите название"
                page.update()
                return
            if name not in playlists:
                playlists[name] = []
            if track_path_str not in playlists[name]:
                playlists[name].append(track_path_str)
                save_playlists(playlists)
            hide_slide_panel()

        def pick_existing(name):
            def handler(ev):
                if name not in playlists:
                    playlists[name] = []
                if track_path_str not in playlists[name]:
                    playlists[name].append(track_path_str)
                    save_playlists(playlists)
                hide_slide_panel()
            return handler

        create_section = ft.Container(
            bgcolor="#222222", padding=12, border_radius=8,
            content=ft.Column(
                controls=[
                    ft.Text("Создать новый", color="grey", size=11),
                    ft.Row(
                        controls=[
                            ft.Container(expand=True, content=new_name_field),
                            ft.IconButton(
                                ft.Icons.ADD_CIRCLE, icon_color=GREEN,
                                icon_size=30, tooltip="Создать и добавить",
                                on_click=create_and_add,
                            ),
                        ],
                        spacing=4,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    error_text,
                ],
                spacing=6,
            ),
        )

        existing_items = []
        if playlists:
            for nm in sorted(playlists.keys(), key=lambda s: s.lower()):
                existing_items.append(
                    ft.Container(
                        padding=ft.padding.symmetric(horizontal=12, vertical=10),
                        border_radius=6, ink=True,
                        on_click=pick_existing(nm),
                        content=ft.Row(
                            controls=[
                                ft.Icon(ft.Icons.QUEUE_MUSIC, color=GREEN, size=18),
                                ft.Text(nm, color="white", size=14,
                                        expand=True, max_lines=1),
                                ft.Text(str(len(playlists[nm])), color="grey", size=12),
                            ],
                            spacing=10,
                        ),
                    )
                )
        else:
            existing_items.append(
                ft.Container(
                    padding=ft.padding.symmetric(horizontal=12, vertical=16),
                    content=ft.Text("Пока нет ни одного плейлиста — создай выше.",
                                    color="grey", size=12),
                )
            )

        existing_section = ft.Column(
            controls=[
                ft.Text("Существующие плейлисты", color="grey", size=11),
                ft.Column(controls=existing_items, spacing=2,
                          scroll=ft.ScrollMode.AUTO, expand=True),
            ],
            spacing=6, expand=True,
        )

        content = ft.Column(
            controls=[
                make_panel_header("Добавить в плейлист", track_label),
                ft.Divider(color="#333333"),
                create_section,
                ft.Container(height=4),
                existing_section,
            ],
            spacing=10, expand=True,
        )
        show_slide_panel(content)

    def show_create_playlist_panel(e=None):
        name_field = ft.TextField(
            label="Название плейлиста", autofocus=True,
            bgcolor="#2a2a2a", color="white", cursor_color=GREEN,
            border_color="#333333", focused_border_color=GREEN,
        )
        error_text = ft.Text("", color="#ff6b6b", size=12)
        wire_typing(name_field)

        def do_create(ev):
            name = (name_field.value or "").strip()
            if not name:
                error_text.value = "Введите название"
                page.update()
                return
            if name in playlists:
                error_text.value = "Плейлист с таким именем уже есть"
                page.update()
                return
            playlists[name] = []
            save_playlists(playlists)
            hide_slide_panel()
            if current_section == "playlists":
                show_playlists(reset_search=False)

        content = ft.Column(
            controls=[
                make_panel_header("Новый плейлист",
                                  "Придумай короткое и понятное имя"),
                ft.Divider(color="#333333"),
                name_field,
                error_text,
                ft.Container(height=8),
                ft.Row(
                    controls=[
                        ft.Container(expand=True),
                        make_styled_button("Создать", GREEN, "black", do_create),
                    ],
                ),
            ],
            spacing=10, expand=True,
        )
        show_slide_panel(content)

    create_playlist_button.on_click = show_create_playlist_panel

    def show_delete_playlist_panel(name):
        def do_delete(ev):
            if name in playlists:
                del playlists[name]
                save_playlists(playlists)
            hide_slide_panel()
            show_playlists(reset_search=False)

        content = ft.Column(
            controls=[
                make_panel_header("Удалить плейлист?"),
                ft.Divider(color="#333333"),
                ft.Container(
                    padding=ft.padding.symmetric(vertical=20),
                    content=ft.Column(
                        controls=[
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED,
                                    color="#ff6b6b", size=48),
                            ft.Text(f"Плейлист «{name}» будет удалён навсегда.",
                                    color="white", size=14,
                                    text_align=ft.TextAlign.CENTER),
                            ft.Text("Это действие нельзя отменить.",
                                    color="grey", size=12,
                                    text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12,
                    ),
                ),
                ft.Container(expand=True),
                ft.Row(
                    controls=[
                        ft.Container(expand=True),
                        ft.TextButton("Отмена",
                                      style=ft.ButtonStyle(color="white"),
                                      on_click=lambda e: hide_slide_panel()),
                        ft.ElevatedButton("Удалить", bgcolor="#c0392b",
                                          color="white", on_click=do_delete),
                    ],
                    spacing=8,
                ),
            ],
            spacing=10, expand=True,
        )
        show_slide_panel(content)

    def show_reset_stats_panel():
        def do_reset(ev):
            reset_stats()
            hide_slide_panel()
            show_statistics()

        content = ft.Column(
            controls=[
                make_panel_header("Сбросить статистику?"),
                ft.Divider(color="#333333"),
                ft.Container(
                    padding=ft.padding.symmetric(vertical=20),
                    content=ft.Column(
                        controls=[
                            ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED,
                                    color="#ff6b6b", size=48),
                            ft.Text("Вся история прослушиваний будет удалена.",
                                    color="white", size=14,
                                    text_align=ft.TextAlign.CENTER),
                            ft.Text("Это действие нельзя отменить.",
                                    color="grey", size=12,
                                    text_align=ft.TextAlign.CENTER),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12,
                    ),
                ),
                ft.Container(expand=True),
                ft.Row(
                    controls=[
                        ft.Container(expand=True),
                        ft.TextButton("Отмена",
                                      style=ft.ButtonStyle(color="white"),
                                      on_click=lambda e: hide_slide_panel()),
                        ft.ElevatedButton("Сбросить", bgcolor="#c0392b",
                                          color="white", on_click=do_reset),
                    ],
                    spacing=8,
                ),
            ],
            spacing=10, expand=True,
        )
        show_slide_panel(content)

    def show_edit_tags_panel(track_path_str):
        pp = Path(track_path_str)
        if not pp.exists():
            return
        meta = read_metadata(pp)
        title_field = ft.TextField(
            label="Название", value=meta["title"],
            bgcolor="#2a2a2a", color="white", cursor_color=GREEN,
            border_color="#333333", focused_border_color=GREEN,
            dense=True, text_size=13)
        artist_field = ft.TextField(
            label="Исполнитель", value=meta["artist"],
            bgcolor="#2a2a2a", color="white", cursor_color=GREEN,
            border_color="#333333", focused_border_color=GREEN,
            dense=True, text_size=13)
        album_field = ft.TextField(
            label="Альбом", value=meta["album"] or "",
            bgcolor="#2a2a2a", color="white", cursor_color=GREEN,
            border_color="#333333", focused_border_color=GREEN,
            dense=True, text_size=13)
        hint_text = ft.Text(
            "Пустые поля «Исполнитель» и «Альбом» сохраняют текущие значения.",
            color="grey", size=11)
        error_text = ft.Text("", color="#ff6b6b", size=11)
        info_text = ft.Text("", color=GREEN, size=11)
        cover_holder = {"bytes": None}
        if meta["cover"]:
            preview_content = ft.Image(src=meta["cover"], width=120,
                                       height=120, fit=ft.ImageFit.COVER,
                                       border_radius=8)
        else:
            preview_content = ft.Icon(ft.Icons.IMAGE, color="grey", size=40)
        cover_preview = ft.Container(
            width=120, height=120, bgcolor="#2a2a2a", border_radius=8,
            alignment=ft.alignment.center, content=preview_content)
        wire_typing(title_field)
        wire_typing(artist_field)
        wire_typing(album_field)

        def do_find_cover(ev):
            artist = (artist_field.value or "").strip()
            title = (title_field.value or "").strip()
            if not artist or not title:
                error_text.value = "Заполни исполнителя и название для поиска обложки"
                page.update()
                return
            error_text.value = ""
            info_text.value = "Ищу обложку..."
            page.update()

            def worker():
                cover_bytes = download_cover_bytes(artist, title)

                def apply():
                    if cover_bytes:
                        cover_holder["bytes"] = cover_bytes
                        key = hashlib.md5(cover_bytes).hexdigest()
                        tmp = COVERS_DIR / f"preview_{key}.jpg"
                        tmp.write_bytes(cover_bytes)
                        cover_preview.content = ft.Image(
                            src=str(tmp), width=120, height=120,
                            fit=ft.ImageFit.COVER, border_radius=8)
                        info_text.value = "Обложка найдена — сохранится вместе с тегами"
                    else:
                        info_text.value = "Обложка не найдена, оставлю текущую"
                    page.update()

                schedule_ui_update(apply)

            threading.Thread(target=worker, daemon=True).start()

        def do_save(ev):
            title = (title_field.value or "").strip()
            artist = (artist_field.value or "").strip()
            album = (album_field.value or "").strip()
            if not title:
                error_text.value = "Название не может быть пустым"
                page.update()
                return
            ok = set_mp3_metadata(pp, title=title, artist=artist,
                                  album=album,
                                  cover_bytes=cover_holder["bytes"])
            if not ok:
                error_text.value = "Не удалось записать теги (файл не MP3?)"
                page.update()
                return
            _metadata_cache.pop(str(pp), None)
            try:
                local_index_cache["index"] = None
            except NameError:
                pass
            hide_slide_panel()
            if current_section in ("tracks", "recent", "albums",
                                   "artists", "liked", "playlists"):
                render_view()
            else:
                page.update()

        content = ft.Column(
            controls=[
                make_panel_header("Редактировать теги", pp.name),
                ft.Divider(color="#333333"),
                ft.Row(controls=[
                    cover_preview,
                    ft.Column(controls=[
                        ft.TextButton("Найти обложку",
                                      on_click=do_find_cover),
                        info_text,
                    ], spacing=6),
                ], spacing=12),
                title_field,
                artist_field,
                album_field,
                hint_text,
                error_text,
                ft.Container(expand=True),
                ft.Row(controls=[
                    ft.Container(expand=True),
                    ft.TextButton("Отмена",
                                  style=ft.ButtonStyle(color="white"),
                                  on_click=lambda e: hide_slide_panel()),
                    ft.ElevatedButton("Сохранить", bgcolor=GREEN,
                                      color="black", on_click=do_save),
                ], spacing=8),
            ],
            spacing=10, expand=True)
        show_slide_panel(content)

    def show_settings_panel(e=None):
        path_field = ft.TextField(
            label="Путь к папке с музыкой",
            value=str(MUSIC_DIR),
            bgcolor="#2a2a2a",
            color="white",
            cursor_color=GREEN,
            border_color="#333333",
            focused_border_color=GREEN,
            autofocus=True,
            text_size=13,
        )
        error_text = ft.Text("", color="#ff6b6b", size=12)
        wire_typing(path_field)
        hint_text = ft.Text(
            "В этой папке приложение ищет треки и сохраняет скачанную музыку.",
            color="grey",
            size=11,
        )

        def refresh_after_music_dir_change():
            _metadata_cache.clear()
            local_index_cache["index"] = None
            clear_rows()

            if current_section == "tracks":
                show_all_tracks(reset_search=False)
            elif current_section == "recent":
                show_recent(reset_search=False)
            elif current_section == "albums":
                show_albums(reset_search=False)
            elif current_section == "artists":
                show_artists(reset_search=False)
            elif current_section == "playlists":
                show_playlists(reset_search=False)
            else:
                page.update()

        def save_settings(ev):
            global MUSIC_DIR

            raw_path = (path_field.value or "").strip()
            if not raw_path:
                error_text.value = "Укажи путь к папке."
                page.update()
                return

            new_path = Path(os.path.expandvars(raw_path)).expanduser()
            try:
                new_path = new_path.resolve()
                if new_path.exists() and not new_path.is_dir():
                    error_text.value = "Указанный путь не является папкой."
                    page.update()
                    return
                new_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                error_text.value = f"Не удалось открыть папку: {exc}"
                page.update()
                return

            if not save_music_dir(new_path):
                error_text.value = "Не удалось сохранить настройки."
                page.update()
                return

            MUSIC_DIR = new_path
            hide_slide_panel()
            refresh_after_music_dir_change()

        content = ft.Column(
            controls=[
                make_panel_header(
                    "Настройки",
                    "Папка музыки используется для библиотеки и скачивания",
                ),
                ft.Divider(color="#333333"),
                ft.Container(
                    bgcolor="#222222",
                    border_radius=8,
                    padding=12,
                    content=ft.Column(
                        controls=[path_field, hint_text, error_text],
                        spacing=8,
                    ),
                ),
                ft.Container(expand=True),
                ft.Row(
                    controls=[
                        ft.Container(expand=True),
                        ft.TextButton(
                            "Отмена",
                            style=ft.ButtonStyle(color="white"),
                            on_click=lambda ev: hide_slide_panel(),
                        ),
                        ft.ElevatedButton(
                            "Сохранить",
                            bgcolor=GREEN,
                            color="black",
                            on_click=save_settings,
                        ),
                    ],
                    spacing=8,
                ),
            ],
            spacing=10,
            expand=True,
        )
        show_slide_panel(content)

    settings_button.on_click = show_settings_panel

    # ---------- Эквалайзер ----------
    def make_equalizer():
        bars = []
        for _ in range(4):
            if equalizer_mode == "scale":
                bars.append(ft.Container(
                    width=3, height=16, bgcolor=GREEN, border_radius=1,
                    scale=ft.transform.Scale(
                        scale_x=1.0, scale_y=0.2,
                        alignment=ft.alignment.bottom_center),
                    animate_scale=ft.Animation(
                        550, ft.AnimationCurve.EASE_IN_OUT)))
            else:
                bars.append(ft.Container(
                    width=3, height=3, bgcolor=GREEN, border_radius=1,
                    animate=ft.Animation(
                        550, ft.AnimationCurve.EASE_IN_OUT)))
        row = ft.Row(controls=bars, spacing=2,
                     alignment=ft.MainAxisAlignment.CENTER,
                     vertical_alignment=ft.CrossAxisAlignment.END)
        return row, bars
    async def animate_equalizer(bars, run_id):
        while True:
            if run_id != equalizer_run_id or not is_playing or not equalizer_enabled:
                for bar in bars:
                    try:
                        if equalizer_mode == "scale":
                            bar.scale = ft.transform.Scale(
                                scale_x=1.0, scale_y=0.2,
                                alignment=ft.alignment.bottom_center)
                        else:
                            bar.height = 3
                    except Exception:
                        pass
                try:
                    page.update()
                except Exception:
                    pass
                return
            for bar in bars:
                if equalizer_mode == "scale":
                    bar.scale = ft.transform.Scale(
                        scale_x=1.0, scale_y=random.uniform(0.25, 1.0),
                        alignment=ft.alignment.bottom_center)
                else:
                    bar.height = random.randint(3, 16)
            try:
                page.update()
            except Exception:
                return
            await asyncio.sleep(0.6)
    def stop_equalizer_animation():
        nonlocal equalizer_run_id
        equalizer_run_id += 1

    def toggle_equalizer():
        nonlocal equalizer_enabled
        equalizer_enabled = not equalizer_enabled
        if equalizer_enabled:
            if is_playing and currently_playing_path:
                refs = track_rows.get(currently_playing_path)
                if refs:
                    refs["trailing"].visible = True
                start_equalizer_animation(currently_playing_path)
        else:
            stop_equalizer_animation()
            for refs in track_rows.values():
                refs["trailing"].visible = False
        try:
            page.update()
        except Exception:
            pass

    def start_equalizer_animation(path_str):
        nonlocal equalizer_run_id
        equalizer_run_id += 1
        my_id = equalizer_run_id
        refs = track_rows.get(path_str)
        if refs and refs.get("bars"):
            page.run_task(animate_equalizer, refs["bars"], my_id)

    def register_row(path_str, title_widget, trailing_widget, bars):
        track_rows[path_str] = {
            "title": title_widget,
            "trailing": trailing_widget,
            "bars": bars,
        }

    def clear_rows():
        stop_equalizer_animation()
        track_rows.clear()
        heart_widgets.clear()

    typing_depth = [0]
    
    def _typing_focus(e):
        typing_depth[0] += 1
    
    def _typing_blur(e):
        typing_depth[0] = max(0, typing_depth[0] - 1)
    
    def wire_typing(field):
        field.on_focus = _typing_focus
        field.on_blur = _typing_blur
    
    def _is_typing():
        if typing_depth[0] > 0:
            return True
        try:
            fc = page.focused_control
            if fc is not None and isinstance(fc, ft.TextField):
                return True
        except Exception:
            pass
        return False
    
    def refresh_row_highlights(flush=True):
        old = last_highlighted[0]
        new = currently_playing_path
        if old != new:
            last_highlighted[0] = new
            for p_str in (old, new):
                refs = track_rows.get(p_str)
                if not refs:
                    continue
                playing = (p_str == new)
                refs["title"].color = GREEN if playing else "white"
                refs["trailing"].visible = playing
                refs["trailing"].opacity = 1.0 if playing else 0.0
        if flush:
            try:
                page.update()
            except Exception:
                pass
    def toggle_like(path_str):
        now_liked = not is_liked(path_str)
        set_like(path_str, now_liked)
        for widget in heart_widgets.get(path_str, []):
            widget.icon = ft.Icons.FAVORITE if now_liked else ft.Icons.FAVORITE_BORDER
            widget.icon_color = GREEN if now_liked else "grey"
        try:
            page.update()
        except Exception:
            pass
    
    def update_play_pause_icon(flush=True):
        if is_playing:
            play_pause_button.icon = ft.Icons.PAUSE
            play_pause_button.tooltip = "Пауза"
        else:
            play_pause_button.icon = ft.Icons.PLAY_ARROW
            play_pause_button.tooltip = "Играть"
        if flush:
            page.update()

    def update_shuffle_button():
        if shuffle_on:
            shuffle_button.icon_color = GREEN
            shuffle_button.tooltip = "Перемешать: вкл"
        else:
            shuffle_button.icon_color = INACTIVE
            shuffle_button.tooltip = "Перемешать: выкл"
        page.update()

    def update_repeat_button():
        if repeat_mode == 0:
            repeat_button.icon = ft.Icons.REPEAT
            repeat_button.icon_color = INACTIVE
            repeat_button.tooltip = "Повтор: выкл"
        elif repeat_mode == 1:
            repeat_button.icon = ft.Icons.REPEAT
            repeat_button.icon_color = GREEN
            repeat_button.tooltip = "Повтор: всё"
        else:
            repeat_button.icon = ft.Icons.REPEAT_ONE
            repeat_button.icon_color = GREEN
            repeat_button.tooltip = "Повтор: один трек"
        page.update()

    play_pause_button = ft.IconButton(
        ft.Icons.PLAY_ARROW, icon_color="white", tooltip="Играть")
    shuffle_button = ft.IconButton(
        ft.Icons.SHUFFLE, icon_color=INACTIVE, tooltip="Перемешать: выкл")
    repeat_button = ft.IconButton(
        ft.Icons.REPEAT, icon_color=INACTIVE, tooltip="Повтор: выкл")

    def on_play_pause_click(e):
        nonlocal is_playing
        if audio is None:
            return
        if is_playing:
            audio.pause()
            is_playing = False
        else:
            audio.resume()
            is_playing = True
            if currently_playing_path:
                start_equalizer_animation(currently_playing_path)
        update_play_pause_icon()

    play_pause_button.on_click = on_play_pause_click

    def on_shuffle_click(e):
        nonlocal shuffle_on, shuffle_played, shuffle_playlist_id
        shuffle_on = not shuffle_on
        shuffle_played = set()
        shuffle_playlist_id = None
        update_shuffle_button()

    shuffle_button.on_click = on_shuffle_click

    def on_repeat_click(e):
        nonlocal repeat_mode
        repeat_mode = (repeat_mode + 1) % 3
        update_repeat_button()

    repeat_button.on_click = on_repeat_click

    def on_position_changed(e):
        if is_dragging or audio is None:
            return
        now = time.monotonic()
        if now - last_position_ui_update[0] < 1.0:
            return
        last_position_ui_update[0] = now
        try:
            pos = int(float(e.data))
            position_slider.value = pos
            current_time_text.value = format_time(pos)
            page.update()
        except Exception:
            pass
    def on_duration_changed(e):
        nonlocal current_play_duration_ms
        try:
            dur = int(float(e.data))
            position_slider.max = dur
            total_time_text.value = format_time(dur)
            current_play_duration_ms = dur
            page.update()
        except Exception:
            pass

    def on_audio_state_changed(e):
        nonlocal is_playing
        state = str(e.data).lower() if e.data is not None else ""
        if state == "playing":
            is_playing = True
            update_play_pause_icon()
        elif state in ("paused", "stopped"):
            is_playing = False
            update_play_pause_icon()
        elif state == "completed":
            is_playing = False
            update_play_pause_icon()
            handle_track_completed()

    def on_slider_start(e):
        nonlocal is_dragging
        is_dragging = True

    def on_slider_change(e):
        current_time_text.value = format_time(position_slider.value)
        page.update()

    def on_slider_end(e):
        nonlocal is_dragging
        is_dragging = False
        if audio is not None:
            audio.seek(int(position_slider.value))
            page.update()

    def on_volume_change(e):
        if audio is not None:
            try:
                audio.volume = volume_slider.value
                audio.update()
                page.update()
            except Exception:
                pass

    def bump_volume(delta):
        volume_slider.value = min(1.0, max(0.0, (volume_slider.value or 0.0) + delta))
        if audio is not None:
            try:
                audio.volume = volume_slider.value
                audio.update()
            except Exception:
                pass
        try:
            page.update()
        except Exception:
            pass
    
    now_playing = ft.Text("Ничего не играет", color="white", size=13, expand=True)
    current_time_text = ft.Text("0:00", color="grey", size=12)
    total_time_text = ft.Text("0:00", color="grey", size=12)

    position_slider = ft.Slider(min=0, max=100, value=0, expand=True)
    position_slider.on_change_start = on_slider_start
    position_slider.on_change = on_slider_change
    position_slider.on_change_end = on_slider_end

    volume_slider = ft.Slider(min=0.0, max=1.0, value=0.7, width=140)
    volume_slider.on_change = on_volume_change

    def finalize_current_play():
        nonlocal current_play_id, current_play_start_time, current_play_duration_ms
        if current_play_id is None:
            return
        listened_ms = int((time.time() - current_play_start_time) * 1000)
        skipped = False
        if listened_ms < SKIP_THRESHOLD_SEC * 1000:
            skipped = True
        elif current_play_duration_ms > 0 and listened_ms < current_play_duration_ms * 0.5:
            skipped = True
        update_play_finish(current_play_id, current_play_duration_ms,
                           listened_ms, skipped)
        current_play_id = None
        current_play_start_time = 0.0
        current_play_duration_ms = 0

    def fade_in_audio(duration_sec=0.35, steps=7):
        if audio is None:
            return
        target = volume_slider.value or 0.7

        def runner():
            for i in range(1, steps + 1):
                frac = target * i / steps

                def apply(v=frac):
                    if audio is not None:
                        try:
                            audio.volume = v
                            audio.update()
                        except Exception:
                            pass

                schedule_ui_update(apply)
                time.sleep(duration_sec / steps)

        threading.Thread(target=runner, daemon=True).start()

    def play_track(path, playlist=None, from_history=False):
        nonlocal audio, is_playing, current_playlist, current_index, currently_playing_path
        nonlocal shuffle_played, shuffle_playlist_id
        nonlocal current_play_id, current_play_start_time, current_play_duration_ms

        finalize_current_play()
        if not path.exists():
            now_playing.value = f"⚠  Файл не найден: {path.name}. Нажми ⟳ чтобы обновить."
            page.update()
            return


        if playlist is not None:
            new_playlist = list(playlist)
            playlist_id = tuple(str(p) for p in new_playlist)
            if playlist_id != shuffle_playlist_id:
                shuffle_played = set()
                shuffle_playlist_id = playlist_id
            current_playlist = new_playlist
            try:
                current_index = current_playlist.index(path)
            except ValueError:
                current_index = -1

        file_path_str = str(path)

        meta_for_stats = read_metadata(path)
        current_play_id = log_play_start(
            file_path_str, meta_for_stats["title"],
            meta_for_stats["artist"], meta_for_stats["album"])
        current_play_start_time = time.time()
        current_play_duration_ms = 0

        if audio is not None:
            try:
                audio.pause()
                page.overlay.remove(audio)
            except Exception:
                pass
        audio = ft.Audio(
            src=file_path_str, autoplay=True,
            volume=0.0,
            on_position_changed=on_position_changed,
            on_duration_changed=on_duration_changed,
            on_state_changed=on_audio_state_changed,
        )
        page.overlay.append(audio)
        fade_in_audio()
        is_playing = True
        currently_playing_path = file_path_str
        shuffle_played.add(file_path_str)
        if not from_history:
            if not play_history or play_history[-1] != file_path_str:
                play_history.append(file_path_str)
                if len(play_history) > 200:
                    del play_history[0]
        update_play_pause_icon(flush=False)

        meta = read_metadata(path)
        label = meta["title"]
        if meta["artist"]:
            label = f"{meta['artist']} — {meta['title']}"
        now_playing.value = f"▶  {label}"

        position_slider.value = 0
        current_time_text.value = "0:00"

        refresh_row_highlights(flush=False)
        page.update()
        start_equalizer_animation(file_path_str)


    def pick_next_shuffle_index():
        nonlocal shuffle_played
        if not current_playlist:
            return -1
        candidates = []
        for i, p in enumerate(current_playlist):
            p_str = str(p)
            if i == current_index:
                continue
            if p_str in shuffle_played:
                continue
            candidates.append(i)
        if not candidates:
            shuffle_played = set()
            for i, p in enumerate(current_playlist):
                if i != current_index:
                    candidates.append(i)
            if not candidates:
                return current_index
        return random.choice(candidates)

    def play_next(e=None):
        nonlocal current_index
        if play_queue:
            queued = play_queue.pop(0)
            refresh_queue_panel()
            qp = Path(queued)
            if qp.exists():
                play_track(qp)
                return
        if not current_playlist:
            return
        if shuffle_on and len(current_playlist) > 1:
            idx = pick_next_shuffle_index()
            if idx >= 0:
                current_index = idx
                play_track(current_playlist[current_index])
            return
        if current_index < len(current_playlist) - 1:
            current_index += 1
            play_track(current_playlist[current_index])
        else:
            if repeat_mode == 1:
                current_index = 0
                play_track(current_playlist[current_index])

    def play_prev(e=None):
        nonlocal current_index
        if not current_playlist:
            return
        if shuffle_on and len(current_playlist) > 1:
            if len(play_history) >= 2:
                target_str = play_history[-2]
                del play_history[-1]
                target = Path(target_str)
                if target.exists():
                    try:
                        current_index = current_playlist.index(target)
                    except ValueError:
                        current_index = -1
                    play_track(target, from_history=True)
                    return
            if audio is not None and currently_playing_path:
                audio.seek(0)
                position_slider.value = 0
                current_time_text.value = "0:00"
                page.update()
            return
        if current_index > 0:
            current_index -= 1
            play_track(current_playlist[current_index])
        else:
            play_track(current_playlist[current_index])
    def handle_track_completed():
        nonlocal is_playing
        if repeat_mode == 2:
            if current_playlist and 0 <= current_index < len(current_playlist):
                path = current_playlist[current_index]
                if audio is not None:
                    try:
                        audio.seek(0)
                        audio.play()
                    except Exception:
                        play_track(path)
                is_playing = True
                update_play_pause_icon()
                start_equalizer_animation(str(path))
        else:
            play_next()

    def on_stop(e):
        nonlocal is_playing, currently_playing_path
        finalize_current_play()
        if audio is not None:
            audio.pause()
            audio.seek(0)
            position_slider.value = 0
            current_time_text.value = "0:00"
        is_playing = False
        currently_playing_path = ""
        update_play_pause_icon()
        now_playing.value = "Ничего не играет"
        page.update()
        stop_equalizer_animation()
        refresh_row_highlights()

    def enqueue_track(path_str):
        play_queue.append(path_str)
        refresh_queue_panel()
    
    def remove_from_queue(idx):
        if 0 <= idx < len(play_queue):
            del play_queue[idx]
        refresh_queue_panel()
    
    def clear_queue():
        play_queue.clear()
        refresh_queue_panel()
    
    def refresh_queue_panel():
        if queue_panel_open:
            show_queue_panel(refresh=True)
    
    def show_queue_panel(e=None, refresh=False):
        nonlocal queue_panel_open
        rows = []
        if not play_queue:
            rows.append(ft.Container(
                padding=ft.padding.symmetric(vertical=20),
                content=ft.Column(
                    controls=[
                        ft.Icon(ft.Icons.QUEUE_MUSIC, color="grey", size=48),
                        ft.Text("Очередь пуста", color="white", size=14),
                        ft.Text("Добавь треки через меню ⋯ → «В очередь»",
                            color="grey", size=12),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=10,
                ),
            ))
        for idx, p in enumerate(play_queue):
            pp = Path(p)
            if pp.exists():
                meta = read_metadata(pp)
                title = meta["title"]
                artist = meta["artist"]
                cover = meta["cover"]
            else:
                title = pp.name
                artist = ""
                cover = None
            if cover:
                cover_widget = ft.Image(src=cover, width=40, height=40,
                                            fit=ft.ImageFit.COVER, border_radius=4)
            else:
                cover_widget = ft.Container(
                    width=40, height=40, bgcolor="#2a2a2a", border_radius=4,
                    alignment=ft.alignment.center,
                    content=ft.Icon(ft.Icons.MUSIC_NOTE, color="grey", size=18))
            rows.append(ft.Container(
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                border_radius=6,
                content=ft.Row(
                    controls=[
                        ft.Text(str(idx + 1), color=GREEN, width=24,
                                size=13, weight=ft.FontWeight.BOLD),
                        cover_widget,
                        ft.Column(controls=[
                            ft.Text(title, color="white", size=13, max_lines=1),
                            ft.Text(artist or "—", color="grey", size=11, max_lines=1),
                        ], spacing=2, expand=True),
                        ft.IconButton(
                            ft.Icons.CLOSE, icon_color="grey", icon_size=16,
                            tooltip="Убрать из очереди",
                            on_click=lambda e, i=idx: remove_from_queue(i)),
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)))
        header = make_panel_header(
            "Очередь воспроизведения",
            "Эти треки заиграют следующими")
        clear_row = ft.Row(controls=[
            ft.Text(f"В очереди: {len(play_queue)}", color="grey", size=12),
            ft.Container(expand=True),
            ft.TextButton("Очистить", on_click=lambda e: clear_queue())
            if play_queue else ft.Container(),
        ])
        content = ft.Column(
            controls=[header, ft.Divider(color="#333333"), clear_row,
                      ft.Column(rows, spacing=2, scroll=ft.ScrollMode.AUTO,
                                expand=True)],
            spacing=10, expand=True)
        if refresh and queue_panel_open:
            slide_panel.content = content
            page.update()
            return
        show_slide_panel(content)
        queue_panel_open = True
    
    queue_button = ft.IconButton(
        ft.Icons.QUEUE_MUSIC, icon_color="white",
        tooltip="Очередь воспроизведения",
        on_click=show_queue_panel,
    )
    
    player_bar = ft.Container(
        bgcolor="#181818",
        padding=ft.padding.symmetric(horizontal=20, vertical=10),
        content=ft.Column(
            controls=[
                ft.Row(
                    controls=[current_time_text, position_slider, total_time_text],
                    spacing=10,
                ),
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.MUSIC_NOTE, color=GREEN, size=24),
                        now_playing,
                        shuffle_button,
                        ft.IconButton(ft.Icons.SKIP_PREVIOUS, icon_color="white",
                                      on_click=play_prev, tooltip="Предыдущий"),
                        play_pause_button,
                        ft.IconButton(ft.Icons.SKIP_NEXT, icon_color="white",
                                      on_click=play_next, tooltip="Следующий"),
                        repeat_button,
                        ft.IconButton(ft.Icons.STOP, icon_color="white",
                                      on_click=on_stop, tooltip="Стоп"),
                                      queue_button,
                        ft.Icon(ft.Icons.VOLUME_UP, color="grey", size=20),
                        volume_slider,
                    ],
                    spacing=5,
                ),
            ],
            spacing=6,
        ),
    )

    content_area = ft.Container(
        expand=True, padding=30,
        content=ft.Text("Выбери раздел слева", size=20, color="white"),
    )

    def make_play_all_button(on_click, label="Играть всё"):
        return ft.ElevatedButton(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.PLAY_ARROW, color="black", size=22),
                    ft.Text(label, color="black",
                            weight=ft.FontWeight.BOLD, size=14),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=6,
            ),
            bgcolor=GREEN, color="black",
            on_click=on_click,
        )

    def make_track_row(path_obj, idx_display, subtitle_text, playlist,
                       use_cover=False, in_playlist_name=None):
        path_str = str(path_obj)
        meta = read_metadata(path_obj)
        is_current = (path_str == currently_playing_path)

        if use_cover:
            if meta["cover"]:
                leading = ft.Image(src=meta["cover"], width=48, height=48,
                                   fit=ft.ImageFit.COVER, border_radius=4)
            else:
                leading = make_cover_placeholder([path_str], 48, 4)
        else:
            leading = ft.Text(str(idx_display),
                              color=GREEN if is_current else "grey", width=30)

        title_widget = ft.Text(meta["title"],
                               color=GREEN if is_current else "white")

        eq_row, eq_bars = make_equalizer()
        eq_container = ft.Container(content=eq_row, visible=is_current,
                            width=25, height=20,
                            opacity=1.0 if is_current else 0.0,
                            animate_opacity=ft.Animation(
                                250, ft.AnimationCurve.EASE_OUT))

        menu_items = [
            ft.PopupMenuItem(text="Добавить в плейлист",
                             icon=ft.Icons.PLAYLIST_ADD,
                             on_click=lambda e, p=path_str:
                                 show_add_to_playlist_panel(p)),
                                 ft.PopupMenuItem(text="В очередь",
                                          icon=ft.Icons.PLAYLIST_PLAY,
                                          on_click=lambda e, p=path_str:
                                              enqueue_track(p)),
                                              ft.PopupMenuItem(text="Редактировать теги",
                                                       icon=ft.Icons.EDIT,
                                                       on_click=lambda e, p=path_str:
                                                           show_edit_tags_panel(p)),
        ]
        if in_playlist_name:
            menu_items.append(
                ft.PopupMenuItem(
                    text=f"Удалить из «{in_playlist_name}»",
                    icon=ft.Icons.DELETE_OUTLINE,
                    on_click=lambda e, p=path_str, nm=in_playlist_name:
                        remove_from_playlist(nm, p)))

        popup = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT, icon_color="grey", items=menu_items)

        heart_button = ft.IconButton(
            ft.Icons.FAVORITE if is_liked(path_str) else ft.Icons.FAVORITE_BORDER,
            icon_color=GREEN if is_liked(path_str) else "grey",
            icon_size=20, tooltip="Нравится",
            on_click=lambda e, p=path_str: toggle_like(p),
        )
        heart_widgets.setdefault(path_str, []).append(heart_button)
        trailing_row = ft.Row(controls=[eq_container, heart_button, popup],
            spacing=0, tight=True)

        tile = ft.ListTile(
            key=path_str,
            leading=leading,
            title=title_widget,
            subtitle=ft.Text(subtitle_text, color="grey", size=11),
            trailing=trailing_row,
            on_click=lambda e, p=path_obj, pl=playlist: play_track(p, pl),
        )
        register_row(path_str, title_widget, eq_container, eq_bars)
        return tile

    def remove_from_playlist(name, path_str):
        if name not in playlists:
            return
        if path_str in playlists[name]:
            playlists[name].remove(path_str)
            save_playlists(playlists)
        if current_section == "playlists" and current_playlist_name == name:
            show_playlist_detail(name)

    def matches_query(*texts):
        if not search_query:
            return True
        for t in texts:
            if t and search_query in str(t).lower():
                return True
        return False

    def render_view():
        nonlocal current_scrollable, play_all_paths
        nonlocal current_groups, current_artist_groups
        clear_cover_subscribers()

        if current_section == "tracks":
            files = find_audio_files(MUSIC_DIR)
            if search_query:
                files = [f for f in files
                         if matches_query(read_metadata(f)["title"],
                                          read_metadata(f)["artist"],
                                          read_metadata(f)["album"], f.name)]
            play_all_paths = list(files)
            view_title_text.value = (f"Все треки — найдено {len(files)}"
                                     if search_query else f"Все треки ({len(files)})")
            clear_rows()
            if not files:
                msg = (f"Ничего не найдено по запросу «{search_field.value}»"
                       if search_query else f"Папка пуста: {MUSIC_DIR}")
                body_container.content = ft.Column(
                    [ft.Text(msg, color="grey", size=14)])
            else:
                rows = []
                for i, f in enumerate(files, start=1):
                    meta = read_metadata(f)
                    parts = [p for p in [meta["artist"], meta["album"]] if p]
                    subtitle_text = " • ".join(parts) if parts else str(f.parent)
                    rows.append(make_track_row(f, i, subtitle_text, play_all_paths,
                                               use_cover=True))
                rows_col = ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True)
                current_scrollable = rows_col
                body_container.content = rows_col
                if currently_playing_path and currently_playing_path in track_rows:
                    start_equalizer_animation(currently_playing_path)

        elif current_section == "liked":
            files = [Path(p) for p in get_liked_paths() if Path(p).exists()]
            if search_query:
                files = [f for f in files
                         if matches_query(read_metadata(f)["title"],
                                          read_metadata(f)["artist"],
                                          read_metadata(f)["album"], f.name)]
            play_all_paths = list(files)
            view_title_text.value = (f"Любимые — найдено {len(files)}"
                                     if search_query else f"Любимые ({len(files)})")
            clear_rows()
            if not files:
                msg = (f"Ничего не найдено по запросу «{search_field.value}»"
                       if search_query
                       else "Пока нет любимых треков. Нажми ♥ на треке.")
                body_container.content = ft.Column(
                    [ft.Text(msg, color="grey", size=14)])
            else:
                rows = []
                for i, f in enumerate(files, start=1):
                    meta = read_metadata(f)
                    parts = [p for p in [meta["artist"], meta["album"]] if p]
                    subtitle_text = " • ".join(parts) if parts else str(f.parent)
                    rows.append(make_track_row(f, i, subtitle_text, play_all_paths,
                                                   use_cover=True))
                rows_col = ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True)
                current_scrollable = rows_col
                body_container.content = rows_col
                if currently_playing_path and currently_playing_path in track_rows:
                    start_equalizer_animation(currently_playing_path)
        elif current_section == "recent":
            files = find_recently_added_files(MUSIC_DIR)
            if search_query:
                files = [f for f in files
                         if matches_query(read_metadata(f)["title"],
                                          read_metadata(f)["artist"],
                                          read_metadata(f)["album"], f.name)]
            play_all_paths = list(files)
            view_title_text.value = (f"Недавно добавленные — найдено {len(files)}"
                                     if search_query
                                     else f"Недавно добавленные ({len(files)})")
            clear_rows()
            if not files:
                msg = (f"Ничего не найдено по запросу «{search_field.value}»"
                       if search_query
                       else "Пока нет треков. Скачай что-нибудь!")
                body_container.content = ft.Column(
                    [ft.Text(msg, color="grey", size=14)])
            else:
                rows = []
                for i, f in enumerate(files, start=1):
                    meta = read_metadata(f)
                    try:
                        mtime = f.stat().st_mtime
                        when = format_relative_time(mtime)
                    except Exception:
                        when = ""
                    parts = [p for p in [meta["artist"], meta["album"], when] if p]
                    subtitle_text = " • ".join(parts)
                    rows.append(make_track_row(f, i, subtitle_text,
                                               play_all_paths, use_cover=True))
                rows_col = ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True)
                current_scrollable = rows_col
                body_container.content = rows_col
                if currently_playing_path and currently_playing_path in track_rows:
                    start_equalizer_animation(currently_playing_path)

        elif current_section == "albums":
            files = find_audio_files(MUSIC_DIR)
            current_groups = group_by_album(files)
            sorted_items = sorted(current_groups.items(),
                                  key=lambda kv: (kv[1]["artist"].lower(),
                                                  kv[1]["album"].lower()))
            if search_query:
                sorted_items = [(k, g) for k, g in sorted_items
                                if matches_query(g["album"], g["artist"])]
            view_title_text.value = (f"Альбомы — найдено {len(sorted_items)}"
                                     if search_query
                                     else f"Альбомы ({len(current_groups)})")
            if not files:
                body_container.content = ft.Column([
                    ft.Text(f"Папка пуста: {MUSIC_DIR}", color="grey")])
            elif not sorted_items:
                body_container.content = ft.Column([
                    ft.Text(f"Ничего не найдено по запросу «{search_field.value}»",
                            color="grey", size=14)])
            else:
                cards = []
                for key, group in sorted_items:
                    cover_widget = (ft.Image(src=group["cover"], width=170,
                                             height=170, fit=ft.ImageFit.COVER,
                                             border_radius=8)
                                    if group["cover"]
                                    else make_cover_placeholder([str(t) for t in group['tracks']], 170, 8, ft.Icons.ALBUM))
                    cards.append(ft.Container(
                        width=170, padding=8, border_radius=8, ink=True,
                        on_click=lambda e, k=key: show_album_detail(k),
                        content=ft.Column(controls=[
                            cover_widget,
                            ft.Text(group["album"], color="white",
                                    weight=ft.FontWeight.BOLD, size=13, max_lines=1),
                            ft.Text(group["artist"], color="grey", size=11,
                                    max_lines=1),
                            ft.Text(f"{len(group['tracks'])} трек(ов)",
                                    color=GREEN, size=10),
                        ], spacing=4)))
                body_container.content = ft.GridView(
                    expand=True, max_extent=200, spacing=15, run_spacing=15,
                    child_aspect_ratio=0.72, controls=cards)

        elif current_section == "artists":
            files = find_audio_files(MUSIC_DIR)
            current_artist_groups = group_by_artist(files)
            sorted_items = sorted(current_artist_groups.items(),
                                  key=lambda kv: kv[1]["artist"].lower())
            if search_query:
                sorted_items = [(k, g) for k, g in sorted_items
                                if matches_query(g["artist"])]
            view_title_text.value = (f"Исполнители — найдено {len(sorted_items)}"
                                     if search_query
                                     else f"Исполнители ({len(current_artist_groups)})")
            if not files:
                body_container.content = ft.Column([
                    ft.Text(f"Папка пуста: {MUSIC_DIR}", color="grey")])
            elif not sorted_items:
                body_container.content = ft.Column([
                    ft.Text(f"Ничего не найдено по запросу «{search_field.value}»",
                            color="grey", size=14)])
            else:
                cards = []
                for key, group in sorted_items:
                    photo = get_artist_photo_cache_only(group["artist"])
                    if photo:
                        avatar = ft.Container(
                            width=170, height=170, border_radius=85,
                            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                            content=ft.Image(src=photo, width=170, height=170,
                                             fit=ft.ImageFit.COVER))
                    else:
                        color = artist_color(group["artist"])
                        letter = group["artist"][:1].upper() if group["artist"] else "?"
                        avatar = ft.Container(
                            width=170, height=170, bgcolor=color, border_radius=85,
                            alignment=ft.alignment.center,
                            content=ft.Text(letter, color="white", size=72,
                                            weight=ft.FontWeight.BOLD))
                    subscribe_cover(artist_photo_key(group["artist"]), lambda cp, c=avatar: _apply_cover_to_placeholder(c, cp, 170, 85))
                    cards.append(ft.Container(
                        width=170, padding=8, border_radius=8, ink=True,
                        on_click=lambda e, k=key: show_artist_detail(k),
                        content=ft.Column(controls=[
                            ft.Row([avatar], alignment=ft.MainAxisAlignment.CENTER),
                            ft.Text(group["artist"], color="white",
                                    weight=ft.FontWeight.BOLD, size=13,
                                    max_lines=1, text_align=ft.TextAlign.CENTER),
                            ft.Text(f"{len(group['tracks'])} трек(ов) • "
                                    f"{len(group['albums'])} альб.",
                                    color=GREEN, size=10,
                                    text_align=ft.TextAlign.CENTER),
                        ], spacing=6)))
                body_container.content = ft.GridView(
                    expand=True, max_extent=200, spacing=15, run_spacing=15,
                    child_aspect_ratio=0.72, controls=cards)

        elif current_section == "playlists":
            if current_playlist_name is not None:
                render_playlist_detail_body(current_playlist_name)
            else:
                names = sorted(playlists.keys(), key=lambda s: s.lower())
                if search_query:
                    names = [n for n in names if matches_query(n)]
                view_title_text.value = (f"Плейлисты — найдено {len(names)}"
                                         if search_query
                                         else f"Плейлисты ({len(playlists)})")
                if not names:
                    msg = (f"Ничего не найдено по запросу «{search_field.value}»"
                           if search_query else "Пока нет ни одного плейлиста.")
                    body_container.content = ft.Column([
                        ft.Text(msg, color="grey", size=14)])
                else:
                    cards = []
                    for name in names:
                        paths = playlists.get(name, [])
                        covers_for_collage = []
                        for p in paths[:4]:
                            pp = Path(p)
                            if pp.exists():
                                m = read_metadata(pp)
                                if m.get("cover"):
                                    covers_for_collage.append(m["cover"])
                        if len(covers_for_collage) >= 4:
                            collage = ft.GridView(
                                width=170, height=170, max_extent=85,
                                spacing=0, run_spacing=0,
                                controls=[ft.Image(src=c, width=85, height=85,
                                                   fit=ft.ImageFit.COVER)
                                          for c in covers_for_collage[:4]])
                        elif len(covers_for_collage) >= 1:
                            collage = ft.Container(
                                width=170, height=170,
                                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                                border_radius=8,
                                content=ft.Image(src=covers_for_collage[0],
                                                 width=170, height=170,
                                                 fit=ft.ImageFit.COVER))
                        else:
                            collage = ft.Container(
                                width=170, height=170, bgcolor="#2a2a2a",
                                border_radius=8, alignment=ft.alignment.center,
                                content=ft.Icon(ft.Icons.QUEUE_MUSIC,
                                                color="grey", size=64))
                        cards.append(ft.Container(
                            width=170, padding=8, border_radius=8, ink=True,
                            on_click=lambda e, n=name: show_playlist_detail(n),
                            content=ft.Column(controls=[
                                collage,
                                ft.Text(name, color="white",
                                        weight=ft.FontWeight.BOLD, size=13,
                                        max_lines=1),
                                ft.Text(f"{len(paths)} трек(ов)",
                                        color=GREEN, size=10),
                            ], spacing=4)))
                    body_container.content = ft.GridView(
                        expand=True, max_extent=200, spacing=15, run_spacing=15,
                        child_aspect_ratio=0.72, controls=cards)

        try:
            page.update()
        except Exception:
            pass

    def render_playlist_detail_body(name):
        nonlocal current_scrollable
        if name not in playlists:
            return
        paths = playlists.get(name, [])
        existing = [Path(p) for p in paths if Path(p).exists()]
        cover_path = read_metadata(existing[0]).get("cover") if existing else None
        if cover_path:
            big_cover = ft.Image(src=cover_path, width=180, height=180,
                                 fit=ft.ImageFit.COVER, border_radius=8)
        else:
            big_cover = ft.Container(width=180, height=180, bgcolor="#2a2a2a",
                                     border_radius=8,
                                     alignment=ft.alignment.center,
                                     content=ft.Icon(ft.Icons.QUEUE_MUSIC,
                                                     color="grey", size=64))
        playlist_paths = list(existing)

        def play_all(e):
            if playlist_paths:
                play_track(playlist_paths[0], playlist_paths)

        play_btn = make_play_all_button(play_all, "Играть плейлист")
        delete_btn = ft.IconButton(
            ft.Icons.DELETE_OUTLINE, icon_color="#ff6b6b",
            tooltip="Удалить плейлист",
            on_click=lambda e: show_delete_playlist_panel(name))
        header_info = ft.Column(controls=[
            ft.Text(name, color="white", size=24, weight=ft.FontWeight.BOLD),
            ft.Text(f"{len(existing)} трек(ов)", color=GREEN, size=12),
            ft.Container(height=6),
            ft.Row(controls=[play_btn, delete_btn], spacing=6),
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        header_row_pl = ft.Row(controls=[big_cover, header_info], spacing=20)

        if not existing:
            rows_body = ft.Column([
                ft.Text("Плейлист пуст. Добавь треки через меню ⋯ в других вкладках.",
                        color="grey", size=13)])
        else:
            rows = []
            for i, f in enumerate(existing, start=1):
                meta = read_metadata(f)
                parts = [p for p in [meta["artist"], meta["album"]] if p]
                subtitle_text = " • ".join(parts) if parts else ""
                rows.append(make_track_row(f, i, subtitle_text, playlist_paths,
                                           use_cover=True, in_playlist_name=name))
            rows_col = ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True)
            current_scrollable = rows_col
            rows_body = rows_col

        body_container.content = ft.Column([
            header_row_pl, ft.Divider(color="grey"), rows_body,
        ], expand=True, spacing=10)

        search_field.visible = False
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.ARROW_BACK
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Назад к плейлистам"
        refresh_button.on_click = lambda e: show_playlists(reset_search=False)

    def on_search_change(e):
        nonlocal search_query
        search_query = (search_field.value or "").strip().lower()
        if current_section in ("tracks", "recent", "albums", "artists", "liked"):
            render_view()
        elif current_section == "playlists" and current_playlist_name is None:
            render_view()

    search_field.on_change = on_search_change
    wire_typing(search_field)

    def on_refresh_tracks(e):
        _metadata_cache.clear()
        local_index_cache["index"] = None
        show_all_tracks(reset_search=False)

    def show_all_tracks(e=None, reset_search=False):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "tracks"
        current_playlist_name = None
        if reset_search:
            search_query = ""
            search_field.value = ""
        search_field.visible = True
        play_all_button.visible = True
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить"
        refresh_button.on_click = on_refresh_tracks
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        render_view()

    def on_refresh_liked(e):
        show_liked(reset_search=False)
    
    def show_liked(e=None, reset_search=False):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "liked"
        current_playlist_name = None
        if reset_search:
            search_query = ""
            search_field.value = ""
        search_field.visible = True
        play_all_button.visible = True
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить"
        refresh_button.on_click = on_refresh_liked
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        render_view()
    def on_refresh_recent(e):
        _metadata_cache.clear()
        local_index_cache["index"] = None
        show_recent(reset_search=False)

    def show_recent(e=None, reset_search=False):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "recent"
        current_playlist_name = None
        if reset_search:
            search_query = ""
            search_field.value = ""
        search_field.visible = True
        play_all_button.visible = True
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить"
        refresh_button.on_click = on_refresh_recent
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        render_view()

    def on_refresh_albums(e):
        _metadata_cache.clear()
        local_index_cache["index"] = None
        show_albums(reset_search=False)

    def show_albums(e=None, reset_search=False):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "albums"
        current_playlist_name = None
        if reset_search:
            search_query = ""
            search_field.value = ""
        search_field.visible = True
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить"
        refresh_button.on_click = on_refresh_albums
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        render_view()

    def show_album_detail(key):
        nonlocal current_scrollable
        clear_rows()
        group = current_groups.get(key)
        if not group:
            show_albums()
            return
        back_button = ft.TextButton("← Назад к альбомам",
                                    on_click=lambda e: show_albums())
        if group["cover"]:
            big_cover = ft.Image(src=group["cover"], width=180, height=180,
                                 fit=ft.ImageFit.COVER, border_radius=8)
        else:
            big_cover = ft.Container(width=180, height=180, bgcolor="#2a2a2a",
                                     border_radius=8,
                                     alignment=ft.alignment.center,
                                     content=ft.Icon(ft.Icons.ALBUM,
                                                     color="grey", size=64))
        album_paths = list(group["tracks"])

        def play_album(e):
            if album_paths:
                play_track(album_paths[0], album_paths)

        play_album_button = make_play_all_button(play_album, "Играть альбом")
        for _t in group["tracks"]:
            subscribe_cover(str(_t), lambda cp, c=big_cover: _apply_cover_to_placeholder(c, cp, 180, 8))
        header_info = ft.Column(controls=[
            ft.Text(group["album"], color="white", size=24,
                    weight=ft.FontWeight.BOLD),
            ft.Text(group["artist"], color="grey", size=14),
            ft.Text(f"{len(group['tracks'])} трек(ов)", color=GREEN, size=12),
            ft.Container(height=6),
            play_album_button,
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        header_row_album = ft.Row(controls=[big_cover, header_info], spacing=20)
        rows = []
        for i, f in enumerate(group["tracks"], start=1):
            meta = read_metadata(f)
            rows.append(make_track_row(f, i, meta["artist"] or "",
                                       album_paths, use_cover=True))
        rows_column = ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True)
        current_scrollable = rows_column
        content_area.content = ft.Column([
            back_button, header_row_album, ft.Divider(color="grey"), rows_column,
        ], expand=True, spacing=10)
        page.update()
        if currently_playing_path and currently_playing_path in track_rows:
            start_equalizer_animation(currently_playing_path)

    def on_refresh_artists(e):
        _metadata_cache.clear()
        local_index_cache["index"] = None
        show_artists(reset_search=False)

    def show_artists(e=None, reset_search=False):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "artists"
        current_playlist_name = None
        if reset_search:
            search_query = ""
            search_field.value = ""
        search_field.visible = True
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить"
        refresh_button.on_click = on_refresh_artists
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        render_view()

    def show_artist_detail(key):
        nonlocal current_scrollable
        clear_rows()
        group = current_artist_groups.get(key)
        if not group:
            show_artists()
            return
        back_button = ft.TextButton("← Назад к исполнителям",
                                    on_click=lambda e: show_artists())
        photo = get_artist_photo_cache_only(group["artist"])
        if photo:
            big_avatar = ft.Container(
                width=180, height=180, border_radius=90,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=ft.Image(src=photo, width=180, height=180,
                                 fit=ft.ImageFit.COVER))
        else:
            color = artist_color(group["artist"])
            letter = group["artist"][:1].upper() if group["artist"] else "?"
            big_avatar = ft.Container(
                width=180, height=180, bgcolor=color, border_radius=90,
                alignment=ft.alignment.center,
                content=ft.Text(letter, color="white", size=88,
                                weight=ft.FontWeight.BOLD))
        subscribe_cover(artist_photo_key(group["artist"]), lambda cp, c=big_avatar: _apply_cover_to_placeholder(c, cp, 180, 90))
        artist_paths = list(group["tracks"])

        def play_artist(e):
            if artist_paths:
                play_track(artist_paths[0], artist_paths)

        play_artist_button = make_play_all_button(play_artist, "Играть всё")
        header_info = ft.Column(controls=[
            ft.Text(group["artist"], color="white", size=26,
                    weight=ft.FontWeight.BOLD),
            ft.Text(f"{len(group['tracks'])} трек(ов) • "
                    f"{len(group['albums'])} альбом(ов)",
                    color="grey", size=13),
            ft.Container(height=6),
            play_artist_button,
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        header_row_artist = ft.Row(controls=[big_avatar, header_info], spacing=20)

        def sort_key(p):
            m = read_metadata(p)
            return ((m["album"] or "").lower(), (m["title"] or "").lower())

        sorted_tracks = sorted(group["tracks"], key=sort_key)
        rows = []
        for i, f in enumerate(sorted_tracks, start=1):
            meta = read_metadata(f)
            rows.append(make_track_row(f, i, meta["album"] or "",
                                       artist_paths, use_cover=True))
        rows_column = ft.Column(rows, scroll=ft.ScrollMode.AUTO, expand=True)
        current_scrollable = rows_column
        content_area.content = ft.Column([
            back_button, header_row_artist, ft.Divider(color="grey"), rows_column,
        ], expand=True, spacing=10)
        page.update()
        if currently_playing_path and currently_playing_path in track_rows:
            start_equalizer_animation(currently_playing_path)

    def on_refresh_playlists(e):
        show_playlists(reset_search=False)

    def show_playlists(e=None, reset_search=False):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "playlists"
        current_playlist_name = None
        if reset_search:
            search_query = ""
            search_field.value = ""
        search_field.visible = True
        play_all_button.visible = False
        create_playlist_button.visible = True
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить"
        refresh_button.on_click = on_refresh_playlists
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        render_view()

    def show_playlist_detail(name):
        nonlocal current_section, current_playlist_name
        if name not in playlists:
            show_playlists(reset_search=False)
            return
        current_section = "playlists"
        current_playlist_name = name
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        clear_rows()
        render_playlist_detail_body(name)
        page.update()
        if currently_playing_path and currently_playing_path in track_rows:
            start_equalizer_animation(currently_playing_path)

    # ============================================================
    #              ВКЛАДКА "РЕКОМЕНДАЦИИ"
    # ============================================================
    def build_local_track_index():
        """Один раз собирает индекс библиотеки для поиска совпадений."""
        now = time.monotonic()
        if (local_index_cache["index"] is not None
                and now - local_index_cache["built_at"] < LOCAL_INDEX_TTL_SEC):
            return local_index_cache["index"]
        exact = {}
        fuzzy = []
        for f in find_audio_files(MUSIC_DIR):
            meta = read_metadata(f)
            a = (meta["artist"] or "").lower().strip()
            t = (meta["title"] or "").lower().strip()
            if not a or not t:
                continue
            fuzzy.append((a, t, f))
            exact.setdefault((a, t), f)
        index = (exact, fuzzy)
        local_index_cache["index"] = index
        local_index_cache["built_at"] = now
        return index

    def find_local_track(artist, title, index=None):
        if index is None:
            index = build_local_track_index()
        artist_lower = artist.lower().strip()
        title_lower = title.lower().strip()
        exact, fuzzy = index
        hit = exact.get((artist_lower, title_lower))
        if hit is not None:
            return hit
        for a, t, f in fuzzy:
            if (artist_lower in a or a in artist_lower) and (title_lower in t or t in title_lower):
                return f
        return None

    def rec_reference_quick():
        # Опорный трек без сканирования папки: то, что играет прямо сейчас.
        if currently_playing_path:
            ref_path = Path(currently_playing_path)
            if ref_path.exists():
                meta = read_metadata(ref_path)
                if meta["artist"] and meta["title"]:
                    return meta["artist"], meta["title"]
        return None

    def rec_cache_key(artist, title):
        return f"{artist.lower()}|{title.lower()}"

    def start_recommendations_compute():
        if rec_cache["computing"]:
            return
        rec_cache["computing"] = True
        rec_cache["error"] = None

        def worker():
            try:
                reference = rec_reference_quick()
                if reference is None:
                    liked = [Path(p) for p in get_liked_paths()
                             if Path(p).exists()]
                    if liked:
                        ref_path = random.choice(liked)
                        meta = read_metadata(ref_path)
                        if meta["artist"] and meta["title"]:
                            reference = (meta["artist"], meta["title"])
                    all_files = find_audio_files(MUSIC_DIR)
                    if reference is None and all_files:
                        ref_path = random.choice(all_files)
                        meta = read_metadata(ref_path)
                        if meta["artist"] and meta["title"]:
                            reference = (meta["artist"], meta["title"])
                if reference is None:
                    rec_cache["key"] = ""
                    rec_cache["data"] = {"reference": None,
                                         "tracks": [], "artists": []}
                    return
                artist, title = reference
                tracks = []
                for item in get_similar_tracks(artist, title, limit=10):
                    try:
                        track_obj = item.item
                        track_artist = str(track_obj.artist)
                        track_title = str(track_obj.title)
                        match_score = float(item.match)
                    except Exception:
                        continue
                    local_path = find_local_track(track_artist, track_title)
                    cover_url = None
                    if local_path:
                        cover_url = read_metadata(local_path).get("cover")
                    if not cover_url:
                        cover_url = get_cover_from_cache_only(
                            track_artist, track_title)
                    tracks.append({
                        "artist": track_artist,
                        "title": track_title,
                        "match": match_score,
                        "local": str(local_path) if local_path else None,
                        "cover": cover_url,
                    })
                artists = []
                for item in get_similar_artists(artist, limit=8):
                    try:
                        artists.append({
                            "name": str(item.item),
                            "match": float(item.match),
                        })
                    except Exception:
                        continue
                rec_cache["key"] = rec_cache_key(artist, title)
                rec_cache["data"] = {"reference": (artist, title),
                                     "tracks": tracks, "artists": artists}
            except Exception as exc:
                rec_cache["data"] = None
                rec_cache["error"] = str(exc)
            finally:
                rec_cache["computing"] = False
            schedule_ui_update(
                lambda: show_recommendations()
                if current_section == "recommendations" else None
            )

        threading.Thread(target=worker, daemon=True).start()

    def render_recommendations_from_data(data):
        clear_rows()
        reference = data.get("reference")
        if not reference:
            return ft.Column([
                ft.Text("Недостаточно данных.", color="grey", size=14)])
        artist, title = reference
        sections = []
        sections.append(ft.Container(
            padding=ft.padding.symmetric(vertical=10),
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.RECOMMEND, color=GREEN, size=28),
                    ft.Column(controls=[
                        ft.Text("Похоже на:", color="grey", size=12),
                        ft.Text(f"{artist} — {title}", color="white",
                                size=16, weight=ft.FontWeight.BOLD),
                    ], spacing=2),
                ],
                spacing=12)))
        tracks = data.get("tracks") or []
        if tracks:
            track_rows_widgets = []
            for row in tracks:
                track_artist = row["artist"]
                track_title = row["title"]
                match_score = row["match"]
                local_path = Path(row["local"]) if row["local"] else None
                cover_url = row["cover"]
                if cover_url:
                    cover_widget = ft.Image(src=cover_url, width=52, height=52,
                                            fit=ft.ImageFit.COVER,
                                            border_radius=4)
                else:
                    cover_widget = ft.Container(
                        width=52, height=52, bgcolor="#2a2a2a",
                        border_radius=4, alignment=ft.alignment.center,
                        content=ft.Icon(ft.Icons.MUSIC_NOTE,
                                        color="grey", size=22))
                if cover_url is None:
                    if local_path:
                        subscribe_cover(
                            str(local_path),
                            lambda cp, c=cover_widget:
                                _apply_cover_to_placeholder(c, cp, 52, 4))
                    else:
                        subscribe_cover(
                            remote_cover_key(track_artist, track_title),
                            lambda cp, c=cover_widget:
                                _apply_cover_to_placeholder(c, cp, 52, 4))
                        request_remote_cover(track_artist, track_title)
                trailing_controls = []
                if local_path:
                    trailing_controls.append(
                        ft.IconButton(
                            ft.Icons.PLAYLIST_ADD,
                            icon_color=GREEN, icon_size=20,
                            tooltip="Добавить в плейлист",
                            on_click=lambda e, p=str(local_path):
                                show_add_to_playlist_panel(p)))
                    trailing_controls.append(
                        ft.IconButton(
                            ft.Icons.FAVORITE if is_liked(str(local_path))
                            else ft.Icons.FAVORITE_BORDER,
                            icon_color=GREEN if is_liked(str(local_path)) else "grey",
                            icon_size=20, tooltip="Нравится",
                            on_click=lambda e, p=str(local_path): toggle_like(p),
                        ))
                    heart_widgets.setdefault(str(local_path), []).append(
                        trailing_controls[-1])
                else:
                    trailing_controls.append(
                        ft.Container(
                            padding=ft.padding.symmetric(horizontal=8),
                            content=ft.Text("нет в библиотеке",
                                            color="grey", size=10)))

                def on_click_track(e, p=local_path):
                    if p:
                        play_track(p)

                tile = ft.Container(
                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    border_radius=6, ink=bool(local_path),
                    on_click=on_click_track if local_path else None,
                    content=ft.Row(
                        controls=[
                            cover_widget,
                            ft.Column(controls=[
                                ft.Text(track_title, color="white",
                                        size=14, max_lines=1),
                                ft.Text(
                                    f"{track_artist} · {int(match_score * 100)}%",
                                    color="grey", size=11),
                            ], spacing=2, expand=True),
                            *trailing_controls,
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER))
                track_rows_widgets.append(tile)
            sections.append(ft.Column(
                controls=[
                    ft.Text("Похожие треки", color="white", size=18,
                            weight=ft.FontWeight.BOLD),
                    ft.Column(track_rows_widgets, spacing=4),
                ], spacing=8))
        artists = data.get("artists") or []
        if artists:
            artist_cards = []
            for row in artists:
                artist_name = row["name"]
                match_score = row["match"]
                photo = get_artist_photo_cache_only(artist_name)
                if photo:
                    avatar = ft.Container(
                        width=44, height=44, border_radius=22,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        content=ft.Image(src=photo, width=44, height=44,
                                         fit=ft.ImageFit.COVER))
                else:
                    avatar = ft.Container(
                        width=44, height=44,
                        bgcolor=artist_color(artist_name), border_radius=22,
                        alignment=ft.alignment.center,
                        content=ft.Text(artist_name[:1].upper(),
                                        color="white", size=18,
                                        weight=ft.FontWeight.BOLD))
                    subscribe_cover(
                        artist_photo_key(artist_name),
                        lambda cp, c=avatar:
                            _apply_cover_to_placeholder(c, cp, 44, 22))
                    request_artist_photo(artist_name)
                artist_cards.append(ft.Container(
                    padding=ft.padding.symmetric(horizontal=8, vertical=4),
                    border_radius=6,
                    content=ft.Row(
                        controls=[
                            avatar,
                            ft.Column(controls=[
                                ft.Text(artist_name, color="white",
                                        size=14, max_lines=1),
                                ft.Text(f"Совпадение: {int(match_score * 100)}%",
                                        color="grey", size=11),
                            ], spacing=2, expand=True),
                        ],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER)))
            sections.append(ft.Column(
                controls=[
                    ft.Text("Похожие исполнители", color="white", size=18,
                            weight=ft.FontWeight.BOLD),
                    ft.Column(artist_cards, spacing=4),
                ], spacing=8))
        return ft.Column(controls=sections, spacing=20,
                         scroll=ft.ScrollMode.AUTO, expand=True)

    def build_recommendations_content():
        if not lastfm_network:
            clear_rows()
            return ft.Column([
                ft.Icon(ft.Icons.INFO_OUTLINE, color="grey", size=48),
                ft.Text("Last.fm не настроен", color="white", size=20,
                        weight=ft.FontWeight.BOLD),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=12)
        data = rec_cache["data"]
        if data is not None and not rec_cache["computing"]:
            quick = rec_reference_quick()
            if quick is None or rec_cache["key"] == rec_cache_key(quick[0], quick[1]):
                return render_recommendations_from_data(data)
        if rec_cache["error"] and not rec_cache["computing"]:
            clear_rows()
            start_recommendations_compute()
            return ft.Column([
                ft.Icon(ft.Icons.ERROR_OUTLINE, color="#ff6b6b", size=48),
                ft.Text("Не удалось получить рекомендации",
                        color="white", size=16),
                ft.Text(rec_cache["error"], color="grey", size=12),
                ft.Text("Пробую обновить автоматически...",
                        color="grey", size=12),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=10)
        clear_rows()
        start_recommendations_compute()
        return ft.Column(
            controls=[
                ft.Container(
                    padding=30,
                    content=ft.Column(
                        controls=[
                            ft.ProgressRing(color=GREEN),
                            ft.Text("Подбираю рекомендации...",
                                    color="white", size=14),
                            ft.Text("Спрашиваю Last.fm о похожих треках",
                                    color="grey", size=12),
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=12)),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
            expand=True,
        )
    def on_refresh_recommendations(e):
        rec_cache["data"] = None
        rec_cache["key"] = None
        rec_cache["error"] = None
        show_recommendations()
    def show_recommendations(e=None):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "recommendations"
        current_playlist_name = None
        search_query = ""
        search_field.value = ""
        search_field.visible = False
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить рекомендации"
        refresh_button.on_click = on_refresh_recommendations
        view_title_text.value = "Рекомендации"
        body_container.content = build_recommendations_content()
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        page.update()

    # ============================================================
    #              ВКЛАДКА "СТАТИСТИКА"
    # ============================================================
    def stat_card(icon, value, label):
        return ft.Container(
            width=200, padding=20, bgcolor="#1a1a1a", border_radius=12,
            content=ft.Column(
                controls=[
                    ft.Icon(icon, color=GREEN, size=32),
                    ft.Text(value, color="white", size=26,
                            weight=ft.FontWeight.BOLD),
                    ft.Text(label, color="grey", size=12),
                ],
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    def top_tracks_section():
        rows = get_top_tracks(limit=10)
        if not rows:
            return ft.Text("Пока нет прослушанных треков.", color="grey", size=13)
        items = []
        for idx, (path, title, artist, cnt, total_ms) in enumerate(rows, start=1):
            p = Path(path)
            cover_url = None
            if p.exists():
                cover_url = read_metadata(p).get("cover")
            if cover_url:
                cover_widget = ft.Image(src=cover_url, width=44, height=44,
                                        fit=ft.ImageFit.COVER, border_radius=4)
            else:
                cover_widget = ft.Container(
                    width=44, height=44, bgcolor="#2a2a2a", border_radius=4,
                    alignment=ft.alignment.center,
                    content=ft.Icon(ft.Icons.MUSIC_NOTE, color="grey", size=20))

            def on_click_item(e, p=p):
                if p.exists():
                    play_track(p)

            items.append(ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                border_radius=6, ink=p.exists(),
                on_click=on_click_item if p.exists() else None,
                content=ft.Row(
                    controls=[
                        ft.Container(width=28, content=ft.Text(f"{idx}", color=GREEN,
                                                               size=16,
                                                               weight=ft.FontWeight.BOLD)),
                        cover_widget,
                        ft.Column(controls=[
                            ft.Text(title or "?", color="white",
                                    size=14, max_lines=1),
                            ft.Text(artist or "?", color="grey",
                                    size=11, max_lines=1),
                        ], spacing=2, expand=True),
                        ft.Column(controls=[
                            ft.Text(f"{cnt} раз", color=GREEN, size=13,
                                    weight=ft.FontWeight.BOLD),
                            ft.Text(format_duration_human(total_ms),
                                    color="grey", size=10),
                        ], spacing=2,
                            horizontal_alignment=ft.CrossAxisAlignment.END),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)))
        return ft.Column(items, spacing=4)

    def top_artists_section():
        rows = get_top_artists(limit=10)
        if not rows:
            return ft.Text("Пока нет данных.", color="grey", size=13)
        items = []
        for idx, (artist, cnt, total_ms) in enumerate(rows, start=1):
            photo = get_artist_photo_cache_only(artist)
            if photo:
                avatar = ft.Container(
                    width=44, height=44, border_radius=22,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    content=ft.Image(src=photo, width=44, height=44,
                                     fit=ft.ImageFit.COVER))
            else:
                avatar = ft.Container(
                    width=44, height=44, bgcolor=artist_color(artist),
                    border_radius=22, alignment=ft.alignment.center,
                    content=ft.Text(artist[:1].upper() if artist else "?",
                                    color="white", size=18,
                                    weight=ft.FontWeight.BOLD))

            subscribe_cover(artist_photo_key(artist), lambda cp, c=avatar: _apply_cover_to_placeholder(c, cp, 44, 22))
            request_artist_photo(artist)
            items.append(ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                border_radius=6,
                content=ft.Row(
                    controls=[
                        ft.Container(width=28, content=ft.Text(f"{idx}", color=GREEN,
                                                               size=16,
                                                               weight=ft.FontWeight.BOLD)),
                        avatar,
                        ft.Column(controls=[
                            ft.Text(artist or "?", color="white",
                                    size=14, max_lines=1),
                        ], spacing=2, expand=True),
                        ft.Column(controls=[
                            ft.Text(f"{cnt} раз", color=GREEN, size=13,
                                    weight=ft.FontWeight.BOLD),
                            ft.Text(format_duration_human(total_ms),
                                    color="grey", size=10),
                        ], spacing=2,
                            horizontal_alignment=ft.CrossAxisAlignment.END),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)))
        return ft.Column(items, spacing=4)

    def top_albums_section():
        rows = get_top_albums(limit=10)
        if not rows:
            return ft.Text("Пока нет данных.", color="grey", size=13)
        items = []
        for idx, (album, artist, cnt) in enumerate(rows, start=1):
            items.append(ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                border_radius=6,
                content=ft.Row(
                    controls=[
                        ft.Container(width=28, content=ft.Text(f"{idx}", color=GREEN,
                                                               size=16,
                                                               weight=ft.FontWeight.BOLD)),
                        ft.Icon(ft.Icons.ALBUM, color=GREEN, size=24),
                        ft.Column(controls=[
                            ft.Text(album or "?", color="white",
                                    size=14, max_lines=1),
                            ft.Text(artist or "?", color="grey",
                                    size=11, max_lines=1),
                        ], spacing=2, expand=True),
                        ft.Text(f"{cnt} раз", color=GREEN, size=13,
                                weight=ft.FontWeight.BOLD),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)))
        return ft.Column(items, spacing=4)

    def recent_plays_section():
        rows = get_recent_plays(limit=20)
        if not rows:
            return ft.Text("Пока нет истории.", color="grey", size=13)
        items = []
        for path, title, artist, ts, skipped in rows:
            p = Path(path)
            cover_url = None
            if p.exists():
                cover_url = read_metadata(p).get("cover")
            if cover_url:
                cover_widget = ft.Image(src=cover_url, width=40, height=40,
                                        fit=ft.ImageFit.COVER, border_radius=4)
            else:
                cover_widget = ft.Container(
                    width=40, height=40, bgcolor="#2a2a2a", border_radius=4,
                    alignment=ft.alignment.center,
                    content=ft.Icon(ft.Icons.MUSIC_NOTE, color="grey", size=18))
            skip_icon = ft.Icon(
                ft.Icons.SKIP_NEXT if skipped else ft.Icons.CHECK,
                color="#ff6b6b" if skipped else GREEN, size=16,
                tooltip="Скип" if skipped else "Дослушан")

            def on_click_item(e, p=p):
                if p.exists():
                    play_track(p)

            items.append(ft.Container(
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                border_radius=6, ink=p.exists(),
                on_click=on_click_item if p.exists() else None,
                content=ft.Row(
                    controls=[
                        cover_widget,
                        ft.Column(controls=[
                            ft.Text(title or "?", color="white",
                                    size=13, max_lines=1),
                            ft.Text(artist or "?", color="grey",
                                    size=11, max_lines=1),
                        ], spacing=2, expand=True),
                        ft.Column(controls=[
                            ft.Text(format_relative_time(ts),
                                    color="grey", size=11),
                            skip_icon,
                        ], spacing=2,
                            horizontal_alignment=ft.CrossAxisAlignment.END),
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER)))
        return ft.Column(items, spacing=4)

    def build_statistics_content():
        summary = get_summary()
        stats_row = ft.Row(
            controls=[
                stat_card(ft.Icons.PLAY_CIRCLE, str(summary["total_plays"]),
                          "Всего прослушиваний"),
                stat_card(ft.Icons.MUSIC_NOTE, str(summary["unique_tracks"]),
                          "Уникальных треков"),
                stat_card(ft.Icons.PERSON, str(summary["unique_artists"]),
                          "Исполнителей"),
                stat_card(ft.Icons.ACCESS_TIME,
                          format_duration_human(summary["total_ms"]),
                          "Всего прослушано"),
            ],
            spacing=12, wrap=True)

        sections = [
            ft.Container(content=stats_row,
                         padding=ft.padding.symmetric(vertical=10)),
            ft.Divider(color="#333333"),
            ft.Text("Топ-10 треков", color="white", size=20,
                    weight=ft.FontWeight.BOLD),
            top_tracks_section(),
            ft.Divider(color="#333333"),
            ft.Text("Топ-10 исполнителей", color="white", size=20,
                    weight=ft.FontWeight.BOLD),
            top_artists_section(),
            ft.Divider(color="#333333"),
            ft.Text("Топ-10 альбомов", color="white", size=20,
                    weight=ft.FontWeight.BOLD),
            top_albums_section(),
            ft.Divider(color="#333333"),
            ft.Text("Недавние прослушивания", color="white", size=20,
                    weight=ft.FontWeight.BOLD),
            recent_plays_section(),
        ]
        return ft.Column(controls=sections, spacing=14,
                         scroll=ft.ScrollMode.AUTO, expand=True)

    def on_refresh_statistics(e):
        body_container.content = build_statistics_content()
        page.update()

    def show_statistics(e=None):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "statistics"
        current_playlist_name = None
        search_query = ""
        search_field.value = ""
        search_field.visible = False
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.DELETE_OUTLINE
        refresh_button.icon_color = "#ff6b6b"
        refresh_button.tooltip = "Сбросить статистику"
        refresh_button.on_click = lambda e: show_reset_stats_panel()
        view_title_text.value = "Статистика"
        body_container.content = build_statistics_content()
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        page.update()

    # ============================================================
    #              ВКЛАДКА "СИНХРОНИЗАЦИЯ"
    # ============================================================
    def reset_sync_plan_cache():
        sync_plan_cache["drive"] = None
        sync_plan_cache["plan"] = None
        sync_plan_cache["error"] = None

    def sync_plan_is_ready(drive):
        return (
            sync_plan_cache["drive"] == str(drive)
            and sync_plan_cache["plan"] is not None
            and not sync_plan_cache["computing"]
        )

    def start_sync_plan_compute(drive):
        if sync_plan_cache["computing"]:
            return
        sync_plan_cache["computing"] = True
        sync_plan_cache["error"] = None

        def worker():
            try:
                if not sync_plan_is_ready(drive):
                    if sync_plan_cache["error"]:
                        sections.append(ft.Container(
                            padding=12, bgcolor="#3a1f1f", border_radius=8,
                            content=ft.Row(controls=[
                                ft.Icon(ft.Icons.ERROR_OUTLINE,
                                    color="#ff6b6b", size=24),
                                ft.Text(
                                    f"Ошибка расчёта плана: {sync_plan_cache['error']}",
                                    color="#ff6b6b", size=12, expand=True),
                                ], spacing=10)))
                    else:
                        sections.append(ft.Container(
                            padding=30,
                            content=ft.Column(controls=[
                                ft.ProgressRing(color=GREEN),
                                ft.Text("Считаю план синхронизации...",
                                    color="white", size=14),
                                ft.Text("Сканирую библиотеку и флешку, подожди",
                                    color="grey", size=12),
                                ],
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                    spacing=12)))
                        start_sync_plan_compute(drive)
                    return ft.Column(controls=sections, spacing=14,
                                 scroll=ft.ScrollMode.AUTO, expand=True)
                plan = sync_plan_cache["plan"]
                sync_plan_cache["drive"] = str(drive)
                sync_plan_cache["plan"] = plan
            except Exception as exc:
                sync_plan_cache["plan"] = None
                sync_plan_cache["error"] = str(exc)
            finally:
                sync_plan_cache["computing"] = False
            schedule_ui_update(
                lambda: show_sync() if current_section == "sync" else None
            )

        threading.Thread(target=worker, daemon=True).start()

    def build_sync_content():
        nonlocal selected_drive
        clear_rows()
        drives = list_removable_drives()

        sections = []

        sections.append(ft.Container(
            padding=ft.padding.symmetric(vertical=10),
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.SYNC, color=GREEN, size=28),
                    ft.Column(controls=[
                        ft.Text("Синхронизация с флешкой", color="white",
                                size=18, weight=ft.FontWeight.BOLD),
                        ft.Text("Все треки в корень, по популярности "
                                "(сначала самые любимые)",
                                color="grey", size=12),
                    ], spacing=2),
                ],
                spacing=12)))

        if sync_in_progress:
            sections.append(ft.Container(
                padding=20, bgcolor="#1a1a1a", border_radius=10,
                content=ft.Column(
                    controls=[
                        ft.Text("Синхронизация...", color="white", size=16,
                                weight=ft.FontWeight.BOLD),
                        sync_progress_text,
                        sync_progress_bar,
                    ], spacing=10)))
            return ft.Column(controls=sections, spacing=14,
                             scroll=ft.ScrollMode.AUTO, expand=True)

        if not drives:
            sections.append(ft.Container(
                padding=40,
                content=ft.Column(
                    controls=[
                        ft.Icon(ft.Icons.USB_OFF, color="grey", size=64),
                        ft.Text("Флешки не обнаружены", color="white", size=16),
                        ft.Text("Вставь флешку и нажми ⟳",
                                color="grey", size=12),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12)))
        elif selected_drive is None or not selected_drive.exists():
            selected_drive = None
            cards = []
            for d in drives:
                try:
                    usage = shutil.disk_usage(str(d))
                    free = usage.free
                    total = usage.total
                except Exception:
                    free = 0
                    total = 0

                def on_drive_click(e, dd=d):
                    nonlocal selected_drive
                    selected_drive = dd
                    reset_sync_plan_cache()
                    show_sync()

                cards.append(ft.Container(
                    padding=16, bgcolor="#1a1a1a", border_radius=10, ink=True,
                    on_click=on_drive_click,
                    content=ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.USB, color=GREEN, size=36),
                            ft.Column(controls=[
                                ft.Text(d.name, color="white", size=15,
                                        weight=ft.FontWeight.BOLD),
                                ft.Text(
                                    f"{human_size(free)} свободно из {human_size(total)}",
                                    color="grey", size=11),
                                ft.Text(str(d), color="grey", size=10),
                            ], spacing=2, expand=True),
                            ft.Icon(ft.Icons.CHEVRON_RIGHT, color="grey", size=24),
                        ],
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER)))
            sections.append(ft.Column(cards, spacing=8))
        else:
            drive = selected_drive

            def on_back(e):
                nonlocal selected_drive
                selected_drive = None
                reset_sync_plan_cache()
                show_sync()

            sections.append(ft.Row(controls=[
                ft.TextButton("← Все флешки", on_click=on_back,
                              style=ft.ButtonStyle(color="white")),
            ]))

            plan = compute_sync_plan(drive)

            try:
                usage = shutil.disk_usage(str(drive))
                free = usage.free
                total = usage.total
            except Exception:
                free = 0
                total = 0

            sections.append(ft.Container(
                padding=20, bgcolor="#1a1a1a", border_radius=10,
                content=ft.Column(
                    controls=[
                        ft.Row(controls=[
                            ft.Icon(ft.Icons.USB, color=GREEN, size=40),
                            ft.Column(controls=[
                                ft.Text(drive.name, color="white", size=18,
                                        weight=ft.FontWeight.BOLD),
                                ft.Text(
                                    f"{human_size(free)} свободно из {human_size(total)}",
                                    color="grey", size=12),
                            ], spacing=2),
                        ], spacing=12),
                        ft.Divider(color="#333333"),
                        ft.Row(controls=[
                            ft.Column(controls=[
                                ft.Text(str(plan["library_count"]), color="white",
                                        size=22, weight=ft.FontWeight.BOLD),
                                ft.Text("треков в библиотеке",
                                        color="grey", size=11),
                            ], spacing=2, expand=True),
                            ft.Column(controls=[
                                ft.Text(str(len(plan["to_copy"])), color=GREEN,
                                        size=22, weight=ft.FontWeight.BOLD),
                                ft.Text("нужно скопировать",
                                        color="grey", size=11),
                            ], spacing=2, expand=True),
                            ft.Column(controls=[
                                ft.Text(str(len(plan["to_delete"])),
                                        color="#ff6b6b" if plan["to_delete"] else "grey",
                                        size=22, weight=ft.FontWeight.BOLD),
                                ft.Text("лишних на флешке",
                                        color="grey", size=11),
                            ], spacing=2, expand=True),
                        ], spacing=12),
                    ], spacing=12)))

            total_needed = plan["total_copy_size"]
            if total_needed > 0:
                if plan["free_space"] < total_needed:
                    sections.append(ft.Container(
                        padding=12, bgcolor="#3a1f1f", border_radius=8,
                        content=ft.Row(controls=[
                            ft.Icon(ft.Icons.ERROR_OUTLINE, color="#ff6b6b", size=24),
                            ft.Text(
                                f"Не хватает места! Нужно {human_size(total_needed)}, "
                                f"свободно {human_size(plan['free_space'])}.",
                                color="#ff6b6b", size=12, expand=True),
                        ], spacing=10)))

            if plan["to_copy"] and not (plan["free_space"] < total_needed):
                preview_items = []
                for rel, src, size in plan["to_copy"][:15]:
                    preview_items.append(ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=4),
                        content=ft.Row(controls=[
                            ft.Icon(ft.Icons.CONTENT_COPY, color=GREEN, size=16),
                            ft.Text(rel, color="white", size=11, expand=True,
                                    max_lines=1),
                            ft.Text(human_size(size), color="grey", size=10),
                        ], spacing=8)))
                if len(plan["to_copy"]) > 15:
                    preview_items.append(ft.Text(
                        f"...и ещё {len(plan['to_copy']) - 15}",
                        color="grey", size=11))

                sections.append(ft.Container(
                    padding=16, bgcolor="#1a1a1a", border_radius=10,
                    content=ft.Column(
                        controls=[
                            ft.Text(f"Будет скопировано ({len(plan['to_copy'])})",
                                    color="white", size=14,
                                    weight=ft.FontWeight.BOLD),
                            ft.Column(preview_items, spacing=2,
                                      scroll=ft.ScrollMode.AUTO, height=200),
                        ], spacing=8)))

            if plan["to_delete"]:
                preview_del = []
                for rel in plan["to_delete"][:15]:
                    preview_del.append(ft.Container(
                        padding=ft.padding.symmetric(horizontal=8, vertical=4),
                        content=ft.Row(controls=[
                            ft.Icon(ft.Icons.DELETE_OUTLINE, color="#ff6b6b",
                                    size=16),
                            ft.Text(rel, color="white", size=11, expand=True,
                                    max_lines=1),
                        ], spacing=8)))
                if len(plan["to_delete"]) > 15:
                    preview_del.append(ft.Text(
                        f"...и ещё {len(plan['to_delete']) - 15}",
                        color="grey", size=11))

                sections.append(ft.Container(
                    padding=16, bgcolor="#2a1a1a", border_radius=10,
                    content=ft.Column(
                        controls=[
                            ft.Text(
                                f"Лишние на флешке ({len(plan['to_delete'])}) — будут удалены:",
                                color="#ff6b6b", size=14,
                                weight=ft.FontWeight.BOLD),
                            ft.Column(preview_del, spacing=2,
                                      scroll=ft.ScrollMode.AUTO, height=200),
                        ], spacing=8)))

            if not plan["to_copy"] and not plan["to_delete"]:
                sections.append(ft.Container(
                    padding=30,
                    content=ft.Column(controls=[
                        ft.Icon(ft.Icons.CHECK_CIRCLE, color=GREEN, size=64),
                        ft.Text("Всё синхронизировано!", color="white",
                                size=16, weight=ft.FontWeight.BOLD),
                        ft.Text("Флешка полностью соответствует библиотеке.",
                                color="grey", size=12),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12)))

            if plan["library_count"] >= 10 and (plan["to_copy"] or plan["to_delete"]):
                def on_sync_click(e):
                    nonlocal selected_drive
                    if selected_drive:
                        page.run_task(do_sync, selected_drive)

                sections.append(ft.Container(
                    padding=ft.padding.symmetric(vertical=10),
                    content=ft.Row(controls=[
                        ft.Container(expand=True),
                        ft.ElevatedButton(
                            "Синхронизировать",
                            bgcolor=GREEN, color="black", height=48,
                            on_click=on_sync_click),
                    ])))
            elif plan["library_count"] < 10:
                sections.append(ft.Container(
                    padding=12, bgcolor="#3a2f1f", border_radius=8,
                    content=ft.Text(
                        "В библиотеке меньше 10 треков. Синхронизация отключена "
                        "для защиты от случайного удаления.",
                        color="#ffa94d", size=12)))

        return ft.Column(controls=sections, spacing=14,
                         scroll=ft.ScrollMode.AUTO, expand=True)

    async def do_sync(drive):
        nonlocal sync_in_progress, selected_drive
        sync_in_progress = True
        show_sync()
        await asyncio.sleep(0.05)

        plan = await asyncio.to_thread(compute_sync_plan, drive)
        to_copy = plan["to_copy"]
        to_delete = plan["to_delete"]
        manifest = plan["manifest"]

        total_ops = max(1, len(to_copy) + len(to_delete))
        done_ops = 0

        try:
            for rel in to_delete:
                try:
                    target = drive / rel
                    if target.exists():
                        await asyncio.to_thread(target.unlink)
                    if rel in manifest:
                        del manifest[rel]
                except Exception as e:
                    print(f"Ошибка удаления {rel}: {e}")
                done_ops += 1
                pct = min(0.99, max(0.0, done_ops / total_ops))
                sync_progress_bar.value = pct
                sync_progress_text.value = f"Удалено {done_ops} из {total_ops}: {rel}"
                page.update()
                await asyncio.sleep(0)

            for rel, src, size in to_copy:
                try:
                    target = drive / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.copy2, str(src), str(target))
                    st = src.stat()
                    manifest[rel] = {"size": st.st_size, "mtime": int(st.st_mtime)}
                except Exception as e:
                    print(f"Ошибка копирования {rel}: {e}")
                done_ops += 1
                pct = min(0.99, max(0.0, done_ops / total_ops))
                sync_progress_bar.value = pct
                sync_progress_text.value = (f"Скопировано {done_ops} из {total_ops}: "
                                            f"{rel}")
                page.update()
                await asyncio.sleep(0)

            save_sync_manifest(drive, manifest)

            sync_progress_text.value = "Готово!"
            sync_progress_bar.value = 1.0
            page.update()
            await asyncio.sleep(1.5)

        except Exception as e:
            print(f"Ошибка синхронизации: {e}")
            sync_progress_text.value = f"Ошибка: {e}"
            page.update()
            await asyncio.sleep(2)

        sync_in_progress = False
        reset_sync_plan_cache()
        show_sync()

    def on_refresh_sync(e):
        reset_sync_plan_cache()
        show_sync()

    def show_sync(e=None):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "sync"
        current_playlist_name = None
        search_query = ""
        search_field.value = ""
        search_field.visible = False
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Обновить список флешек"
        refresh_button.on_click = on_refresh_sync
        view_title_text.value = "Синхронизация"
        body_container.content = build_sync_content()
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        page.update()

    # ============================================================
    #              ВКЛАДКА "СКАЧАТЬ МУЗЫКУ"
    # ============================================================
    download_url_field = ft.TextField(
        hint_text="Ссылка на YouTube / YouTube Music или название песни...",
        bgcolor="#2a2a2a",
        color="white",
        cursor_color=GREEN,
        border_color="#333333",
        focused_border_color=GREEN,
        expand=True,
        text_size=14,
    )

    download_progress_bar = ft.ProgressBar(value=0, color=GREEN,
                                           bgcolor="#333333")
    download_progress_text = ft.Text("", color="grey", size=12)

    download_status_icon = ft.Icon(ft.Icons.MUSIC_NOTE, color=GREEN, size=64)
    download_status_title = ft.Text("Готов к скачиванию",
                                     color="white", size=18,
                                     weight=ft.FontWeight.BOLD)
    download_status_subtitle = ft.Text(
        "Вставь ссылку или напиши название песни",
        color="grey", size=13, text_align=ft.TextAlign.CENTER)

    download_status_card = ft.Container(
        padding=30,
        bgcolor="#1a1a1a",
        border_radius=12,
        content=ft.Column(
            controls=[
                download_status_icon,
                download_status_title,
                download_status_subtitle,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=10,
        ),
    )

    download_log_column = ft.Column(
        controls=[],
        spacing=4,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )

    download_button = ft.ElevatedButton(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.DOWNLOAD, color="black", size=22),
                ft.Text("Скачать", color="black",
                        weight=ft.FontWeight.BOLD, size=15),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        bgcolor=GREEN,
        color="black",
        height=48,
    )

    process_button = ft.ElevatedButton(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.AUTO_FIX_HIGH, color="black", size=21),
                ft.Text(
                    "Обработать без обложки",
                    color="black",
                    weight=ft.FontWeight.BOLD,
                    size=14,
                ),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        bgcolor="#ffa94d",
        color="black",
        height=44,
    )

    stop_process_button = ft.ElevatedButton(
        content=ft.Row(
            controls=[
                ft.Icon(ft.Icons.STOP, color="white", size=20),
                ft.Text(
                    "Остановить обработку",
                    color="white",
                    weight=ft.FontWeight.BOLD,
                    size=14,
                ),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            spacing=8,
        ),
        bgcolor="#c0392b",
        color="white",
        height=44,
        visible=False,
    )

    process_last_ui_update = 0.0
    ui_event_queue = queue.Queue()

    def schedule_ui_update(callback):
        # Этот метод может вызываться из фонового потока. В нём нельзя
        # обращаться к Flet напрямую — только передать событие UI-потоку.
        ui_event_queue.put(callback)

    async def ui_event_pump():
        while True:
            handled = False
            while True:
                try:
                    callback = ui_event_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    callback()
                    handled = True
                except Exception:
                    pass

            if handled:
                try:
                    page.update()
                except Exception:
                    pass
            await asyncio.sleep(0.2)

    def append_log_item(text, color="white"):
        download_log_items.append((text, color))
        if len(download_log_items) > 200:
            del download_log_items[:len(download_log_items) - 200]
        download_log_column.controls = [
            ft.Text(t, color=c, size=11, selectable=True)
            for t, c in download_log_items
        ]

    def log_add(text, color="white"):
        schedule_ui_update(lambda: append_log_item(text, color))

    def log_clear():
        download_log_items.clear()
        download_log_column.controls = []

    def apply_status(state: str, title: str = "", subtitle: str = ""):
        if state == "idle":
            download_status_icon.name = ft.Icons.MUSIC_NOTE
            download_status_icon.color = GREEN
            download_status_title.value = title or "Готов к скачиванию"
            download_status_title.color = "white"
            download_status_subtitle.value = (subtitle or
                "Вставь ссылку или напиши название песни")
            download_status_subtitle.color = "grey"
        elif state == "downloading":
            download_status_icon.name = ft.Icons.DOWNLOAD
            download_status_icon.color = "#1DB954"
            download_status_title.value = title or "Скачивание..."
            download_status_title.color = "white"
            download_status_subtitle.value = subtitle or "Подожди немного"
            download_status_subtitle.color = "grey"
        elif state == "processing":
            download_status_icon.name = ft.Icons.AUTO_FIX_HIGH
            download_status_icon.color = "#ffa94d"
            download_status_title.value = title or "Обработка файлов..."
            download_status_title.color = "#ffa94d"
            download_status_subtitle.value = subtitle or "Подожди немного"
            download_status_subtitle.color = "grey"
        elif state == "success":
            download_status_icon.name = ft.Icons.CHECK_CIRCLE
            download_status_icon.color = GREEN
            download_status_title.value = title or "Скачано!"
            download_status_title.color = GREEN
            download_status_subtitle.value = subtitle or ""
            download_status_subtitle.color = "white"
        elif state == "error":
            download_status_icon.name = ft.Icons.CANCEL
            download_status_icon.color = "#ff6b6b"
            download_status_title.value = title or "Ошибка"
            download_status_title.color = "#ff6b6b"
            download_status_subtitle.value = subtitle or ""
            download_status_subtitle.color = "grey"
        elif state == "stopped":
            download_status_icon.name = ft.Icons.STOP
            download_status_icon.color = "#ffa94d"
            download_status_title.value = title or "Обработка остановлена"
            download_status_title.color = "#ffa94d"
            download_status_subtitle.value = subtitle or ""
            download_status_subtitle.color = "white"

    def set_status(state: str, title: str = "", subtitle: str = ""):
        schedule_ui_update(
            lambda: apply_status(state, title=title, subtitle=subtitle)
        )

    def on_download_progress(pct, downloaded, total):
        def update_progress():
            download_progress_bar.value = min(0.99, max(0.0, pct))
            download_progress_text.value = (
                f"{human_size(downloaded)} / {human_size(total)} "
                f"({int(pct * 100)}%)"
            )

        schedule_ui_update(update_progress)

    def on_download_done(success, title, error_msg):
        nonlocal download_in_progress
        download_in_progress = False

        def finish_download():
            download_button.disabled = False
            download_progress_bar.value = 1.0 if success else 0.0
            if success:
                _metadata_cache.clear()
                local_index_cache["index"] = None
                apply_status(
                    "success",
                    title="Скачано!",
                    subtitle=f"{title}\nТрек появился в библиотеке",
                )
                append_log_item(
                    "🔄 Кэш обновлён. Трек появился в библиотеке.",
                    "#1DB954",
                )
            else:
                apply_status(
                    "error",
                    title="Не удалось скачать",
                    subtitle=error_msg or "Проверь ссылку и попробуй снова",
                )

        schedule_ui_update(finish_download)

    def download_thread_worker(url_or_query):
        run_download(url_or_query,
                     on_progress=on_download_progress,
                     on_log=lambda msg: log_add(msg),
                     on_done=on_download_done)

    async def on_download_click(e):
        nonlocal download_in_progress
        if download_in_progress:
            log_add("⏳ Уже идёт скачивание, подожди...", "#ffa94d")
            return
        if process_in_progress:
            log_add("⏳ Сначала дождись окончания обработки файлов...", "#ffa94d")
            return
        url_or_query = (download_url_field.value or "").strip()
        if not url_or_query:
            set_status("error", title="Пустое поле",
                       subtitle="Введи ссылку или название песни")
            log_add("❌ Введи ссылку или название песни", "#ff6b6b")
            return

        download_in_progress = True
        download_button.disabled = True
        download_progress_bar.value = 0
        download_progress_text.value = ""
        log_clear()
        set_status("downloading",
                   title="Скачивание...",
                   subtitle=url_or_query[:80])
        log_add(f"▶ Начинаю: {url_or_query}")

        await asyncio.to_thread(download_thread_worker, url_or_query)

    wire_typing(download_url_field)
    download_button.on_click = on_download_click

    def on_process_progress(current, total, filename):
        nonlocal process_last_ui_update
        now = time.monotonic()
        if current < total and now - process_last_ui_update < 0.15:
            return
        process_last_ui_update = now

        def update_progress():
            pct = current / total if total else 0
            download_progress_bar.value = min(0.99, max(0.0, pct))
            download_progress_text.value = (
                f"{current} из {total}: {filename}"
            )

        schedule_ui_update(update_progress)

    def on_process_done(
        ok_count, no_cover_count, skipped_count, cancelled=False
    ):
        nonlocal process_in_progress
        process_in_progress = False

        def finish_processing():
            process_button.disabled = False
            process_button.visible = True
            stop_process_button.disabled = False
            stop_process_button.visible = False
            download_button.disabled = False
            _metadata_cache.clear()
            local_index_cache["index"] = None
            total = ok_count + no_cover_count + skipped_count
            download_progress_bar.value = 1.0 if total else 0.0
            download_progress_text.value = (
                (
                    f"Остановлено: {ok_count} обработано, "
                    f"{no_cover_count} без обложки, "
                    f"{skipped_count} пропущено"
                )
                if cancelled
                else (
                    f"Готово: {ok_count} обработано, "
                    f"{no_cover_count} без обложки, "
                    f"{skipped_count} пропущено"
                )
            )
            if cancelled:
                apply_status(
                    "stopped",
                    title="Обработка остановлена",
                    subtitle=(
                        f"Обработано: {ok_count}; "
                        f"без обложки: {no_cover_count}; "
                        f"пропущено: {skipped_count}"
                    ),
                )
            elif total == 0:
                apply_status(
                    "success",
                    title="Обработка не требуется",
                    subtitle="Все MP3 уже имеют локальную обложку",
                )
            else:
                apply_status(
                    "success",
                    title="Обработка завершена",
                    subtitle=(
                        f"Обработано: {ok_count}; "
                        f"без обложки: {no_cover_count}; "
                        f"пропущено: {skipped_count}"
                    ),
                )

        schedule_ui_update(finish_processing)

    def process_thread_worker():
        try:
            process_all_files(
                on_progress=on_process_progress,
                on_log=log_add,
                on_done=on_process_done,
                stop_event=process_stop_event,
            )
        except Exception as exc:
            log_add(f"❌ Критическая ошибка обработки: {exc}", "#ff6b6b")
            try:
                on_process_done(0, 0, 0, False)
            except Exception:
                pass

    def on_stop_process_click(e):
        if not process_in_progress:
            return
        process_stop_event.set()
        stop_process_button.disabled = True
        download_progress_text.value = "Останавливаем после текущего файла..."
        log_add("⏹ Запрошена остановка обработки...", "#ffa94d")
        page.update()

    stop_process_button.on_click = on_stop_process_click

    async def on_process_click(e):
        nonlocal process_in_progress
        if process_in_progress:
            log_add("⏳ Обработка уже идёт, подожди...", "#ffa94d")
            return
        if download_in_progress:
            log_add("⏳ Сначала дождись окончания скачивания...", "#ffa94d")
            return

        process_in_progress = True
        process_stop_event.clear()
        process_button.disabled = True
        process_button.visible = False
        stop_process_button.disabled = False
        stop_process_button.visible = True
        download_button.disabled = True
        download_progress_bar.value = 0
        download_progress_text.value = ""
        log_clear()
        set_status(
            "processing",
            title="Проверяю обложки...",
            subtitle="Ищу MP3 без встроенной или локальной обложки",
        )
        log_add("Ищу MP3 без встроенной или локальной обложки...")
        await asyncio.to_thread(process_thread_worker)

    process_button.on_click = on_process_click

    def build_download_content():
        return ft.Column(
            controls=[
                ft.Container(
                    padding=ft.padding.symmetric(vertical=10),
                    content=ft.Row(
                        controls=[
                            ft.Icon(ft.Icons.DOWNLOAD, color=GREEN, size=28),
                            ft.Column(controls=[
                                ft.Text("Скачать музыку", color="white",
                                        size=18, weight=ft.FontWeight.BOLD),
                                ft.Text(
                                    "Вставь ссылку на YouTube / YouTube Music "
                                    "или напиши название.",
                                    color="grey", size=12),
                            ], spacing=2),
                        ],
                        spacing=12,
                    ),
                ),
                download_status_card,
                ft.Container(
                    padding=16, bgcolor="#1a1a1a", border_radius=10,
                    content=ft.Column(
                        controls=[
                            ft.Row(controls=[
                                download_url_field,
                                download_button,
                            ], spacing=10,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER),
                            ft.Row(
                                controls=[process_button, stop_process_button],
                                alignment=ft.MainAxisAlignment.END,
                                spacing=8,
                            ),
                            download_progress_bar,
                            download_progress_text,
                        ], spacing=10),
                ),
                ft.Divider(color="#333333"),
                ft.Text("Лог", color="grey", size=11),
                ft.Container(
                    padding=12, bgcolor="#0d0d0d", border_radius=8,
                    height=180,
                    content=download_log_column,
                ),
            ],
            spacing=14,
            expand=True,
            scroll=ft.ScrollMode.AUTO,
        )

    def on_refresh_download(e):
        set_status("idle")

    def show_download(e=None):
        nonlocal current_section, current_playlist_name, search_query
        current_section = "download"
        current_playlist_name = None
        search_query = ""
        search_field.value = ""
        search_field.visible = False
        play_all_button.visible = False
        create_playlist_button.visible = False
        refresh_button.icon = ft.Icons.REFRESH
        refresh_button.icon_color = "white"
        refresh_button.tooltip = "Сбросить статус"
        refresh_button.on_click = on_refresh_download
        view_title_text.value = "Скачать музыку"
        body_container.content = build_download_content()
        content_area.content = ft.Column([
            header_row, ft.Divider(color="grey"), body_container,
        ], expand=True)
        page.update()

    # ---------- Заглушка ----------
    def show_stub(title):
        nonlocal current_section, current_playlist_name
        current_section = None
        current_playlist_name = None
        clear_rows()
        content_area.content = ft.Column([
            ft.Text(title, size=32, weight=ft.FontWeight.BOLD, color="white"),
            ft.Text("Здесь скоро что-то появится...", color="grey")])
        page.update()

    sidebar = ft.Container(
        width=220, bgcolor="#000000", padding=20,
        content=ft.Column(controls=[
            ft.Text("🎵 Моя Музыка", size=22,
                    weight=ft.FontWeight.BOLD, color="white"),
            ft.Divider(color="grey"),
            ft.TextButton("Все треки",
                          on_click=lambda e: show_all_tracks(reset_search=True)),
            ft.TextButton("Недавно добавленные",
                          on_click=lambda e: show_recent(reset_search=True)),
            ft.TextButton("Любимые", on_click=lambda e: show_liked(reset_search=True)),
            ft.TextButton("Исполнители",
                          on_click=lambda e: show_artists(reset_search=True)),
            ft.TextButton("Альбомы",
                          on_click=lambda e: show_albums(reset_search=True)),
            ft.TextButton("Плейлисты",
                          on_click=lambda e: show_playlists(reset_search=True)),
            ft.TextButton("Рекомендации", on_click=show_recommendations),
            ft.TextButton("Статистика", on_click=show_statistics),
            ft.TextButton("Синхронизация", on_click=show_sync),
            ft.TextButton("Скачать музыку", on_click=show_download),
        ], spacing=5))

    right_side_inner = ft.Column(
        controls=[content_area, player_bar], expand=True, spacing=0)

    main_fill = ft.Container(
        content=ft.Row(controls=[sidebar, right_side_inner],
                       expand=True, spacing=0),
        left=0, top=0, right=0, bottom=0)

    page.add(ft.Stack(
        controls=[main_fill, slide_backdrop, slide_panel],
        expand=True))
    async def cover_ready_pump():
        while True:
            items = take_ready_covers()
            if items:
                for key, cover in items:
                    for cb in _cover_subscribers.pop(key, []):
                        try:
                            cb(cover)
                        except Exception:
                            pass
                try:
                    page.update()
                except Exception:
                    pass
            await asyncio.sleep(1.0)

    last_key_handled = {"key": None, "ts": 0.0}
    
    def on_keyboard_event(e):
        if getattr(e, "ctrl", False) or getattr(e, "alt", False) or getattr(e, "meta", False):
            return
        if _is_typing():
            return
        try:
            fc = page.focused_control
            if fc is not None and not isinstance(fc, ft.TextField):
                fc.blur()
        except Exception:
            pass
        raw = (getattr(e, "key", "") or "").lower()
        aliases = {
            "space": "space", " ": "space",
            "left arrow": "left", "arrowleft": "left", "arrow left": "left",
            "right arrow": "right", "arrowright": "right", "arrow right": "right",
            "up arrow": "up", "arrowup": "up", "arrow up": "up",
            "down arrow": "down", "arrowdown": "down", "arrow down": "down",
            "l": "like", "л": "like",
            "e": "eq", "у": "eq",
        }
        action = aliases.get(raw)
        if action is None:
            return
        now = time.monotonic()
        if last_key_handled["key"] == raw and now - last_key_handled["ts"] < 0.15:
            return
        last_key_handled["key"] = raw
        last_key_handled["ts"] = now
        if getattr(e, "is_repeat", False) and action in ("space", "like"):
            return
        if action == "space":
            on_play_pause_click(None)
        elif action == "left":
            play_prev(None)
        elif action == "right":
            play_next(None)
        elif action == "up":
            bump_volume(0.05)
        elif action == "down":
            bump_volume(-0.05)
        elif action == "like":
            if currently_playing_path:
                try:
                    toggle_like(currently_playing_path)
                except NameError:
                    print("Лайки ещё не подключены: клавиша L временно не работает")
        elif action == "eq":            toggle_equalizer()    
    page.on_keyboard_event = on_keyboard_event
    page.run_task(ui_event_pump)
    start_cover_worker()
    page.run_task(cover_ready_pump)


ft.app(target=main, assets_dir="/")

