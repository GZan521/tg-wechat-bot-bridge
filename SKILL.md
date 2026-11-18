# Telegram and WeChat ClawBot Bridge

## Start with the transport choice

- Ask the user which platform they want first, Telegram or WeChat ClawBot. Build one bridge per platform, and reuse the same design spine for both.

- State every hard condition before the first step, so the user never gets stranded mid flow. Telegram needs the user to create a bot and change one privacy setting. WeChat needs the user to scan a QR code and send the first message inside a ten minute binding window.

- Guide the setup one step at a time. Name exactly what the user does, wait for them to finish, and verify the result before moving to the next step.

- Set capability expectations per platform before building anything. Telegram carries text, photos, videos, and voice in both directions. WeChat carries text, images, files, and videos in both directions, and voice only inbound, because the channel drops voice messages that a bot sends.

- Offer the verified route first. Both routes run in production for this user. The WeChat route was deployed from the upstream open source bridge and verified live on 2026-11-18, with text, images, and video confirmed in both directions.

## Know the shared shape

- Run the bridge with no resident process on either platform. A polling hook performs one long poll per run, and a woken worker agent answers from a durable queue.

- Split the system into four parts that change on their own schedules. The protocol client script, the hook script, the worker prompt inside the hook definition, and the watchdog cron.

- Treat the worker prompt as a first class component. A cutover that updates the scripts but not the prompt leaves new messages claimed by nobody.

- Build the client around two phase delivery. Poll only fetches and never advances the offset or cursor, the caller queues the batch durably, and commit records the seen message ids before it advances.

- Make every send idempotent with a ledger or a send key, and append each send to an audit log. Refuse empty bodies and bodies that start with two dashes, and never retry a failed send by hand, because the server may have accepted it without returning a receipt.

- Give the hook script a single instance lock, a heartbeat file touched on every run, and a wake throttle. Fresh batches wake at once, and reminders about an unclaimed batch wait at least two minutes.

- Build the alert loop into the hook and add a watchdog that reports only on state changes. Token problems wake once and then at most hourly, poll failures wake only after ten minutes of continuous failure, and a recovery after an alerted outage wakes once.

## Connect Telegram

- Have the user create the bot with @BotFather and turn group privacy off, so the bot receives every group message. Registration and account steps stay with the user.

- Decide where the bot token lives before writing any code, and get the user to choose the storage. A local token file with mode 0600 survives restarts on its own, while a credential manager can fail in transit.

- Verify the token with a direct getMe call before blaming the user or the code when calls fail. A token that passes in the browser but fails through a manager points at the manager path, and entering the same token again does not fix it.

- Start from the starter copies in the references folder. The client is references/tg_poll.py, the hook script is references/tg-bridge-inbox.sh, and the worker prompt is references/worker-prompt-template.md.

- Read the live deployment record before touching the running bridge. The connector doc at ~/workspace/custom-connectors/custom.telegram-bot/CONNECTOR.md holds the commands, the reply rules, and the history for this user's bot at ~/workspace/telegram-bridge.

## Connect WeChat ClawBot

- Clone the upstream bridge and follow the deployment contract in its root AGENTS.md, which is maintained with the code. The repository is https://github.com/gfydfhcf/wechat-ilink-muse-bridge and the protocol notes distilled from it sit in references/wechat-ilink-notes.md.

- Install the single dependency first with `pip install -r requirements.txt`. Its cryptography library powers the encrypted media upload, and skipping the install breaks outbound images and videos while text keeps working. Then generate the login QR code with the client. Never display the QR image directly in the chat. Give the user a link to the QR code instead, and let them open the link and scan the code with WeChat from there. The code expires in about two minutes, so have the user ready to scan before you generate it, and always generate a fresh code instead of reusing an old link. Wait for them to finish scanning before running the login command.

- Guard the session file like a password after login. It stays on the local machine, out of chats, out of git, and out of any shared folder.

- Open the owner binding window right after login and have the user send the bot a message before it closes. The first sender becomes the owner, the whitelist is enforced in code from that moment, and approvals for new senders happen only in the main chat, never inside WeChat itself.

- Choose the hook driver from the repository examples for a Muse deployment. Keep the example daemon for protocol learning only, and run exactly one driver at a time.

- Expect the session to expire at an unpredictable time. The bridge asks for a fresh scan once when that happens, and the user performs the scan while the agent waits.

## Carry media in both directions

- Download attachments during poll on both platforms, because poll is the only stage that talks to the channel. Fold captions and transcriptions into what the worker sees, and record the local file path on the queued message.

- On Telegram, cover every attachment kind, including photos, documents, stickers, videos, animations, video notes, audio, and voice, and respect the Bot API download limit of 20 MB. Record an oversized file as not downloaded with the reason attached, so the worker can ask the user to shrink it.

- On WeChat, send media only with the client commands. A reply uses `send-image`, `send-video`, or `send-file` with the user id, the context token from the message being answered, and the file path. A proactive push uses the `proactive-image`, `proactive-video`, or `proactive-file` variants, which reuse the cached context token. The queue must carry the context token to the worker, because a media reply without it cannot be sent.

- Never put a WeChat picture or video inside a text reply as markdown image syntax. The channel keeps only the alt text, so the user receives words and no media while the send looks successful. Treat this as the first suspect whenever outbound WeChat media goes missing.

- On WeChat, let downloads resume after interruptions instead of restarting, and read the protocol notes before debugging a media failure, because two enum and encoding details in this protocol disagree with the obvious reading. Keep outbound files under the client limit of 100 MB, and send media at its original quality by default. Full size images were verified to arrive intact on this channel, so shrink a file only as a last resort after repeated failures, and tell the user when you do.

- Treat a WeChat send that ends with a closed connection or a missing response as an unknown outcome, never as a failure. The server may have delivered the media before the receipt was lost, and a live test here proved exactly that, with a full size image arriving after the client reported an error. Do not tell the user the send failed on that evidence, and do not resend until the user confirms it never arrived, because a blind resend delivers duplicates.

- Send media with the matching send command for each platform, under the same idempotency discipline as text. Keep Telegram photos under 10 MB and videos and voice under 50 MB.

- Require the worker to open the received file before replying. Sample frames from a video with ffmpeg when the content matters, and never let the worker claim that an attachment could not pass through the bridge.

## Run the voice loop where the channel allows it

- On Telegram, transcribe voice and audio messages locally before the worker answers. This deployment uses faster-whisper with the small model through the transcribe helper from the Transcribe Recordings skill, with the language set from the user.

- Answer a Telegram batch that contains a voice message with a voice message. Compose short spoken sentences with pauses, synthesize with the voice the user approved, convert to ogg with the opus codec, and send it as voice.

- On WeChat, use the transcription that arrives with each inbound voice message, and answer in text or media instead. The channel silently drops outbound voice today, so send audio as a file or a video when the bot needs to be heard on WeChat.

- Fall back to a text reply only when synthesis or conversion fails, and say so in the worker report. Include the spoken text in the report so the exchange stays auditable, and keep links and long numbers out of spoken replies.

## Verify before calling it done

- Test the client offline first with simulated updates and a stubbed transport. Cover each attachment kind, the oversize skip where the platform has one, the upload body, and the idempotency replay.

- Run the platform health command and a hook dry run after every change to the client, the hook script, or the worker prompt. A clean dry run ends silent with no new messages.

- Send one harmless live test for each new capability with the user watching, such as a test photo, a three second video, or a short voice line on Telegram. Report the delivered message ids as the evidence.

- On WeChat, run the client smoke test before calling outbound media done. It sends the owner a real test text and a one pixel test image through the production send paths. Have the user confirm both arrived inside WeChat, because a successful API response alone does not prove delivery on this channel.

- Close the loop with messages from the user inside the app itself. The bridge works when the user sends text, an image, and a video, watches the replies land in the same chat, and receives one proactive push without asking.

## Dodge the known failure modes

- Never judge liveness by matching a process name or a command line. A starter that launches the same program with a shorter command line defeats the match and wedges the down flag, so use the heartbeat file instead.

- Never treat a corrupt queue file as an empty queue. Quarantine it under a timestamped name and log the move, because an unreadable batch still holds real messages.

- Never let repeated wakes pile up while a batch waits unclaimed. A wake every ten seconds flooded the worker queue during the first Telegram cutover and delayed the first real replies by an hour.

- Never advance the offset or cursor before the batch sits in the queue. At least once delivery with redelivery filtering beats a silent loss that nobody notices.

- Never approve a new WeChat sender from inside the WeChat conversation. The whitelist changes only from the main chat, because a stranger must not talk their way onto the list.

- Never promise bot to bot conversation inside a Telegram group. The Bot API never delivers one bot's messages to another bot, and a relay needs a real user account in the middle, which is a separate build.

- Never read or print credential files while debugging. Health output, error types, and status codes carry enough signal without exposing the secret.

## Available connectors

- [Telegram Bot](~/workspace/custom-connectors/custom.telegram-bot/CONNECTOR.md) for the live Telegram bot connection, its command reference, and the deployment history of this user's bridge.

- Use `muse.connector_search` to find other connectors and `muse.skill_search` to find related skills.

- If nothing here fits, look on the web for a maintained API or MCP server and set it up with [Custom Connectors and MCPs](/opt/muse/skills/custom-connector-setup/SKILL.md), or build a custom connector.
