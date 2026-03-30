import yt_dlp

# ------- yt-dlp options -------
YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "skip_download": True,
    "extract_flat": False,
    "nocheckcertificate": True,
    "source_address": "0.0.0.0",
    "cookiefile": "/home/ubuntu/My-Music-Bot/cookies.txt",
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "android_vr", "tv"]
        }
    },
}

# YT-DLP options for multi-result search
YTDL_SEARCH_OPTS = {
    **YTDL_OPTS,
    "extract_flat": True,
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn"
}

# ------- Shared yt-dlp instances -------
ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
ytdl_search = yt_dlp.YoutubeDL(YTDL_SEARCH_OPTS)

# ------- Timeout / retry settings -------
INACTIVITY_TIMEOUT = 1800   # 30 minutes of no songs playing
ALONE_TIMEOUT = 60          # 1 minute if bot is alone in voice channel
RECONNECT_ATTEMPTS = 3
RECONNECT_DELAY = 2         # seconds between reconnect attempts
