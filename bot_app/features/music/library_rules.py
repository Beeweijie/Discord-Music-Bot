"""Portable track metadata and queue algorithms (no Discord or disk access)."""
from collections import OrderedDict
from urllib.parse import urlsplit, parse_qs

TRACK_FIELDS = ("input", "title", "requester_id", "requester_name", "is_url", "webpage_url", "thumbnail", "duration", "uploader")


def track_data(song):
    return {key: getattr(song, key) for key in TRACK_FIELDS}


def validate_track(data):
    """Reject corrupt metadata before it enters a queue or personal library."""
    if not isinstance(data, dict):
        raise ValueError("Invalid track metadata")
    track = {key: data.get(key) for key in TRACK_FIELDS}
    for key in ("input", "title", "requester_name"):
        if not isinstance(track[key], str) or len(track[key]) > 10000:
            raise ValueError("Invalid track text")
    if type(track["is_url"]) is not bool or type(track["requester_id"]) is not int:
        raise ValueError("Invalid track identity")
    if not track["is_url"] and any(c in track["input"] for c in ("/", "\\", ":")):
        raise ValueError("Invalid local track")
    for key in ("webpage_url", "thumbnail", "uploader"):
        if track[key] is not None and not isinstance(track[key], str):
            raise ValueError("Invalid track metadata")
    duration = track["duration"]
    if duration is not None and (type(duration) not in (int, float) or not 0 <= duration <= 10**9):
        raise ValueError("Invalid track duration")
    return track


def track_key(song):
    value = song.webpage_url or song.input or song.title
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}:
        video = url.path.strip("/") if host == "youtu.be" else (parse_qs(url.query).get("v") or [""])[0]
        if video:
            return "youtube:" + video  # Video IDs are case-sensitive.
    return value.strip() if song.is_url else value.strip().casefold()


def fair_queue(songs):
    """Round robin requesters, preserving each person's song order."""
    groups = OrderedDict()
    for song in songs:
        groups.setdefault(song.requester_id, []).append(song)
    result = []
    while groups:
        for user in list(groups):
            result.append(groups[user].pop(0))
            if not groups[user]:
                del groups[user]
    return result
