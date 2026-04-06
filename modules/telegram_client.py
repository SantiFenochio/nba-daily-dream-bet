import os
from telegram import Bot
from telegram.constants import ParseMode

# Telegram HTML message limit (leave buffer for safety)
_MAX_MSG_CHARS = 4000


async def send_telegram_message(text: str) -> None:
    """
    Send message to Telegram, splitting automatically if it exceeds 4000 chars.
    Splitting happens at double-newline boundaries (between picks/games)
    to avoid cutting in the middle of HTML tags.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")

    bot = Bot(token=token)
    chunks = _split_html_message(text)

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"[telegram] Sending part {i+1}/{len(chunks)} ({len(chunk)} chars)")
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
        )


def _split_html_message(text: str) -> list[str]:
    """
    Split a long HTML message into chunks of at most _MAX_MSG_CHARS characters.
    Splits preferentially at double-newline (game/pick boundaries) to avoid
    breaking HTML tags mid-stream.
    """
    if len(text) <= _MAX_MSG_CHARS:
        return [text]

    parts: list[str] = []
    # Split on double newlines (natural block boundaries)
    blocks = text.split("\n\n")
    current: list[str] = []
    current_len = 0

    for block in blocks:
        block_len = len(block) + 2  # +2 for the \n\n separator

        if current_len + block_len > _MAX_MSG_CHARS and current:
            parts.append("\n\n".join(current))
            current = [block]
            current_len = block_len
        else:
            current.append(block)
            current_len += block_len

    if current:
        parts.append("\n\n".join(current))

    # Safety: if a single block somehow exceeds the limit, hard-truncate it
    final: list[str] = []
    for part in parts:
        if len(part) <= _MAX_MSG_CHARS:
            final.append(part)
        else:
            # Hard truncate at a safe boundary (avoid cutting HTML tags)
            safe = part[:_MAX_MSG_CHARS]
            # Close any open <b> or <code> tags
            for tag in ("</code>", "</b>", "</i>"):
                if safe.count(tag.replace("/", "")) > safe.count(tag):
                    safe += tag
            final.append(safe)

    return final if final else [text[:_MAX_MSG_CHARS]]
