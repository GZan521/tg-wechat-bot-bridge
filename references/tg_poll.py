#!/usr/bin/env python3
"""Telegram poll client for a Telegram bot (hook-driven bridge).

Design borrowed from wechat-ilink-muse-bridge: two-phase delivery.
`poll` only fetches — it never advances the offset and never records
seen ids. The caller queues the batch durably, then calls `commit`
with the commit block from poll's output. A crash anywhere before
commit simply re-fetches the same batch next round: messages may be
delivered twice, they are never silently lost.

Commands:
  poll                      one getUpdates long-poll; prints one JSON
                            line {"msgs":[...], "commit":{"offset":N,
                            "ids":[...]}} or a status line:
                            NO_TOKEN / TOKEN_INVALID / POLL_ERROR:<x>
  commit                    stdin: {"offset":N, "ids":[...]}
                            records seen ids FIRST, then the offset
  send <chat_id> <key>      text on stdin; idempotent per key via the
                            sent ledger; refuses empty / >4096 chars /
                            leading "--" text
  send-photo <chat_id> <key> <file>
                            sendPhoto with the local image file;
                            optional caption on stdin (<=1024 chars);
                            same idempotency ledger as send
  send-video <chat_id> <key> <file>
                            sendVideo with the local video file;
                            optional caption on stdin; same ledger
  send-voice <chat_id> <key> <file>
                            sendVoice with the local voice file
                            (ogg/opus preferred); optional caption
                            on stdin; same ledger
  typing <chat_id>          sendChatAction=typing (best effort)
  download <file_id> <dest> fetch a file via getFile into dest
                            (worker retry path for media)
  health                    JSON status snapshot; exit 0/1/2

Media: poll also captures captions (folded into "text") and downloads
attachments into media/ at poll time — photo, document, sticker,
video, animation, video_note, audio, voice — so the queued message
carries "media": {"kind", "path", "file_id", ...} and the woken
worker can open the file itself. Bot API getFile refuses files over
20 MB; those (and any failed download) are recorded with
"downloaded": false plus a reason, so the worker at least knows
something arrived.

The bot token lives in token.txt next to this script (mode 0600, the
user's 2026-11-17 decision B, same posture as ilink's session.json).
It is never printed and never logged; API URLs embed it by design, so
all error output carries only error types/codes.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

BASE_DIR = os.environ.get(
    "TG_BRIDGE_DIR", os.path.dirname(os.path.abspath(__file__)))
HOOK_STATE = os.environ.get(
    "TG_HOOK_STATE_DIR", os.path.expanduser("~/hooks/state"))
TOKEN_FILE = os.path.join(BASE_DIR, "token.txt")
STATE = os.path.join(BASE_DIR, "state.json")
SEEN = os.path.join(BASE_DIR, "seen.json")
LEDGER = os.path.join(BASE_DIR, "sent-ledger.json")
AUDIT = os.path.join(BASE_DIR, "send-audit.jsonl")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
SEEN_CAP = 500


def _atomic_write(path, data):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
    except Exception:
        return None
    return tok or None


def load_offset():
    return int(_load_json(STATE, {}).get("offset", 0))


def save_offset(offset):
    _atomic_write(STATE, json.dumps({"offset": offset}))


def load_seen():
    ids = _load_json(SEEN, [])
    return ids if isinstance(ids, list) else []


def mark_seen(ids):
    seen = load_seen()
    have = set(seen)
    for i in ids:
        if i not in have:
            seen.append(i)
            have.add(i)
    _atomic_write(SEEN, json.dumps(seen[-SEEN_CAP:]))


class Api:
    def __init__(self, token):
        self.token = token
        self.base = "https://api.telegram.org/bot" + token

    def call(self, method, payload, timeout=40):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/" + method, data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def download_file(self, file_id, dest):
        """getFile + raw download into dest (atomic). Raises on error;
        error text never contains the token (URLs are not echoed)."""
        res = self.call("getFile", {"file_id": file_id}, timeout=15)
        file_path = (res.get("result") or {}).get("file_path")
        if not file_path:
            raise RuntimeError("getFile returned no file_path")
        url = "https://api.telegram.org/file/bot" + self.token + \
            "/" + file_path
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.tmp.{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return len(data)

    def call_multipart(self, method, fields, file_field, file_path,
                       timeout=120):
        """POST multipart/form-data with one file part (sendPhoto /
        sendVideo uploads). Error text never contains the token."""
        import uuid
        boundary = "tgpoll-" + uuid.uuid4().hex
        body = bytearray()
        for name, value in fields.items():
            body += f"--{boundary}\r\n".encode()
            body += (f'Content-Disposition: form-data; '
                     f'name="{name}"\r\n\r\n').encode()
            body += str(value).encode("utf-8") + b"\r\n"
        fname = os.path.basename(file_path)
        body += f"--{boundary}\r\n".encode()
        body += (f'Content-Disposition: form-data; name="{file_field}"; '
                 f'filename="{fname}"\r\n').encode()
        body += b"Content-Type: application/octet-stream\r\n\r\n"
        with open(file_path, "rb") as f:
            body += f.read()
        body += f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            self.base + "/" + method, data=bytes(body),
            headers={"Content-Type":
                     f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))


BOT_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile refuses files >20 MB
_MIME_EXT = {
    "video/mp4": ".mp4", "video/quicktime": ".mov",
    "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/ogg": ".ogg",
    "audio/mp4": ".m4a", "image/jpeg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
}


def _att_ext(att, kind):
    """Pick a file extension for an attachment: original file name
    first, then its mime type, then a per-kind default."""
    name = att.get("file_name") or ""
    ext = os.path.splitext(name)[1]
    if ext:
        return ext
    if kind == "sticker" and att.get("is_animated"):
        return ".tgs"
    mime = str(att.get("mime_type", ""))
    if mime in _MIME_EXT:
        return _MIME_EXT[mime]
    return {"photo": ".jpg", "video": ".mp4", "animation": ".mp4",
            "video_note": ".mp4", "sticker": ".webp",
            "audio": ".mp3", "voice": ".ogg",
            "document": ".bin"}.get(kind, ".bin")


def _extract_media(api, msg, uid):
    """Return a media descriptor for the message's attachment, or
    None. Attachments are downloaded into media/ right away (poll is
    the only stage that talks to Telegram) so the woken worker can
    open the file itself; oversize or failed downloads are recorded
    with downloaded: false and a reason."""
    mid = msg.get("message_id")
    photo = msg.get("photo")
    if photo:
        # PhotoSize list is ascending; the last is the largest.
        return _try_download(api, "photo", photo[-1], uid, mid, ".jpg")
    for kind in ("video", "animation", "video_note", "document",
                 "sticker", "audio", "voice"):
        att = msg.get(kind)
        if isinstance(att, dict) and att.get("file_id"):
            return _try_download(api, kind, att, uid, mid,
                                 _att_ext(att, kind))
    return None


def _try_download(api, kind, att, uid, mid, ext):
    desc = {"kind": kind, "file_id": att.get("file_id")}
    for extra in ("mime_type", "file_name", "file_size", "duration"):
        if att.get(extra) is not None:
            desc[extra] = att[extra]
    size = att.get("file_size")
    if size and size > BOT_DOWNLOAD_LIMIT:
        desc["downloaded"] = False
        desc["error"] = "too_large_for_bot_api"
        return desc
    dest = os.path.join(MEDIA_DIR, f"{uid}_{mid}_{kind}{ext}")
    try:
        api.download_file(att["file_id"], dest)
        desc["downloaded"] = True
        desc["path"] = dest
    except Exception as e:
        desc["downloaded"] = False
        desc["error"] = type(e).__name__
    return desc


def cmd_poll():
    token = load_token()
    if not token:
        print("NO_TOKEN")
        return 1
    api = Api(token)
    offset = load_offset()
    try:
        res = api.call("getUpdates",
                       {"offset": offset, "timeout": 25,
                        "allowed_updates": ["message"]},
                       timeout=40)
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            print("TOKEN_INVALID")
            return 1
        print(f"POLL_ERROR:HTTP {e.code}")
        return 1
    except Exception as e:
        print(f"POLL_ERROR:{type(e).__name__}")
        return 1
    updates = res.get("result", [])
    seen = set(load_seen())
    msgs = []
    ids = []
    new_offset = offset
    for upd in updates:
        uid = upd["update_id"]
        ids.append(uid)
        new_offset = max(new_offset, uid + 1)
        if uid in seen:
            continue  # redelivery of an already-committed update
        msg = upd.get("message")
        if not msg:
            continue
        entry = {
            "update_id": uid,
            "chat_id": msg["chat"]["id"],
            "chat_type": msg["chat"].get("type"),
            "chat_title": msg["chat"].get("title"),
            "from_id": msg.get("from", {}).get("id"),
            "from_name": msg.get("from", {}).get("first_name"),
            "from_username": msg.get("from", {}).get("username"),
            "from_is_bot": bool(msg.get("from", {}).get("is_bot")),
            "message_id": msg.get("message_id"),
            # Captions ride along with photos/files; fold them into
            # text so the worker sees what the user wrote under it.
            "text": msg.get("text") or msg.get("caption"),
            "date": msg.get("date"),
        }
        media = _extract_media(api, msg, uid)
        if media:
            entry["media"] = media
        msgs.append(entry)
    print(json.dumps({"msgs": msgs,
                      "commit": {"offset": new_offset, "ids": ids}},
                     ensure_ascii=False))
    return 0


def cmd_commit():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        print(f"COMMIT_ERROR:bad stdin: {type(e).__name__}")
        return 1
    if not isinstance(payload, dict):
        print("COMMIT_ERROR:payload must be an object")
        return 1
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or \
            not all(isinstance(i, int) for i in ids):
        print("COMMIT_ERROR:ids must be a list of ints")
        return 1
    offset = payload.get("offset")
    if offset is not None and not isinstance(offset, int):
        print("COMMIT_ERROR:offset must be an int")
        return 1
    # Seen ids BEFORE the offset: if we die between the two writes the
    # un-advanced offset makes Telegram redeliver the batch, and the
    # recorded seen ids filter that redelivery out at poll time.
    mark_seen(ids)
    if offset is not None:
        save_offset(offset)
    print("COMMIT_OK")
    return 0


def _audit(entry):
    entry["at"] = int(time.time())
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def cmd_send(chat_id, key):
    text = sys.stdin.read()
    if not text.strip():
        print("SEND_REFUSED:empty text")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "result": "refused_empty"})
        return 1
    if len(text) > 4096:
        print("SEND_REFUSED:text over 4096 chars")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "result": "refused_too_long"})
        return 1
    if text.lstrip().startswith("--"):
        # A CLI flag must never land in a chat as message text (the
        # ilink bridge shipped exactly this bug once, at 00:11).
        print("SEND_REFUSED:text starts with --")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "result": "refused_dashdash"})
        return 1
    ledger = _load_json(LEDGER, {})
    if not isinstance(ledger, dict):
        ledger = {}
    if key in ledger:
        print("SEND_ALREADY_SENT")
        return 0
    token = load_token()
    if not token:
        print("NO_TOKEN")
        return 1
    api = Api(token)
    try:
        res = api.call("sendMessage", {"chat_id": chat_id, "text": text})
    except urllib.error.HTTPError as e:
        # Never retry a failed send blindly: the server may have taken
        # the message without returning a receipt.
        print(f"SEND_ERROR:HTTP {e.code}")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "result": f"error_http_{e.code}"})
        return 1
    except Exception as e:
        print(f"SEND_ERROR:{type(e).__name__}")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "result": f"error_{type(e).__name__}"})
        return 1
    mid = (res.get("result") or {}).get("message_id")
    ledger[key] = {"chat_id": chat_id, "message_id": mid,
                   "at": int(time.time())}
    _atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False))
    _audit({"dir": "out", "chat_id": chat_id, "key": key,
            "result": "sent", "message_id": mid})
    print(f"SENT {mid}")
    return 0


_SEND_MEDIA = {
    # command: (api method, file form field, max upload bytes)
    "send-photo": ("sendPhoto", "photo", 10 * 1024 * 1024),
    "send-video": ("sendVideo", "video", 50 * 1024 * 1024),
    "send-voice": ("sendVoice", "voice", 50 * 1024 * 1024),
}


def cmd_send_media(cmd, chat_id, key, path):
    method, field, limit = _SEND_MEDIA[cmd]
    caption = sys.stdin.read()
    if len(caption) > 1024:
        print("SEND_REFUSED:caption over 1024 chars")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "kind": cmd, "result": "refused_caption_too_long"})
        return 1
    if caption.lstrip().startswith("--"):
        print("SEND_REFUSED:caption starts with --")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "kind": cmd, "result": "refused_dashdash"})
        return 1
    if not os.path.isfile(path):
        print("SEND_REFUSED:file not found")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "kind": cmd, "result": "refused_no_file"})
        return 1
    if os.path.getsize(path) > limit:
        print(f"SEND_REFUSED:file over {limit // (1024 * 1024)} MB")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "kind": cmd, "result": "refused_too_large"})
        return 1
    ledger = _load_json(LEDGER, {})
    if not isinstance(ledger, dict):
        ledger = {}
    if key in ledger:
        print("SEND_ALREADY_SENT")
        return 0
    token = load_token()
    if not token:
        print("NO_TOKEN")
        return 1
    fields = {"chat_id": chat_id}
    if caption.strip():
        fields["caption"] = caption
    if cmd == "send-video":
        fields["supports_streaming"] = "true"
    try:
        res = Api(token).call_multipart(method, fields, field, path)
    except urllib.error.HTTPError as e:
        # Never retry a failed upload blindly: the server may have
        # taken it without returning a receipt.
        print(f"SEND_ERROR:HTTP {e.code}")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "kind": cmd, "result": f"error_http_{e.code}"})
        return 1
    except Exception as e:
        print(f"SEND_ERROR:{type(e).__name__}")
        _audit({"dir": "out", "chat_id": chat_id, "key": key,
                "kind": cmd, "result": f"error_{type(e).__name__}"})
        return 1
    mid = (res.get("result") or {}).get("message_id")
    ledger[key] = {"chat_id": chat_id, "message_id": mid,
                   "kind": cmd, "file": os.path.basename(path),
                   "at": int(time.time())}
    _atomic_write(LEDGER, json.dumps(ledger, ensure_ascii=False))
    _audit({"dir": "out", "chat_id": chat_id, "key": key,
            "kind": cmd, "result": "sent", "message_id": mid,
            "file": os.path.basename(path)})
    print(f"SENT {mid}")
    return 0


def cmd_download(file_id, dest):
    token = load_token()
    if not token:
        print("NO_TOKEN")
        return 1
    try:
        n = Api(token).download_file(file_id, dest)
    except urllib.error.HTTPError as e:
        print(f"DOWNLOAD_ERROR:HTTP {e.code}")
        return 1
    except Exception as e:
        print(f"DOWNLOAD_ERROR:{type(e).__name__}")
        return 1
    print(f"DOWNLOADED {dest} {n}")
    return 0


def cmd_typing(chat_id):
    token = load_token()
    if not token:
        print("NO_TOKEN")
        return 1
    try:
        Api(token).call("sendChatAction",
                        {"chat_id": chat_id, "action": "typing"},
                        timeout=10)
        print("TYPING_OK")
    except Exception as e:
        print(f"TYPING_ERROR:{type(e).__name__}")
        return 1
    return 0


def _queue_count(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("msgs"), list):
            return len(data["msgs"])
    except Exception:
        pass
    return 0


def cmd_health():
    now = int(time.time())
    status = {"token_file": os.path.exists(TOKEN_FILE),
              "offset": load_offset(),
              "seen_count": len(load_seen())}
    hb = os.path.join(HOOK_STATE, "tg-bridge-heartbeat")
    try:
        status["heartbeat_age_secs"] = now - int(os.path.getmtime(hb))
    except Exception:
        status["heartbeat_age_secs"] = None
    status["pending"] = _queue_count(
        os.path.join(HOOK_STATE, "tg-pending.json"))
    proc = os.path.join(HOOK_STATE, "tg-processing.json")
    status["processing"] = _queue_count(proc)
    try:
        status["processing_age_secs"] = now - int(os.path.getctime(proc))
    except Exception:
        status["processing_age_secs"] = None
    alert = _load_json(os.path.join(BASE_DIR, "alert-state.json"), {})
    status["alert"] = {k: alert.get(k, 0)
                       for k in ("err_since", "exp_since")}
    token = load_token()
    probe = "no_token"
    if token:
        try:
            me = Api(token).call("getMe", {}, timeout=15)
            probe = "ok" if me.get("ok") else "bad_response"
            status["bot"] = (me.get("result") or {}).get("username")
        except urllib.error.HTTPError as e:
            probe = f"http_{e.code}"
        except Exception as e:
            probe = f"error_{type(e).__name__}"
    status["token_probe"] = probe
    print(json.dumps(status, ensure_ascii=False))
    if probe in ("no_token", "http_401", "http_404"):
        return 2
    hb_age = status["heartbeat_age_secs"]
    if hb_age is None or hb_age > 180 or status["alert"]["err_since"] or \
            status["alert"]["exp_since"]:
        return 1
    return 0


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "poll":
        return cmd_poll()
    if cmd == "commit":
        return cmd_commit()
    if cmd == "send" and len(argv) == 4:
        return cmd_send(int(argv[2]), argv[3])
    if cmd in _SEND_MEDIA and len(argv) == 5:
        return cmd_send_media(cmd, int(argv[2]), argv[3], argv[4])
    if cmd == "typing" and len(argv) == 3:
        return cmd_typing(int(argv[2]))
    if cmd == "download" and len(argv) == 4:
        return cmd_download(argv[2], argv[3])
    if cmd == "health":
        return cmd_health()
    print(f"unknown or malformed command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
