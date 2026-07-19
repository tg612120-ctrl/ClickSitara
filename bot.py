import asyncio
import logging
import os
import re

from telethon import TelegramClient
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("userbot")

# Matches things like "43 + 48 = ?" (addition only, per the target bot's behavior)
MATH_PATTERN = re.compile(r"(\d+)\s*\+\s*(\d+)\s*=\s*\?")


def find_button(buttons, text_substring: str):
    """Search the 2D button grid for a button whose text contains text_substring."""
    if not buttons:
        return None
    for row in buttons:
        for button in row:
            if button.text and text_substring.lower() in button.text.lower():
                return button
    return None


def format_buttons(buttons):
    """Flatten button grid into a readable list of option labels."""
    if not buttons:
        return "(no buttons found)"
    labels = []
    for row in buttons:
        for button in row:
            if button.text:
                labels.append(button.text)
    return ", ".join(labels)


class Account:
    """One userbot instance tied to a single Telegram session."""

    def __init__(self, name: str, api_id: int, api_hash: str, session_string: str,
                 target_bot: str, notify_chat: str, interval_minutes: int, button_text: str):
        self.name = name
        self.target_bot = target_bot
        self.notify_chat = notify_chat
        self.interval_minutes = interval_minutes
        self.button_text = button_text
        self.client = TelegramClient(StringSession(session_string), api_id, api_hash)

    def log(self, msg, *args):
        log.info(f"[{self.name}] {msg}", *args)

    async def notify_user(self, text: str):
        try:
            await self.client.send_message(self.notify_chat, text)
        except Exception as e:
            log.error("[%s] Failed to notify user: %s", self.name, e)

    async def run_cycle(self):
        """One full pass: /start -> click button -> check for robot-check question."""
        self.log("Starting cycle against %s", self.target_bot)
        try:
            async with self.client.conversation(self.target_bot, timeout=30) as conv:
                await conv.send_message("/start")
                menu_msg = await conv.get_response()

                button = find_button(menu_msg.buttons, self.button_text)
                if not button:
                    self.log("Could not find '%s' button. Menu text: %r",
                             self.button_text, menu_msg.text)
                    await self.notify_user(
                        f"⚠️ Couldn't find the '{self.button_text}' button after /start.\n"
                        f"Menu text was:\n{menu_msg.text}"
                    )
                    return

                self.log("Clicking '%s' button", self.button_text)
                await button.click()

                try:
                    followup = await conv.get_response(timeout=15)
                except asyncio.TimeoutError:
                    self.log("No follow-up message this cycle. Nothing to do.")
                    return

                followup_text = followup.text or ""
                match = MATH_PATTERN.search(followup_text)

                if match:
                    a, b = int(match.group(1)), int(match.group(2))
                    options = format_buttons(followup.buttons)
                    self.log("Robot-check detected: %s + %s = ?", a, b)
                    await self.notify_user(
                        "🤖 Robot-check appeared on {bot}!\n\n"
                        "Question: {a} + {b} = ?\n"
                        "Answer options: {options}\n\n"
                        "This needs to be solved manually — go tap the correct "
                        "button in your chat with {bot}.".format(
                            bot=self.target_bot, a=a, b=b, options=options
                        )
                    )
                else:
                    self.log("Follow-up wasn't a robot-check. Ignoring: %r",
                              followup_text[:200])

        except asyncio.TimeoutError:
            self.log("Conversation timed out waiting for a response from %s", self.target_bot)
        except Exception as e:
            log.exception("[%s] Error during cycle: %s", self.name, e)
            await self.notify_user(f"⚠️ Userbot error during cycle: {e}")

    async def run_forever(self):
        await self.client.start()
        me = await self.client.get_me()
        self.log("Logged in as %s (id=%s)", me.username or me.first_name, me.id)
        await self.notify_user(
            f"✅ Userbot started. Will click '{self.button_text}' on {self.target_bot} "
            f"every {self.interval_minutes} minutes."
        )

        while True:
            await self.run_cycle()
            self.log("Sleeping for %s minutes", self.interval_minutes)
            await asyncio.sleep(self.interval_minutes * 60)


def load_accounts() -> list:
    """
    Loads one or more accounts from environment variables.

    Shared defaults (used unless overridden per-account):
      API_ID, API_HASH, TARGET_BOT, NOTIFY_CHAT, INTERVAL_MINUTES, BUTTON_TEXT

    Per-account session strings, numbered starting at 1:
      SESSION_STRING_1, SESSION_STRING_2, SESSION_STRING_3, ...

    Any per-account override, e.g. for account 2:
      API_ID_2, API_HASH_2, TARGET_BOT_2, NOTIFY_CHAT_2, INTERVAL_MINUTES_2, BUTTON_TEXT_2

    Backward compatible: if SESSION_STRING (no suffix) is set instead, it's
    treated as a single account "1".
    """
    accounts = []

    # Backward-compatible single-account fallback
    if "SESSION_STRING" in os.environ and "SESSION_STRING_1" not in os.environ:
        os.environ["SESSION_STRING_1"] = os.environ["SESSION_STRING"]

    i = 1
    while True:
        session_key = f"SESSION_STRING_{i}"
        if session_key not in os.environ:
            break

        def get(key_base, default=None, required=False):
            val = os.environ.get(f"{key_base}_{i}", os.environ.get(key_base, default))
            if required and val is None:
                raise RuntimeError(f"Missing required env var: {key_base}_{i} or {key_base}")
            return val

        accounts.append(Account(
            name=f"account{i}",
            api_id=int(get("API_ID", required=True)),
            api_hash=get("API_HASH", required=True),
            session_string=os.environ[session_key],
            target_bot=get("TARGET_BOT", required=True),
            notify_chat=get("NOTIFY_CHAT", default="me"),
            interval_minutes=int(get("INTERVAL_MINUTES", default="10")),
            button_text=get("BUTTON_TEXT", default="Кликер"),
        ))
        i += 1

    if not accounts:
        raise RuntimeError(
            "No accounts configured. Set SESSION_STRING_1 (and API_ID_1/"
            "API_HASH_1 or shared API_ID/API_HASH, plus TARGET_BOT_1 or "
            "shared TARGET_BOT)."
        )

    return accounts


async def main():
    accounts = load_accounts()
    log.info("Loaded %d account(s): %s", len(accounts), ", ".join(a.name for a in accounts))

    async def run_isolated(account: Account):
        """Wrap each account's loop so one crashing doesn't kill the others."""
        try:
            async with account.client:
                await account.run_forever()
        except Exception as e:
            log.exception("[%s] Fatal error, this account stopped: %s", account.name, e)

    batch_size = int(os.environ.get("BATCH_SIZE", "5"))
    batch_delay = float(os.environ.get("BATCH_DELAY_SECONDS", "5"))

    tasks = []
    for start in range(0, len(accounts), batch_size):
        batch = accounts[start:start + batch_size]
        log.info("Starting batch: %s", ", ".join(a.name for a in batch))
        for account in batch:
            tasks.append(asyncio.create_task(run_isolated(account)))
        # Wait before starting the next batch, unless this was the last one
        if start + batch_size < len(accounts):
            await asyncio.sleep(batch_delay)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
