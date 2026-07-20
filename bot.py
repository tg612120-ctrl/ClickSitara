import asyncio
import logging
import os
import re

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntitySpoiler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("userbot")

# Telethon logs its own internal update-sync chatter ("Got difference for
# channel..." etc.) at INFO level, which drowns out our actual logs. Quiet
# it down to WARNING so only our [account...] lines show up.
logging.getLogger("telethon").setLevel(logging.WARNING)

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


def extract_spoiler_text(message):
    """Return the first spoiler-tagged text in a message, or None if there isn't one."""
    if not message.entities:
        return None
    pairs = message.get_entities_text(MessageEntitySpoiler)
    if not pairs:
        return None
    _entity, text = pairs[0]
    return text.strip()


class Account:
    """One userbot instance tied to a single Telegram session."""

    def __init__(self, name: str, api_id: int, api_hash: str, session_string: str,
                 target_bot: str, notify_chat: str, interval_minutes: int, button_text: str,
                 source_channel: str = None, profile_button_text: str = "Профиль",
                 promo_button_text: str = "Промокод"):
        self.name = name
        self.target_bot = target_bot
        self.notify_chat = notify_chat
        self.interval_minutes = interval_minutes
        self.button_text = button_text
        self.client = TelegramClient(StringSession(session_string), api_id, api_hash, catch_up=True)

        # Guards any conversation with the target bot, so the Кликер cycle
        # and the promo-code task never talk to it at the same moment.
        self.lock = asyncio.Lock()

        # Promo-code feature (only active if source_channel is set for this account)
        self.source_channel = source_channel
        self.profile_button_text = profile_button_text
        self.promo_button_text = promo_button_text
        self.promo_queue = asyncio.Queue()

    def log(self, msg, *args):
        log.info(f"[{self.name}] {msg}", *args)

    async def notify_user(self, text: str):
        try:
            await self.client.send_message(self.notify_chat, text)
        except Exception as e:
            log.error("[%s] Failed to notify user: %s", self.name, e)

    async def wait_for_buttons(self, conv, max_messages: int = 5, timeout: int = 15):
        """
        Some bots send several messages in a row (e.g. a photo/GIF first,
        then the actual menu/question with buttons a moment later).
        Keep reading messages from the conversation until one has buttons,
        or we run out of attempts / time.
        """
        last_msg = None
        for _ in range(max_messages):
            try:
                msg = await conv.get_response(timeout=timeout)
            except asyncio.TimeoutError:
                return last_msg
            last_msg = msg
            if msg.buttons:
                return msg
        return last_msg

    async def run_cycle(self):
        """One full pass: /start -> click button -> check for robot-check question."""
        self.log("Starting cycle against %s", self.target_bot)
        async with self.lock:
            try:
                async with self.client.conversation(self.target_bot, timeout=30) as conv:
                    await conv.send_message("/start")
                    menu_msg = await self.wait_for_buttons(conv)

                    if menu_msg is None:
                        self.log("No response at all after /start.")
                        await self.notify_user("⚠️ No response at all after /start.")
                        return

                    button = find_button(menu_msg.buttons, self.button_text)
                    if not button:
                        self.log("Could not find '%s' button. Menu text: %r",
                                 self.button_text, menu_msg.text)
                        await self.notify_user(
                            f"⚠️ Couldn't find the '{self.button_text}' button after /start.\n"
                            f"Last message text was:\n{menu_msg.text}"
                        )
                        return

                    self.log("Clicking '%s' button", self.button_text)
                    await button.click()

                    followup = await self.wait_for_buttons(conv, max_messages=3, timeout=15)
                    if followup is None:
                        self.log("No follow-up message this cycle. Nothing to do.")
                        return

                    followup_text = followup.text or ""
                    match = MATH_PATTERN.search(followup_text)

                    if match:
                        a, b = int(match.group(1)), int(match.group(2))
                        answer = a + b
                        self.log("Robot-check detected: %s + %s = ?", a, b)
                        answer_button = find_button(followup.buttons, str(answer))
                        if answer_button:
                            await answer_button.click()
                            self.log("Robot-check solved: clicked '%s'", answer)
                            await self.notify_user(
                                f"🤖 Robot-check appeared on {self.target_bot} — "
                                f"solved automatically: {a} + {b} = {answer} ✅"
                            )
                        else:
                            options = format_buttons(followup.buttons)
                            self.log("Robot-check answer button not found for '%s'", answer)
                            await self.notify_user(
                                "🤖 Robot-check appeared on {bot}!\n\n"
                                "Question: {a} + {b} = ?\n"
                                "Answer options: {options}\n\n"
                                "Couldn't find a matching button — go tap the correct "
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

    async def redeem_promo(self, code: str):
        """/start -> Профиль -> Промокод -> send the code -> report the bot's reply."""
        self.log("Redeeming promo code: %s", code)
        async with self.lock:
            try:
                async with self.client.conversation(self.target_bot, timeout=30) as conv:
                    await conv.send_message("/start")
                    menu_msg = await self.wait_for_buttons(conv)
                    if menu_msg is None:
                        await self.notify_user(
                            f"⚠️ Promo task: no response after /start for code '{code}'."
                        )
                        return

                    profile_btn = find_button(menu_msg.buttons, self.profile_button_text)
                    if not profile_btn:
                        await self.notify_user(
                            f"⚠️ Promo task: couldn't find '{self.profile_button_text}' "
                            f"button. Menu text:\n{menu_msg.text}"
                        )
                        return
                    await profile_btn.click()

                    profile_msg = await self.wait_for_buttons(conv)
                    if profile_msg is None:
                        await self.notify_user(
                            f"⚠️ Promo task: no response after clicking "
                            f"'{self.profile_button_text}'."
                        )
                        return

                    promo_btn = find_button(profile_msg.buttons, self.promo_button_text)
                    if not promo_btn:
                        await self.notify_user(
                            f"⚠️ Promo task: couldn't find '{self.promo_button_text}' "
                            f"button. Menu text:\n{profile_msg.text}"
                        )
                        return
                    await promo_btn.click()

                    # Bot should now prompt "send your code" - just wait for that,
                    # then send the actual code as a plain message.
                    try:
                        await conv.get_response(timeout=15)
                    except asyncio.TimeoutError:
                        pass  # some bots may not send a prompt at all; try sending anyway

                    await conv.send_message(code)

                    try:
                        result_msg = await conv.get_response(timeout=15)
                        result_text = result_msg.text or "(no text in reply)"
                    except asyncio.TimeoutError:
                        result_text = "(bot didn't reply in time)"

                    self.log("Promo code '%s' submitted. Reply: %r", code, result_text[:200])
                    await self.notify_user(
                        f"🎟 Promo code '{code}' submitted on {self.target_bot}.\n"
                        f"Bot replied:\n{result_text}"
                    )

            except asyncio.TimeoutError:
                await self.notify_user(
                    f"⚠️ Promo task timed out while redeeming code '{code}'."
                )
            except Exception as e:
                log.exception("[%s] Error redeeming promo code '%s': %s", self.name, code, e)
                await self.notify_user(f"⚠️ Error redeeming promo code '{code}': {e}")

    async def promo_worker(self):
        """Pulls detected codes off the queue one at a time and redeems them."""
        while True:
            code = await self.promo_queue.get()
            await self.redeem_promo(code)

    def register_source_channel_listener(self):
        """Watch the source channel for new posts and queue any spoiler-hidden code."""
        @self.client.on(events.NewMessage(chats=self.source_channel))
        async def _handler(event):
            self.log("Source channel post received (id=%s). Checking for spoiler...",
                      event.message.id)
            code = extract_spoiler_text(event.message)
            if code:
                self.log("Detected spoiler code in source channel: %s", code)
                await self.notify_user(
                    f"📢 New post detected with promo code: {code}\n"
                    f"Queuing redemption (will run before the next Кликер cycle)..."
                )
                await self.promo_queue.put(code)
            else:
                self.log("Source channel post had no spoiler text. Ignoring.")

    async def run_forever(self):
        await self.client.start()
        me = await self.client.get_me()
        self.log("Logged in as %s (id=%s)", me.username or me.first_name, me.id)

        startup_msg = (
            f"✅ Userbot started. Will click '{self.button_text}' on {self.target_bot} "
            f"every {self.interval_minutes} minutes."
        )
        if self.source_channel:
            self.register_source_channel_listener()
            asyncio.create_task(self.promo_worker())
            startup_msg += (
                f"\nAlso watching {self.source_channel} for spoiler promo codes "
                f"(will pause Кликер briefly to redeem them when found)."
            )
        await self.notify_user(startup_msg)

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

    Promo-code feature (single designated account only):
      SOURCE_CHANNEL       - channel username/id to watch for spoiler codes
      PROMO_ACCOUNT        - which account name watches it, default "account1"
      PROFILE_BUTTON_TEXT  - default "Профиль"
      PROMO_CODE_BUTTON_TEXT - default "Промокод"
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

    # Wire up the promo-code feature on ALL loaded accounts
    source_channel = os.environ.get("SOURCE_CHANNEL")
    if source_channel:
        for target in accounts:
            target.source_channel = source_channel
            target.profile_button_text = os.environ.get("PROFILE_BUTTON_TEXT", "Профиль")
            target.promo_button_text = os.environ.get("PROMO_CODE_BUTTON_TEXT", "Промокод")
            log.info("Promo-code watching enabled on %s for channel %s",
                     target.name, source_channel)

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
