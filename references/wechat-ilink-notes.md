# WeChat ClawBot route notes (iLink protocol)

Distilled from the open source bridge [gfydfhcf/wechat-ilink-muse-bridge](https://github.com/gfydfhcf/wechat-ilink-muse-bridge) (MIT license), which connects Muse to WeChat over Tencent's official iLink / ClawBot protocol at ilinkai.weixin.qq.com. The repository root carries an AGENTS.md deployment contract that is maintained with the code. Clone the repository and follow that contract for the deployment itself, and use these notes for the protocol facts, the limits, and the checks that sit around it.

Verification status: the Telegram route in this skill is verified end to end on this user's live bridge. The WeChat route is distilled from the upstream repository and its documented production use by its author. It has not been deployed for this user yet, so treat the first deployment as a supervised trial and verify each stage before promising the result.

## How the channel works

- The bot is a separate iLink identity inside WeChat. It only receives messages sent to the bot itself, and the protocol has no endpoints for the user's own chats, contacts, or moments.

- Login is a QR scan. Run `python3 ilink.py qrcode` to print a QR image path, give the user a link to that image instead of displaying the image in the chat, and have the user open the link and scan it with WeChat, then run `python3 ilink.py login <qrcode-id>`. The login state lands in `data/session.json` under the data directory (default ./data, override with the ILINK_DATA_DIR environment variable).

- Treat `data/session.json` as a password. It stays local, it is gitignored upstream, and it never gets shared, pasted into chats, or committed anywhere.

- Owner binding is required before any driver starts. Run `python3 ilink.py bind` to open a ten minute window, and have the user send the bot any message inside that window. The first sender becomes the owner and receives four welcome messages. A timeout prints BIND_TIMEOUT, and rerunning the command reopens the window.

- The whitelist is enforced in code through `data/owner.json` (owner_ids, with owner_id as the primary account, and multiple accounts of the same person allowed). Messages from anyone else are dropped during poll, never queued, and their media is never downloaded. Their sender ids collect in `data/unknown_senders.json` as a pending approval list. Approvals happen only from the user's main chat with Muse, never from inside the WeChat conversation itself, because a stranger must not be able to talk their way onto the list.

- Without an owner file the bridge refuses everyone by default.

## Drivers

- Two drivers exist and only one may run. A shared lock file (ilink-driver.lock in the state directory) makes the first driver win and the later one stand down quietly.

- `daemon.py` is an example driver for any server. It commits the cursor only after handling, so a crash redelivers the batch. Use it to learn the protocol, not for production reliability.

- The hook driver under `examples/muse-hook/` in the repository is the production route inside Muse. It uses the same spine as the Telegram route in this skill, with a durable queue, crash recovery, the alert loop, and a heartbeat watchdog.

## Commands worth knowing

- `python3 ilink.py poll` fetches one round of new messages as JSON, and `python3 ilink.py commit` commits the cursor after the batch is queued.

- `python3 ilink.py send <user_id> <context_token> -` sends a reply with the body on stdin. Replies need the context token that arrived with the user's message.

- `python3 ilink.py proactive <user_id> -` pushes a message without a fresh user message, reusing the cached context token, with a retry that drops the token when the cached one has gone stale.

- `python3 ilink.py typing <user_id> <start|stop>` drives the typing indicator. The client fetches a typing ticket through getconfig and caches it for about nine minutes.

- `python3 ilink.py send-image <user_id> <ctx> <path>`, `send-file`, and `send-video` send media as a reply, and the `proactive-image`, `proactive-file`, and `proactive-video` variants push media unprompted.

- `python3 ilink.py health` prints the full state (login age, token probe, queue depth, recent poll errors) with exit codes 0, 1, and 2 for monitoring. `python3 ilink.py watchdog` judges the hook heartbeat with a thirty minute default limit. `python3 ilink.py stats` is a local snapshot that never calls the server.

- `python3 ilink.py preflight` checks the environment before login, and `python3 ilink.py smoke-test` runs an end to end self check. `prune-media` enforces media retention (thirty days and 500 MB by default), and `rotate-chatlog` archives older chat logs.

- `python3 ilink.py version-check` compares against the latest upstream release. The owner can subscribe to update notices and install an update by replying with the agreed keywords, and `rollback` restores the newest backup when an update misbehaves. Updates replace code only and never touch state files, and a changed AGENTS.md contract still needs a manual merge.

## Media flow

- Sending media takes four steps. Apply for an upload URL with getuploadurl, encrypt the file with AES-128-ECB, upload it to the CDN, then send the message with the media item attached. Two enum and encoding details in the protocol disagree with the obvious reading, and the upstream code comments mark both spots.

- Receiving media reverses the CDN path with decryption. Downloads resume with HTTP Range requests and exponential backoff, because a full restart on a weak network tends to die near the end of a large file.

- Inbound quotes resolve locally. Poll keeps the quote relation the server sends, and a local cache maps message ids to originals for thirty days or ten thousand entries, including messages the bot itself sent. Messages from before the cache existed cannot be recovered, because the server has no fetch-by-id endpoint.

- Inbound deduplication filters on message_id against the last five hundred ids persisted on disk, because the server occasionally delivers the same message twice.

## Channel limits, all verified upstream

- Voice goes one way only. The bot receives voice messages with WeChat's own transcription attached, but voice messages the bot sends are silently dropped by the channel. The server reports success and the client never displays them. The upstream send-voice path (full SILK encoding chain) is implemented and dormant until Tencent opens the channel. Until then, send audio as a file or a video when the bot needs to be heard.

- Tool call progress items exist in the protocol (message types 11 and 12) but the channel never creates them. A send returns HTTP 200 with an empty body and no message id. The typing indicator is the only working progress signal.

- Outbound quotes do not exist. Adding a reference field to the send structure still delivers a plain message with nothing quoted above it. Inbound quote reading works, outbound quote sending does not, and the upstream project deliberately does not fake it by pasting the original text into the body.

- Session lifetime varies by account. Early community reports said about a day, while upstream production ran one login for over three days without expiry. An expired session needs a fresh QR scan by the user, and the bridge raises that request only once instead of repeating it.

- Bot text renders as Markdown in the WeChat client (headings, bold, code blocks with syntax highlighting, lists, links, tables, strikethrough, quotes). Task lists degrade to plain bullets with the checkbox text left visible, image syntax shows only the alt text, and formulas lose their dollar signs without rendering. Send real images through the media commands, never through Markdown.

- Long polling connections get cut by cross country links now and then. The client backs off and reconnects, and POLL_ERROR lines in the log are routine rather than alarming.

## The guard that came from an accident

Upstream once sent the literal text `--key` to the owner at midnight, because a worker built a send command with the body placeholder missing and the idempotency flag slid into the body position. Since then, send and proactive validate the body before anything goes out and refuse empty bodies and bodies starting with `--`, recording a refusal in the audit instead. The Telegram client in this skill carries the same guard for the same reason.
