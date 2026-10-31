"""Telegram adapters. All authorization is evaluated on each interaction."""

import secrets
import time
from collections import deque

from telegram import InlineKeyboardButton as Button
from telegram import InlineKeyboardMarkup as Markup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .config import channel_id, group_id, positive_id
from .delivery import DeliveryService
from .permissions import Permissions
from .search import Busy, Cache, Limiter, Query, SearchService, page
from .welcome import render, validate_template

# Runtime tuning cannot exceed deployment hard bounds.
TUNABLES = {"search_limit": (1, 500), "cache_ttl": (1, 3600), "user_rate": (1, 120), "chat_rate": (1, 300)}
STAFF_COMMANDS = {"approve", "unapprove", "setwelcome", "previewwelcome", "setphoto", "delphoto", "resetwelcome"}
HELP = """Send a movie or series title, or /search <title>.
Examples: Money Heist S02 · Interstellar 1080p · Breaking Bad S01 Hindi
Use the file buttons to receive a result in this chat.
/start shows your welcome message. Group search requires approval."""
ADMIN_HELP = """Administration (numeric IDs only)
/addsource <channel_id> — authorize a source you may distribute
/setstore <channel_id> — authorize storage (additive)
/delsource <channel_id> · /delstore <channel_id>
/approve [group_id] · /unapprove [group_id]
/addsudo <user_id> · /delsudo <user_id> (or reply to a user)
/sudo — list sudo users
/setwelcome <HTML text> (or reply to text)
/setphoto (reply to a Telegram photo) · /delphoto
/previewwelcome · /resetwelcome
/configure <search_limit|cache_ttl|user_rate|chat_rate> <integer>
/menu — owner dashboard
Sources must also be storage channels; both accounts must already have access."""


class Handlers:
    def __init__(self, settings, db, source):
        self.settings, self.db, self.source = settings, db, source
        self.permissions = Permissions(settings.owners, db)
        self.search_service = SearchService(db, source, settings)
        self.delivery = DeliveryService(db, source, settings)
        self.limiter = Limiter(settings.rate_window)
        self.sessions = Cache(settings.cache_size, 600)
        self.confirmations = Cache(200, 120)
        self.notices = Cache(settings.cache_size, 10)
        self.boot = time.monotonic()
        self.errors = deque(maxlen=15)

    async def option(self, key, default):
        return await self.db.setting(key, default)

    async def allowed(self, update):
        uid, chat = update.effective_user.id, update.effective_chat
        if not await self.option("search_enabled", True):
            raise Busy("Search is temporarily disabled by an owner.")
        if not await self.permissions.search(
            uid, chat.id, chat.type, await self.option("private_search", self.settings.private_search)
        ):
            raise Busy("Search is unavailable here. Ask an owner to approve this group or enable private search.")

    async def limits(self, update):
        self.limiter.check(
            [
                (("user", update.effective_user.id), await self.option("user_rate", self.settings.user_rate)),
                (("chat", update.effective_chat.id), await self.option("chat_rate", self.settings.chat_rate)),
            ]
        )

    def install(self, app):
        for command in (
            "start",
            "help",
            "search",
            "menu",
            "addsudo",
            "delsudo",
            "sudo",
            "approve",
            "unapprove",
            "addsource",
            "delsource",
            "setstore",
            "delstore",
            "setwelcome",
            "previewwelcome",
            "setphoto",
            "delphoto",
            "resetwelcome",
            "configure",
        ):
            app.add_handler(CommandHandler(command, self.command))
        app.add_handler(CallbackQueryHandler(self.callback))
        app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, self.channel_post))
        app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, self.welcome_members))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.search))
        app.add_handler(MessageHandler(filters.COMMAND, self.unknown))

    async def channel_post(self, update, context):
        message = update.effective_message
        cid = message.chat.id
        if cid not in await self.db.channels("source") or cid not in await self.db.channels("storage"):
            return
        if message.has_protected_content:
            return
        media = message.document or message.video or message.audio
        if media:
            original = await self.source.message(cid, message.message_id)
            if original:
                f = await self.source.index(original, cid)
                await self.db.save_file_id(f["id"], media.file_id)

    async def unknown(self, update, context):
        if update.effective_chat.type == "private":
            await update.effective_message.reply_text("Unknown command. Use /help.")

    async def welcome_text(self, user):
        return render(await self.option("welcome_text", self.settings.default_welcome), user)

    async def welcome(self, message, user, preview=False):
        text = await self.welcome_text(user)
        photo = await self.option("welcome_photo", self.settings.default_photo)
        if photo:
            try:
                await message.reply_photo(photo, caption=text, parse_mode="HTML")
                return
            except TelegramError:
                # A stale photo reference must not prevent the welcome itself.
                pass
        await message.reply_text(text, parse_mode="HTML")

    async def welcome_members(self, update, context):
        if not await self.db.approved(update.effective_chat.id):
            return
        for user in update.effective_message.new_chat_members[:5]:
            if not user.is_bot:
                await self.welcome(update.effective_message, user)

    async def command(self, update, context):
        if not update.effective_user or not update.effective_message:
            return
        message, uid = update.effective_message, update.effective_user.id
        cmd = message.text.split()[0].split("@")[0][1:].lower()
        if cmd == "search":
            return await self.search(update, context, " ".join(context.args))
        if cmd in ("start", "help"):
            if message.chat.type != "private":
                if not await self.db.approved(message.chat.id):
                    return
            self.limiter.check([(("command", uid), 20)])
            await self.db.seen(uid)
            if cmd == "start":
                await self.welcome(message, update.effective_user)
            else:
                await message.reply_text(HELP)
                if await self.permissions.staff(uid):
                    await message.reply_text(ADMIN_HELP)
            return
        permitted = await self.permissions.staff(uid) if cmd in STAFF_COMMANDS else self.permissions.root(uid)
        if not permitted:
            try:
                self.limiter.check([(("denied", uid), 3)])
            except Busy:
                return
            await message.reply_text("This action requires owner permission.")
            return
        self.limiter.check([(("admin", uid), 30)])
        try:
            if cmd == "menu":
                if message.chat.type != "private":
                    raise ValueError("Open /menu in a private chat with the bot.")
                text, markup = await self.menu("home", 0)
                await message.reply_text(text, reply_markup=markup)
            elif cmd in ("sudo",):
                rows = await self.db.listing("sudo", 0)
                await message.reply_text(
                    "Sudo users\n" + ("\n".join(str(r["id"]) for r in rows) or "None") + "\nUse /menu for pagination."
                )
            elif cmd in (
                "addsudo",
                "delsudo",
                "approve",
                "unapprove",
                "addsource",
                "delsource",
                "setstore",
                "delstore",
            ):
                category = {
                    "addsudo": "sudo",
                    "delsudo": "sudo",
                    "approve": "group",
                    "unapprove": "group",
                    "addsource": "source",
                    "delsource": "source",
                    "setstore": "storage",
                    "delstore": "storage",
                }[cmd]
                if category == "sudo":
                    reply = message.reply_to_message
                    if context.args:
                        target = positive_id(context.args[0])
                    elif reply and reply.from_user and not reply.from_user.is_bot:
                        target = reply.from_user.id
                    else:
                        raise ValueError("Supply a user ID or reply to a user message.")
                    if self.permissions.root(target):
                        raise ValueError("Root owners are configured only through OWNER_IDS.")
                else:
                    validator = group_id if category == "group" else channel_id
                    target = validator(context.args[0]) if context.args else validator(str(message.chat.id))
                add = cmd in ("addsudo", "approve", "addsource", "setstore")
                if add and category in ("source", "storage"):
                    if len(await self.db.channels(category)) >= self.settings.max_sources:
                        raise ValueError("Channel limit reached. Remove an unused channel first.")
                    await self.source.validate(target)
                    chat = await context.bot.get_chat(target)
                    member = await context.bot.get_chat_member(target, context.bot.id)
                    if (
                        chat.type != "channel"
                        or chat.has_protected_content
                        or member.status not in ("member", "administrator", "creator")
                    ):
                        raise ValueError("Both accounts need access to an unprotected channel.")
                if add:
                    await self.db.change_member(category, target, uid)
                    self.search_service.cache.clear()
                    await message.reply_text("✓ Saved.")
                else:
                    await self.confirm(message, uid, ("remove", category, target), f"Remove {category} {target}?")
            elif cmd == "setwelcome":
                text = message.text.partition(" ")[2] or (
                    (message.reply_to_message.text_html or "") if message.reply_to_message else ""
                )
                validate_template(text)
                await self.db.set_setting("welcome_text", text, uid)
                await message.reply_text("✓ Welcome text saved. Use /previewwelcome.")
            elif cmd == "previewwelcome":
                await self.welcome(message, update.effective_user, True)
            elif cmd == "setphoto":
                reply = message.reply_to_message
                if not reply or not reply.photo:
                    raise ValueError("Reply to a Telegram photo with /setphoto.")
                photo = reply.photo[-1].file_id
                await message.reply_photo(photo, caption="Welcome photo preview")
                await self.db.set_setting("welcome_photo", photo, uid)
                await message.reply_text("✓ Welcome photo saved.")
            elif cmd in ("delphoto", "resetwelcome"):
                await self.confirm(message, uid, (cmd,), "Confirm welcome setting reset?")
            elif cmd == "configure":
                if len(context.args) != 2 or context.args[0] not in TUNABLES:
                    raise ValueError("Use /configure <search_limit|cache_ttl|user_rate|chat_rate> <integer>.")
                key, raw = context.args
                value = int(raw)
                low, high = TUNABLES[key]
                if key == "search_limit":
                    high = self.settings.search_limit
                if not low <= value <= high:
                    raise ValueError(f"{key} must be between {low} and {high}.")
                await self.db.set_setting(key, value, uid)
                if key == "cache_ttl":
                    self.search_service.cache.ttl = value
                self.search_service.cache.clear()
                await message.reply_text("✓ Configuration saved.")
        except (ValueError, TelegramError) as exc:
            # Never include Telegram exception details, which can contain channel identifiers.
            text = (
                str(exc)
                if isinstance(exc, ValueError)
                else "Telegram access check failed. Check account membership and permissions."
            )
            await message.reply_text(text)

    async def confirm(self, message, uid, action, text):
        token = secrets.token_urlsafe(8)
        sent = await message.reply_text(
            text,
            reply_markup=Markup(
                [[Button("Confirm", callback_data=f"c:{token}:yes"), Button("Cancel", callback_data=f"c:{token}:no")]]
            ),
        )
        self.confirmations.put(token, (uid, message.chat.id, sent.message_id, action))

    async def menu(self, section, offset):
        back = [Button("‹ Dashboard", callback_data="m:home:0")]
        if section in ("source", "storage", "group", "sudo"):
            rows = await self.db.listing(section, offset, 9)
            text = section.title() + "\n" + ("\n".join(str(r["id"]) for r in rows[:8]) or "None configured.")
            command = {"source": "addsource", "storage": "setstore", "group": "approve", "sudo": "addsudo"}[section]
            text += f"\nAdd with /{command} <numeric_id>.\nSelect a row to remove it."
            buttons = [[Button(f"Remove {r['id']}", callback_data=f"r:{section}:{r['id']}")] for r in rows[:8]]
            nav = []
            if offset:
                nav.append(Button("Previous", callback_data=f"m:{section}:{max(0, offset - 8)}"))
            if len(rows) > 8:
                nav.append(Button("Next", callback_data=f"m:{section}:{offset + 8}"))
            if nav:
                buttons.append(nav)
            return text, Markup(buttons + [back])
        if section == "welcome":
            return (
                "Welcome settings\n"
                + await self.option("welcome_text", self.settings.default_welcome)
                + "\n\nUse /setwelcome, /setphoto, /delphoto, /resetwelcome, or /previewwelcome."
            ), Markup([back])
        if section == "config":
            values = {k: await self.option(k, getattr(self.settings, k)) for k in TUNABLES}
            return "\n".join(
                f"{k}: {v}" for k, v in values.items()
            ) + "\nChange with /configure <key> <value>.", Markup([back])
        if section == "errors":
            return "Recent error types (this process)\n" + ("\n".join(self.errors) or "No errors recorded."), Markup(
                [back]
            )
        if section != "home":
            raise ValueError("Invalid menu section.")
        stats = await self.db.stats()
        enabled = await self.option("search_enabled", True)
        private = await self.option("private_search", self.settings.private_search)
        text = (
            "Owner dashboard\n"
            f"Uptime: {int(time.monotonic() - self.boot)} seconds\n"
            f"Database: {'healthy' if await self.db.health() else 'unavailable'}\n"
            f"User session: {'connected' if self.source.client.is_connected() else 'disconnected'}\n"
            + "\n".join(f"{k.title()}: {v}" for k, v in stats.items())
            + f"\nCached searches: {len(self.search_service.cache.data)}"
        )
        return text, Markup(
            [
                [Button("Sources", callback_data="m:source:0"), Button("Storage", callback_data="m:storage:0")],
                [Button("Groups", callback_data="m:group:0"), Button("Sudo users", callback_data="m:sudo:0")],
                [Button("Welcome", callback_data="m:welcome:0"), Button("Search limits", callback_data="m:config:0")],
                [
                    Button(f"Search: {'on' if enabled else 'off'}", callback_data="t:search_enabled"),
                    Button(f"Private: {'on' if private else 'off'}", callback_data="t:private_search"),
                ],
                [Button("Clear cache", callback_data="x:cache"), Button("Recent errors", callback_data="m:errors:0")],
                [Button("Refresh", callback_data="m:home:0")],
            ]
        )

    def result_markup(self, token, results, number):
        rows, pages = page(results, number, self.settings.page_size)
        buttons = [
            [Button(f"{f['title'][:48]} · {f['size'] / 1048576:.1f} MB", callback_data=f"f:{token}:{f['id']}")]
            for f in rows
        ]
        nav = []
        if number:
            nav.append(Button("‹ Previous", callback_data=f"p:{token}:{number - 1}"))
        if number + 1 < pages:
            nav.append(Button("Next ›", callback_data=f"p:{token}:{number + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([Button("Clear search", callback_data=f"z:{token}:0")])
        return Markup(buttons), pages

    async def search(self, update, context, text=None):
        if not update.effective_user:
            return
        message = update.effective_message
        if (
            text is None
            and message.chat.type in ("group", "supergroup")
            and not await self.db.approved(message.chat.id)
        ):
            return
        try:
            await self.limits(update)
            await self.allowed(update)
            query = Query.parse(text if text is not None else message.text, self.settings.query_length)
            await self.db.seen(update.effective_user.id)
            await self.db.metric("searches")
            limit = min(await self.option("search_limit", self.settings.search_limit), self.settings.search_limit)
            results, unavailable = await self.search_service.search(query, limit)
            if not results:
                await message.reply_text(
                    "Sources are temporarily unavailable. Please try later."
                    if unavailable
                    else "No files found. Try the title, year, or a different spelling."
                )
                return
            token = secrets.token_urlsafe(8)
            markup, pages = self.result_markup(token, results, 0)
            title = f"{len(results)} results · page 1/{pages}" + (
                "\nSome sources are unavailable." if unavailable else ""
            )
            sent = await message.reply_text(title, reply_markup=markup)
            self.sessions.put(
                token,
                {"uid": update.effective_user.id, "cid": message.chat.id, "mid": sent.message_id, "results": results},
            )
        except (ValueError, Busy) as exc:
            key = (update.effective_user.id, message.chat.id)
            if self.notices.get(key) is None:
                self.notices.put(key, True)
                await message.reply_text(str(exc))

    async def callback(self, update, context):
        cb, uid = update.callback_query, update.effective_user.id
        if not cb.message or not cb.data or len(cb.data) > 64:
            return await cb.answer("This button is unavailable.", show_alert=True)
        parts = cb.data.split(":")
        try:
            if parts[0] in ("p", "f", "z"):
                if len(parts) != 3:
                    raise ValueError("Invalid result button.")
                action, token, raw = parts
                session = self.sessions.get(token)
                if not session or (uid, cb.message.chat.id, cb.message.message_id) != (
                    session["uid"],
                    session["cid"],
                    session["mid"],
                ):
                    raise ValueError("This search belongs to another user or has expired. Start a new search.")
                await self.allowed(update)
                await self.limits(update)
                value = int(raw)
                await cb.answer()
                if action == "p":
                    markup, pages = self.result_markup(token, session["results"], value)
                    await cb.edit_message_text(
                        f"{len(session['results'])} results · page {value + 1}/{pages}", reply_markup=markup
                    )
                elif action == "z":
                    self.sessions.data.pop(token, None)
                    await cb.edit_message_text("Search cleared.")
                elif value in {f["id"] for f in session["results"]}:
                    await context.bot.send_chat_action(session["cid"], "upload_document")
                    await self.delivery.send(context.bot, uid, session["cid"], value)
                else:
                    raise ValueError("Invalid file selection.")
                return
            if parts[0] == "c":
                if len(parts) != 3 or parts[2] not in ("yes", "no"):
                    raise ValueError("Invalid confirmation.")
                item = self.confirmations.get(parts[1])
                if not item or item[:3] != (uid, cb.message.chat.id, cb.message.message_id):
                    raise ValueError("This confirmation expired or belongs to another user.")
                action = item[3]
                requires_root = action[0] == "cache" or (action[0] == "remove" and action[1] != "group")
                permitted = self.permissions.root(uid) if requires_root else await self.permissions.staff(uid)
                if not permitted:
                    raise ValueError("Your permission for this action has been revoked.")
                self.confirmations.data.pop(parts[1], None)
                await cb.answer()
                if parts[2] == "no":
                    return await cb.edit_message_text("Cancelled.")
                if action[0] == "remove":
                    await self.db.change_member(action[1], action[2], uid, False)
                    self.search_service.cache.clear()
                elif action[0] == "delphoto":
                    await self.db.set_setting("welcome_photo", "", uid)
                elif action[0] == "resetwelcome":
                    await self.db.set_setting("welcome_text", self.settings.default_welcome, uid)
                    await self.db.set_setting("welcome_photo", self.settings.default_photo, uid)
                elif action[0] == "cache":
                    self.search_service.cache.clear()
                    self.sessions.clear()
                    await self.db.audit(uid, "clear_cache", "search")
                return await cb.edit_message_text("✓ Saved.")
            if not self.permissions.root(uid) or cb.message.chat.type != "private":
                raise ValueError("This panel is restricted to root owners in private chats.")
            self.limiter.check([(("admin", uid), 30)])
            await cb.answer()
            if parts[0] == "m" and len(parts) == 3:
                offset = int(parts[2])
                if not 0 <= offset <= 100000 or offset % 8:
                    raise ValueError("Invalid page.")
                text, markup = await self.menu(parts[1], offset)
                await cb.edit_message_text(text, reply_markup=markup)
            elif parts[0] == "r" and len(parts) == 3 and parts[1] in ("source", "storage", "group", "sudo"):
                validator = {"sudo": positive_id, "group": group_id}.get(parts[1], channel_id)
                target = validator(parts[2])
                if parts[1] == "sudo" and self.permissions.root(target):
                    raise ValueError("Root owners cannot be modified here.")
                await self.confirm(cb.message, uid, ("remove", parts[1], target), f"Remove {parts[1]} {target}?")
            elif parts[0] == "t" and len(parts) == 2 and parts[1] in ("search_enabled", "private_search"):
                key = parts[1]
                default = self.settings.private_search if key == "private_search" else True
                await self.db.set_setting(key, not await self.option(key, default), uid)
                text, markup = await self.menu("home", 0)
                await cb.edit_message_text(text, reply_markup=markup)
            elif parts == ["x", "cache"]:
                await self.confirm(cb.message, uid, ("cache",), "Clear cached searches and active result buttons?")
            else:
                raise ValueError("Invalid owner button.")
        except (ValueError, Busy) as exc:
            await cb.answer(str(exc)[:200], show_alert=True)
        except TelegramError:
            await cb.answer("Telegram could not update this message. Open /menu or search again.", show_alert=True)
