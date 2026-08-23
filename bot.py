import asyncio
import logging
import os
import re

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntitySpoiler, User
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest

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


# --- Subscription barrier detection (new) -----------------------------------
# This barrier can pop up after /start regardless of which task (Кликер,
# promo, daily bonus) triggered the /start. It shows 1-N "Подписаться"
# buttons (one per sponsor channel/bot) and one confirm button. Substrings
# are used (not exact match) so minor label variations still match.
SUBSCRIBE_BUTTON_TEXT = "подписаться"
BARRIER_CONFIRM_BUTTON_TEXT = "выполнил"  # matches "Я выполнил(а)"


def is_subscription_barrier(buttons) -> bool:
    """True if this button grid looks like the sponsor-subscription barrier."""
    return find_button(buttons, BARRIER_CONFIRM_BUTTON_TEXT) is not None


def get_subscribe_buttons(buttons):
    """All 'Подписаться'-style buttons in the grid (there can be 1, 2, 3, or more)."""
    if not buttons:
        return []
    return [
        b for row in buttons for b in row
        if b.text and SUBSCRIBE_BUTTON_TEXT in b.text.lower()
    ]


def parse_telegram_link(url: str):
    """
    Classify a t.me URL from a subscribe button.
    Returns ("invite_hash", hash), ("username", username), or None if unrecognized.
    """
    if not url:
        return None
    m = re.search(r"t\.me/\+([\w-]+)", url)
    if m:
        return ("invite_hash", m.group(1))
    m = re.search(r"t\.me/joinchat/([\w-]+)", url)
    if m:
        return ("invite_hash", m.group(1))
    m = re.search(r"t\.me/([\w_]+)", url)
    if m and m.group(1).lower() != "joinchat":
        return ("username", m.group(1))
    return None
# ------------------------------------------------------------------------------


# --- Fruit robot-check detection (new) ---------------------------------------
# A different kind of robot-check: after /start, a message with a fruit-emoji
# grid appears, saying "нажми на кнопку, где изображено «Вишня»" (fruit name
# varies each time). It blocks whatever task /start was for until solved.
ROBOT_CHECK_MARKER = "проверка на робота"
FRUIT_NAME_PATTERN = re.compile(r"«([^»]+)»")

FRUIT_EMOJI_MAP = {
    "вишня": "🍒",
    "виноград": "🍇",
    "банан": "🍌",
    "клубника": "🍓",
    "помидор": "🍅",
    "яблоко": "🍎",
    "ананас": "🍍",
    "арбуз": "🍉",
    "кокос": "🥥",
    "абрикос": "🍑",
    "персик": "🍑",
    "лимон": "🍋",
    "апельсин": "🍊",
    "груша": "🍐",
    "манго": "🥭",
    "киви": "🥝",
    "дыня": "🍈",
    "черника": "🫐",
    "слива": "🍑",
}


def extract_target_fruit(text: str):
    """Pull the fruit name out of «...» quotes in the robot-check text, lowercased."""
    if not text:
        return None
    m = FRUIT_NAME_PATTERN.search(text)
    if m:
        return m.group(1).strip().lower()
    return None


def is_fruit_robot_check(msg) -> bool:
    """True if this message is the fruit-emoji robot-check."""
    text = (msg.text or "").lower()
    return ROBOT_CHECK_MARKER in text and extract_target_fruit(msg.text) is not None
# ------------------------------------------------------------------------------


class Account:
    """One userbot instance tied to a single Telegram session."""

    def __init__(self, name: str, api_id: int, api_hash: str, session_string: str,
                 target_bot: str, notify_chat: str, interval_minutes: int, button_text: str,
                 source_channel: str = None, profile_button_text: str = "Профиль",
                 promo_button_text: str = "Промокод",
                 daily_bonus_button_text: str = "Ежедневка", daily_bonus_interval_minutes: int = 1445):
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

        # Daily bonus feature (independent of Кликер and promo-code tasks)
        self.daily_bonus_button_text = daily_bonus_button_text
        self.daily_bonus_interval_minutes = daily_bonus_interval_minutes

    def log(self, msg, *args):
        log.info(f"[{self.name}] {msg}", *args)

    async def notify_user(self, text: str):
        try:
            await self.client.send_message(self.notify_chat, text)
        except Exception as e:
            log.error("[%s] Failed to notify user: %s", self.name, e)

    # --- Subscription barrier solving (new, isolated from existing tasks) ---
    async def _handle_subscribe_button(self, button):
        """Join the channel or /start the bot behind a single 'Подписаться' button."""
        url = getattr(button, "url", None)
        if not url:
            # Not a URL button (rare) - fall back to just clicking it.
            await button.click()
            return

        parsed = parse_telegram_link(url)
        if parsed is None:
            self.log("Subscribe button URL not recognized: %s", url)
            return

        kind, value = parsed
        try:
            if kind == "invite_hash":
                await self.client(ImportChatInviteRequest(value))
                self.log("Subscription barrier: joined channel via invite link")
            else:  # kind == "username"
                entity = await self.client.get_entity(value)
                if isinstance(entity, User) and entity.bot:
                    await self.client.send_message(entity, "/start")
                    self.log("Subscription barrier: started bot @%s", value)
                else:
                    await self.client(JoinChannelRequest(entity))
                    self.log("Subscription barrier: joined channel @%s", value)
        except Exception as e:
            log.warning("[%s] Subscription barrier: failed to process '%s': %s",
                        self.name, url, e)

    async def solve_subscription_barrier(self, msg):
        """
        Handle the sponsor-subscription barrier: join/start every 'Подписаться'
        button (however many there are), then click the confirm button.
        """
        subscribe_buttons = get_subscribe_buttons(msg.buttons)
        self.log("Subscription barrier detected with %d subscribe button(s)",
                  len(subscribe_buttons))

        for button in subscribe_buttons:
            await self._handle_subscribe_button(button)
            await asyncio.sleep(1)  # small pause between joins to avoid flooding

        confirm_button = find_button(msg.buttons, BARRIER_CONFIRM_BUTTON_TEXT)
        if confirm_button:
            await confirm_button.click()
            self.log("Subscription barrier: clicked confirm button")
            await self.notify_user(
                f"🔓 Subscription barrier appeared on {self.target_bot} — "
                f"handled automatically ({len(subscribe_buttons)} channel(s)/bot(s))."
            )
        else:
            self.log("Subscription barrier: confirm button not found, could not clear it")
            await self.notify_user(
                f"⚠️ Subscription barrier appeared on {self.target_bot} but the "
                f"confirm button wasn't found — may need manual action."
            )
    # --------------------------------------------------------------------------

    async def solve_fruit_robot_check(self, msg) -> bool:
        """
        Handle the fruit-emoji robot-check: figure out the target fruit from
        the message text, click the matching emoji button. Returns True if
        solved, False if the fruit/button couldn't be matched (in which case
        we leave it alone rather than guess).
        """
        fruit_name = extract_target_fruit(msg.text)
        emoji = FRUIT_EMOJI_MAP.get(fruit_name)
        if not emoji:
            self.log("Fruit robot-check: no emoji mapping for fruit '%s'", fruit_name)
            await self.notify_user(
                f"⚠️ Fruit robot-check on {self.target_bot} asked for '{fruit_name}' "
                f"but I don't have that fruit mapped — go tap it manually."
            )
            return False

        button = find_button(msg.buttons, emoji)
        if not button:
            self.log("Fruit robot-check: button for '%s' (%s) not found", fruit_name, emoji)
            await self.notify_user(
                f"⚠️ Fruit robot-check on {self.target_bot}: couldn't find the "
                f"{emoji} button for '{fruit_name}' — go tap it manually."
            )
            return False

        await button.click()
        self.log("Fruit robot-check solved: '%s' -> %s", fruit_name, emoji)
        return True

    async def wait_for_buttons(self, conv, max_messages: int = 5, timeout: int = 15):
        """
        Some bots send several messages in a row (e.g. a photo/GIF first,
        then the actual menu/question with buttons a moment later).
        Keep reading messages from the conversation until one has buttons,
        or we run out of attempts / time.

        Also transparently handles the sponsor-subscription barrier and the
        fruit-emoji robot-check if either shows up in between: solves them
        and keeps waiting for the real follow-up, without counting it against
        max_messages. If neither ever appears, this behaves exactly as before.
        """
        last_msg = None
        attempts = 0
        barrier_attempts = 0
        fruit_attempts = 0
        while attempts < max_messages:
            try:
                msg = await conv.get_response(timeout=timeout)
            except asyncio.TimeoutError:
                return last_msg
            last_msg = msg

            if msg.buttons and is_subscription_barrier(msg.buttons):
                barrier_attempts += 1
                if barrier_attempts > 3:
                    self.log("Subscription barrier kept reappearing, giving up after 3 tries")
                    return last_msg
                await self.solve_subscription_barrier(msg)
                continue  # don't count this toward attempts, keep waiting for real content

            if msg.buttons and is_fruit_robot_check(msg):
                fruit_attempts += 1
                if fruit_attempts > 3:
                    self.log("Fruit robot-check kept reappearing, giving up after 3 tries")
                    return last_msg
                solved = await self.solve_fruit_robot_check(msg)
                if solved:
                    continue  # don't count this toward attempts, keep waiting for real content
                else:
                    return msg  # couldn't match the fruit - hand back to caller rather than loop

            attempts += 1
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

    async def run_daily_bonus_cycle(self):
        """/start -> Профиль -> daily-bonus button -> solve math -> click correct answer."""
        self.log("Starting daily bonus cycle")
        async with self.lock:
            try:
                async with self.client.conversation(self.target_bot, timeout=30) as conv:
                    await conv.send_message("/start")
                    menu_msg = await self.wait_for_buttons(conv)
                    if menu_msg is None:
                        await self.notify_user("⚠️ Daily bonus: no response after /start.")
                        return

                    profile_btn = find_button(menu_msg.buttons, self.profile_button_text)
                    if not profile_btn:
                        await self.notify_user(
                            f"⚠️ Daily bonus: couldn't find '{self.profile_button_text}' button.\n"
                            f"Menu text:\n{menu_msg.text}"
                        )
                        return
                    await profile_btn.click()

                    profile_msg = await self.wait_for_buttons(conv)
                    if profile_msg is None:
                        await self.notify_user("⚠️ Daily bonus: no response after Профиль.")
                        return

                    bonus_btn = find_button(profile_msg.buttons, self.daily_bonus_button_text)
                    if not bonus_btn:
                        await self.notify_user(
                            f"⚠️ Daily bonus: couldn't find '{self.daily_bonus_button_text}' button.\n"
                            f"Menu text:\n{profile_msg.text}"
                        )
                        return
                    await bonus_btn.click()

                    followup = await self.wait_for_buttons(conv, max_messages=5, timeout=15)
                    if followup is None:
                        await self.notify_user("⚠️ Daily bonus: no response after clicking bonus button.")
                        return

                    followup_text = followup.text or ""
                    match = MATH_PATTERN.search(followup_text)
                    if not match:
                        self.log("Daily bonus: no math question found. Text: %r", followup_text[:200])
                        await self.notify_user(
                            f"⚠️ Daily bonus: expected a math question but didn't find one.\n"
                            f"Message text:\n{followup_text}"
                        )
                        return

                    a, b = int(match.group(1)), int(match.group(2))
                    answer = a + b
                    answer_button = find_button(followup.buttons, str(answer))
                    if answer_button:
                        await answer_button.click()
                        self.log("Daily bonus solved: %s + %s = %s", a, b, answer)
                        await self.notify_user(
                            f"🎁 Daily bonus claimed on {self.target_bot} — "
                            f"solved {a} + {b} = {answer} ✅"
                        )
                    else:
                        options = format_buttons(followup.buttons)
                        await self.notify_user(
                            "🎁 Daily bonus question appeared but couldn't auto-click!\n\n"
                            f"Question: {a} + {b} = ?\nOptions: {options}\n\n"
                            "Go tap the correct button manually."
                        )

            except asyncio.TimeoutError:
                self.log("Daily bonus cycle timed out")
            except Exception as e:
                log.exception("[%s] Error during daily bonus cycle: %s", self.name, e)
                await self.notify_user(f"⚠️ Daily bonus error: {e}")

    async def daily_bonus_loop(self):
        """Runs the daily-bonus cycle every `daily_bonus_interval_minutes`, independently."""
        while True:
            await self.run_daily_bonus_cycle()
            self.log("Daily bonus: sleeping for %s minutes", self.daily_bonus_interval_minutes)
            await asyncio.sleep(self.daily_bonus_interval_minutes * 60)

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

        asyncio.create_task(self.daily_bonus_loop())
        startup_msg += (
            f"\nAlso running daily bonus claim every {self.daily_bonus_interval_minutes} minutes."
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

    Daily bonus feature (runs for every account automatically):
      DAILY_BONUS_BUTTON_TEXT      - default "Ежедневка"
      DAILY_BONUS_INTERVAL_MINUTES - default 1445 (24h 5m)
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
            daily_bonus_button_text=get("DAILY_BONUS_BUTTON_TEXT", default="Ежедневка"),
            daily_bonus_interval_minutes=int(get("DAILY_BONUS_INTERVAL_MINUTES", default="1445")),
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
