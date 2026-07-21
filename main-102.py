import os
import json
import re
import time
import glob
import queue
import base64
import inspect
import logging
import threading
import asyncio
import shutil
import jdatetime
from zoneinfo import ZoneInfo
import pytesseract
from PIL import Image, ImageOps
from rubka import Robot
from rubka.context import Message
from rubka.button import InlineBuilder

logger = logging.getLogger(__name__)


# ===========================================================
# رجیستری مشترک بین همه ربات‌ها (چون همه توی یک پروسه، هر کدوم
# روی یک ترد جدا اجرا می‌شن). با این رجیستری، وقتی روی ربات اصلی
# (bot1) یه تنظیم عوض بشه، می‌تونیم همون تغییر رو روی تنظیمات
# ربات‌های دیگه هم اعمال کنیم.
# ===========================================================
_ALL_BOTS_REGISTRY = []  # list[dict]: label, settings, save_settings, bot, loop
_registry_lock = threading.Lock()


# ===========================================================
# جوین اجباری واقعی (چک زنده‌ی عضویت کانال با self-bot pyrubi)
# ===========================================================
# چون sender_id که از رابکا (فضای بات‌پلتفرم) میاد با GUID واقعیِ کاربر
# (فضای pyrubi/self-bot) قابل مقایسه‌ی مستقیم نیست، اول از author_info
# پیام یوزرنیم کاربر رو در میاریم و بعد با pyrubi (که با یه اکانت واقعی
# لاگین کرده) اون یوزرنیم رو به GUID واقعی resolve می‌کنیم و لیست واقعی
# اعضای کانال رو می‌گیریم. این زیرساخت بین همه‌ی ربات‌ها (bot1..bot5)
# مشترکه چون همه‌شون توی یک پروسه‌ن و فقط یک اکانت سلف‌بات لازمه.

PYRUBI_SESSION_NAME = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "pyrubi_acc"
)

_pyrubi_login_state = {}  # in_progress, code_queue

MEMBERS_CACHE_TTL = 90  # sanie - moddat cache shodane liste azaye kanal
_members_cache = {"channel_guid": None, "guids": None, "fetched_at": 0.0}
_members_cache_lock = threading.Lock()

_REAL_GUID_ATTR_CANDIDATES = ("author_info", "author", "author_object", "author_name")
_GUID_KEY_CANDIDATES = (
    "object_guid", "objectGuid", "channel_guid", "channelGuid",
    "chat_guid", "chatGuid", "from_object_guid", "fromObjectGuid",
    "guid", "id",
)
_FORWARD_ATTR_CANDIDATES = (
    "forwarded_from", "forward_from", "forwarded", "forward",
    "forward_message", "forwarded_message_info", "forward_from_chat",
    "forwarded_message", "original_sender", "forward_info", "forward_chat",
)


def _spawn(coro):
    """coroutine ro be onvane ye asyncio Task jodagane ejra mikone (na await
    mostaghim) ta bot bedoone block shodan be baghie ham javab bede."""
    task = asyncio.create_task(coro)

    def _log_exc(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logger.exception("khataye modiriyat-nashode dar task-e pas-zamine", exc_info=exc)

    task.add_done_callback(_log_exc)
    return task


def _restore_pyrubi_session():
    """age PYRUBI_SESSION_B64 too Railway set shode bashe, faile session ro
    dobare roo disk misaze ta ba'd az har redeploy majbur be login dobare
    nabashim (chon join ejbari vaghean be hamin session vabaste)."""
    b64 = os.environ.get("PYRUBI_SESSION_B64", "")
    if not b64:
        return
    session_path = PYRUBI_SESSION_NAME + ".pyrubi"
    if os.path.exists(session_path):
        return
    try:
        raw = base64.b64decode(b64)
        with open(session_path, "wb") as f:
            f.write(raw)
        print("[pyrubi] session az roo PYRUBI_SESSION_B64 bazsazi shod: " + session_path)
    except Exception as e:
        print("[pyrubi] khata dar bazsazi session az PYRUBI_SESSION_B64: " + str(e))


def _dig_guid(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value if value[:2] in ("c0", "g0", "b0", "u0") else None
    if isinstance(value, dict):
        for k in _GUID_KEY_CANDIDATES:
            v = value.get(k)
            if isinstance(v, str) and v:
                return v
        return None
    for k in _GUID_KEY_CANDIDATES:
        v = getattr(value, k, None)
        if isinstance(v, str) and v:
            return v
    return None


def _dig_username(value):
    if isinstance(value, dict):
        v = value.get("username")
        if isinstance(v, str) and v:
            return v
        for sub in value.values():
            if isinstance(sub, dict):
                found = _dig_username(sub)
                if found:
                    return found
        return None
    v = getattr(value, "username", None)
    return v if isinstance(v, str) and v else None


def _safe_getattr_skip_coroutine(obj, attr):
    try:
        val = getattr(obj, attr)
    except Exception:
        return None
    if inspect.iscoroutine(val):
        val.close()
        return None
    return val


def _extract_forward_channel_guid(message):
    """age payam forward shode bashe, GUID mabda (kanale asli) ro dar miare -
    baraye fallback vaghti check zende mumken nabashe."""
    for attr in _FORWARD_ATTR_CANDIDATES:
        guid = _dig_guid(_safe_getattr_skip_coroutine(message, attr))
        if guid:
            return guid
    try:
        for attr in [a for a in dir(message) if not a.startswith("_") and "forward" in a.lower()]:
            guid = _dig_guid(_safe_getattr_skip_coroutine(message, attr))
            if guid:
                return guid
    except Exception:
        pass
    return None


def _pyrubi_fetch_all_member_guids_blocking(channel_guid):
    """kolle GUIDhaye azaye adi + admin/sazandeye kanal ro ba pyrubi migire."""
    from pyrubi import Client
    client = Client(PYRUBI_SESSION_NAME)
    guids = set()
    for method_name in ("get_all_members", "get_admin_members"):
        fn = getattr(client, method_name)
        start_id = None
        pages = 0
        while True:
            pages += 1
            result = fn(channel_guid, start_id=start_id)
            if isinstance(result, list):
                members = result
                has_continue = False
                next_start_id = None
            else:
                members = result.get("in_chat_members") or []
                has_continue = result.get("has_continue")
                next_start_id = result.get("next_start_id")
            for item in members:
                g = item.get("member_guid") if isinstance(item, dict) else item
                if g:
                    guids.add(str(g))
            if not has_continue or not next_start_id or pages > 40:
                break
            start_id = next_start_id
    return guids


def _pyrubi_is_member_blocking(channel_guid, user_guid):
    """chek vagheiye ozviyat ba cache kootah-moddat. javabe 'ozve' ghabele
    e'temade az cache, vali javabe 'ozv nist' hamishe ba liste taze (bedoone
    cache) tayid mishe ta karbare taze-join-shode eshtebahan rad nashe."""
    now = time.time()
    with _members_cache_lock:
        cache_valid = (
            _members_cache["channel_guid"] == channel_guid
            and _members_cache["guids"] is not None
            and (now - _members_cache["fetched_at"]) < MEMBERS_CACHE_TTL
        )
        if cache_valid and str(user_guid) in _members_cache["guids"]:
            return True

    guids = _pyrubi_fetch_all_member_guids_blocking(channel_guid)
    with _members_cache_lock:
        _members_cache["channel_guid"] = channel_guid
        _members_cache["guids"] = guids
        _members_cache["fetched_at"] = time.time()
    return str(user_guid) in guids


def _pyrubi_resolve_user_by_username_blocking(username):
    from pyrubi import Client
    client = Client(PYRUBI_SESSION_NAME)
    result = client.get_chat_info_by_username(username)
    if isinstance(result, dict) and result.get("exist"):
        user = result.get("user") or {}
        guid = user.get("user_guid")
        if isinstance(guid, str) and guid:
            return guid
    return None


async def _resolve_real_sender_guid(message):
    """az author_info/author_name dombale ye username migarde, ba'ad ba
    pyrubi (get_chat_info_by_username) GUID vagheiye hesab ro resolve mikone.
    age username nabashe, None bar migardune ta caller be flag/forward
    fallback bere."""
    username = None
    for attr in _REAL_GUID_ATTR_CANDIDATES:
        try:
            val = getattr(message, attr, None)
        except Exception:
            continue
        if val is None:
            continue
        if inspect.iscoroutine(val):
            try:
                val = await val
            except Exception:
                continue
        guid = _dig_guid(val)
        if guid:
            return guid
        if not username:
            username = _dig_username(val)

    if username and os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi"):
        try:
            real_guid = await asyncio.to_thread(_pyrubi_resolve_user_by_username_blocking, username)
        except Exception:
            logger.exception("khata dar resolve kardane username '" + str(username) + "' ba pyrubi")
            return None
        return real_guid
    return None


def _pyrubi_resolve_channel_username_blocking(username):
    from pyrubi import Client
    client = Client(PYRUBI_SESSION_NAME)
    result = client.check_channel_username(username)
    return result


def _pyrubi_login_worker(phone, code_queue, result_queue):
    import builtins
    original_input = builtins.input

    def fake_input(prompt=""):
        p = (prompt or "").lower()
        if "phone" in p or "شماره" in p:
            return phone
        if "correct" in p or "[y or n]" in p or "y/n" in p:
            return "y"
        return code_queue.get()

    builtins.input = fake_input
    try:
        from pyrubi import Client
        client = Client(PYRUBI_SESSION_NAME)
        try:
            client.get_me()
        except Exception:
            pass
        result_queue.put(("ok", None))
    except Exception as e:
        result_queue.put(("error", str(e)))
    finally:
        builtins.input = original_input
        _pyrubi_login_state["in_progress"] = False


def make_verification_bot(
    bot_label,
    settings_filename,
    token_env=None,
    super_admin_env=None,
    direct_token=None,
    direct_admin=None,
):
    """
    ربات احراز هویت با پنل ادمین کامل:
    - /start -> پیام ثبت‌نام
    - کاربر اسکرین‌شات می‌ده -> پیام "در حال بررسی" + فوروارد برای ادمین بررسی‌کننده
    - تایید/رد کاربر، پیام همگانی، آمار، چند ادمین همزمان

    token / admin ro mishe be 2 tarigh moshakhas kard:
    - token_env / super_admin_env: az environment variable khoonde mishe (baraye robat haye
      az ghabl tarif shode dar code, masalan bot1, bot2)
    - direct_token / direct_admin: mostaghiman pass mishe, age bekhay bedoone environment
      variable ham robat besazi.

    baraye ezafe kardane robate jadid, kafie ye run_botN jadid ba
    make_verification_bot besazi (mesle bot2 paeen) va ye token_env /
    super_admin_env jadid ham dar Railway (environment variables) tanzim koni.
    """

    def run():
        token = direct_token if direct_token else (os.environ.get(token_env) if token_env else None)
        if not token:
            print("[" + bot_label + "] token tanzim nashode, in bot run nemishe.")
            return

        if direct_admin is not None:
            seed_admin = direct_admin.strip() if isinstance(direct_admin, str) else direct_admin
        else:
            seed_admin = os.environ.get(super_admin_env, "").strip() if super_admin_env else ""

        default_settings = {
            "start_message": (
                "✅ این ربات واقعی و فعاله، نگران نباش! کافیه مراحل زیر رو کامل کنی:\n\n"
                "سلام! 👋\n\n"
                "برای دریافت فیلم‌ها، اول از طریق لینک زیر ثبت‌نام و احراز هویت کن:\n\n"
                "🔗 https://milli.gold/app/sign-up?referralCode=milli-mlun7\n\n"
                "بعد، این دو اسکرین‌شات رو برام بفرست:\n"
                "1️⃣ صفحه پروفایل (که احراز هویت‌شده باشه)\n"
                "2️⃣ صفحه تاریخچه (که تراکنش هدیه‌ی معرف داخلش باشه)\n\n"
                "بعد از بررسی و تأیید، دسترسی فیلم‌ها برات فعال می‌شه. ✅"
            ),
            "start_image_url": "",
            "start_image_path": "",
            "screenshot_reply": "اسکرین شاتت دریافت شد، در حال بررسی هست. نتیجه رو بهت اطلاع می دم.",
            "approved_message": "تبریک! ثبت نامت تایید شد.",
            "rejected_message": "متاسفانه ثبت نامت تایید نشد. لطفا دوباره تلاش کن.",
            "old_registration_message": (
                "به نظر می‌رسه این تراکنش مربوط به یه ثبت‌نام قبلیه که قبل از استارت الانت انجام شده 🙂\n\n"
                "برای دریافت فیلم‌ها لازمه با یه کد ملی جدید از طریق لینک زیر ثبت‌نام کنی و بعد دوباره اسکرین‌شات‌ها رو برام بفرستی:\n"
                "https://milli.gold/app/sign-up?referralCode=milli-mlun7"
            ),
            "review_admin_chat_id": seed_admin,
            "admins": [seed_admin] if seed_admin else [],
            "bot_display_name": "",  # esme namayeshi (masalan @botjdjdjd) baraye gozaresh haye chand robat
            "start_delete_seconds": 60,  # ba'd az chand sanie payame start (aks+matn) khodkar pak beshe
            "start_cooldown_seconds": 10,  # zed-espam: fasele minimum beine 2 ta /start az yek karbar
            "users": {},
            "stats": {"total_screenshots": 0, "approved": 0, "rejected": 0},
            # ---- join ejbari vaghei ----
            "channel_guid": "",       # GUID kanal (masalan c0xxxxxxxxxxxxxxxx)
            "channel_link": "",       # link amoomiye kanal baraye neshoon dadan be karbar
            "realcheck_enabled": "1",  # 1 = talash kon check zende (self-bot) bezan
            "join_delete_seconds": 60,  # ba'd az chand sanie payame "join ejbari" khodkar pak beshe (0 = ghodghod nashe)
            "joined_flags": {},       # chat_id -> True, sabt shode az tarighe check zende ya forward
            "real_guid_cache": {},    # chat_id -> GUID vaghei (baraye jelogiri az resolve tekrari)
        }

        def load_settings():
            if os.path.exists(settings_filename):
                with open(settings_filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in default_settings.items():
                        data.setdefault(k, v)
                    return data
            return default_settings.copy()

        def save_settings(data):
            with open(settings_filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        settings = load_settings()

        bot = Robot(token)

        # in bot ro too registrye moshtarek sabt mikonim ta robate asli
        # (bot1) betoone tanzimat va broadcast ro roo hameye robat ha
        # ham menfak kone.
        _registry_entry = {
            "label": bot_label,
            "settings": settings,
            "save_settings": save_settings,
            "bot": bot,
            "loop": None,
        }
        with _registry_lock:
            _ALL_BOTS_REGISTRY.append(_registry_entry)

        def _other_bots():
            with _registry_lock:
                return [e for e in _ALL_BOTS_REGISTRY if e["label"] != bot_label]

        def sync_to_all_bots(update_dict):
            """faghat robate asli (bot1) in kar ro mikone: hamun taghirat ro
            roo tanzimate hameye robat haye dige ham save mikone."""
            if bot_label != "bot1":
                return
            for entry in _other_bots():
                entry["settings"].update(update_dict)
                entry["save_settings"](entry["settings"])

        def sync_start_image_file_to_all_bots(source_path):
            """copy kardane khode faile aks be settings_file/path haye
            robat haye dige, chon masir e file baraye har robat farghe."""
            if bot_label != "bot1" or not source_path or not os.path.exists(source_path):
                return
            for entry in _other_bots():
                dest_path = "start_image_" + entry["label"] + ".jpg"
                try:
                    shutil.copyfile(source_path, dest_path)
                    entry["settings"]["start_image_path"] = dest_path
                    entry["settings"]["start_image_url"] = ""
                    entry["save_settings"](entry["settings"])
                except Exception as e:
                    print("[" + bot_label + "] khata dar copy kardane start_image baraye " + entry["label"] + ": " + str(e))

        async def broadcast_to_all_bots(broadcast_text):
            """faghat robate asli (bot1) in kar ro mikone: hamun payam ro
            baraye karbaraye hameye robat haye dige ham (az tarighe
            khode oon robat) ersal mikone. javab: (movafagh, namovafagh)"""
            if bot_label != "bot1":
                return 0, 0

            async def _send_via(entry):
                their_bot = entry["bot"]
                their_users = entry["settings"].get("users", {})
                ok, bad = 0, 0
                for uid in list(their_users.keys()):
                    try:
                        await their_bot.send_message(chat_id=uid, text=broadcast_text)
                        ok += 1
                    except Exception:
                        bad += 1
                return ok, bad

            total_ok, total_bad = 0, 0
            for entry in _other_bots():
                loop = entry.get("loop")
                if not loop:
                    continue
                try:
                    fut = asyncio.run_coroutine_threadsafe(_send_via(entry), loop)
                    ok, bad = await asyncio.wrap_future(fut)
                    total_ok += ok
                    total_bad += bad
                except Exception as e:
                    print("[" + bot_label + "] khata dar broadcast be " + entry["label"] + ": " + str(e))
            return total_ok, total_bad

        awaiting_start_image = set()  # chat_id haye admin ke montazere ersale aks hastan
        _last_start_at = {}  # chat_id -> timestamp, baraye zed-espam roo /start

        def is_admin(chat_id):
            return chat_id in settings.get("admins", [])

        # ------------------------------------------------
        # join ejbari vaghei (check zende ba self-bot pyrubi + fallback forward)
        # ------------------------------------------------
        def has_joined(chat_id):
            return bool(settings.get("joined_flags", {}).get(str(chat_id)))

        def mark_joined(chat_id):
            flags = settings.setdefault("joined_flags", {})
            was_new = str(chat_id) not in flags
            flags[str(chat_id)] = True
            save_settings(settings)
            return was_new

        def get_cached_real_guid(chat_id):
            return settings.get("real_guid_cache", {}).get(str(chat_id))

        def save_real_guid(chat_id, guid):
            cache = settings.setdefault("real_guid_cache", {})
            cache[str(chat_id)] = guid
            save_settings(settings)

        async def check_membership_live_with_message(message: Message) -> bool:
            """check asli: age channel_guid tanzim nashode, hame azad hastan.
            vagarna, aval talash mikone check zende (self-bot) bezane; age
            nashod (self-bot login nist ya khata dad), be flag has_joined
            (ke az tarighe forward post kanal ham sabt mishe) fallback mikone."""
            channel_guid = settings.get("channel_guid")
            if not channel_guid:
                return True

            chat_id = message.chat_id
            if settings.get("realcheck_enabled", "1") == "1" and os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi"):
                real_guid = get_cached_real_guid(chat_id)
                if not real_guid:
                    real_guid = await _resolve_real_sender_guid(message)
                    if real_guid:
                        save_real_guid(chat_id, real_guid)
                if real_guid:
                    try:
                        live_result = await asyncio.to_thread(_pyrubi_is_member_blocking, channel_guid, real_guid)
                    except Exception:
                        print("[" + bot_label + "] khata dar check zendeye ozviyat - fallback be flag has_joined")
                    else:
                        if live_result:
                            mark_joined(chat_id)
                            return True
                        return False  # check zende ghat'an mige ozv nist

            return has_joined(chat_id)

        def not_joined_keypad():
            b = InlineBuilder().row(
                InlineBuilder().button_simple(id="check_join", text="✅ بررسی عضویت")
            )
            return b.build()

        async def _auto_delete_join_message(bot: Robot, chat_id, message_id, delay_seconds):
            try:
                await asyncio.sleep(delay_seconds)
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception as e:
                    print("[" + bot_label + "] khata dar delete kardane payam join ejbari: " + str(e))

                expired_keypad = InlineBuilder().row(
                    InlineBuilder().button_simple(id="check_join", text="✅ بررسی عضویت")
                ).build()
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text="⏳ پیام قبلی منقضی شد و برای امنیت پاک شد.\nبعد از عضویت، دکمه‌ی زیر رو بزن 🔁",
                        inline_keypad=expired_keypad,
                    )
                except Exception as e:
                    print("[" + bot_label + "] khata dar ersal payam join expired: " + str(e))
            except Exception as e:
                print("[" + bot_label + "] khata dar auto-delete join task: " + str(e))

        async def send_not_joined(message: Message):
            link = settings.get("channel_link")
            text = "🔒 برای استفاده از ربات، اول باید عضو کانال بشی، بعد دکمه‌ی زیر رو بزن."
            if link:
                text += "\n🔗 " + link
            try:
                sent = await message.reply(text, inline_keypad=not_joined_keypad())
            except Exception as e:
                print("[" + bot_label + "] khata dar send_not_joined: " + str(e))
                return

            delay_seconds = settings.get("join_delete_seconds", 60)
            message_id = _extract_message_id(sent)
            if message_id and delay_seconds:
                asyncio.create_task(
                    _auto_delete_join_message(bot, message.chat_id, message_id, delay_seconds)
                )

        async def handle_check_join_callback(bot: Robot, message: Message):
            channel_guid = settings.get("channel_guid")
            if not channel_guid:
                await send_start_screen(bot, message)
                return
            try:
                await message.reply("🔎 در حال بررسی عضویت...")
            except Exception:
                pass
            member = await check_membership_live_with_message(message)
            if member:
                await send_start_screen(bot, message)
                return
            await send_not_joined(message)

        async def try_mark_joined_from_forward(message: Message):
            """fallback: age karbar ye post az khode kanal forward kone,
            GUID mabda ro ba channel_guid moghayese mikonim va joined_flags
            ro sabt mikonim - baraye vaghti check zende mumken nist."""
            channel_guid = settings.get("channel_guid")
            if not channel_guid:
                return False
            forwarded_guid = _extract_forward_channel_guid(message)
            if forwarded_guid and forwarded_guid == channel_guid:
                mark_joined(message.chat_id)
                return True
            return False

        async def _watch_pyrubi_login_for_bot(bot: Robot, chat_id, result_queue):
            status, err = await asyncio.to_thread(result_queue.get)
            _pyrubi_login_state["in_progress"] = False
            if status == "ok":
                try:
                    candidates = sorted(
                        set(glob.glob(PYRUBI_SESSION_NAME + "*")),
                        key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                        reverse=True,
                    )
                    candidates = [p for p in candidates if os.path.isfile(p)]
                    if not candidates:
                        raise FileNotFoundError("hich file sessioni peida nashod")
                    session_path = candidates[0]
                    with open(session_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    # payam ro chand tike mikonim chon momkene toolani bashe
                    header = (
                        "✅ لاگین pyrubi موفق شد.\n"
                        "این متن رو کامل کپی کن و توی Railway به‌عنوان متغیر PYRUBI_SESSION_B64 ذخیره کن، بعد این پیام‌ها رو از چت پاک کن:\n\n"
                    )
                    await bot.send_message(chat_id=chat_id, text=header)
                    chunk_size = 3000
                    for i in range(0, len(b64), chunk_size):
                        await bot.send_message(chat_id=chat_id, text=b64[i:i + chunk_size])
                except Exception as e:
                    try:
                        await bot.send_message(chat_id=chat_id, text="⚠️ لاگین pyrubi شد ولی خوندن session خطا داد: " + str(e))
                    except Exception:
                        pass
            else:
                try:
                    await bot.send_message(chat_id=chat_id, text="❌ لاگین pyrubi ناموفق: " + str(err))
                except Exception:
                    pass

        def count_filter_passed(users_dict):
            """chand nafar profile_ss va history_ss hardoshun True hastan,
            yani az filter (harde screenshot) rad shodan."""
            return sum(
                1 for u in users_dict.values()
                if u.get("profile_ss") and u.get("history_ss")
            )

        def register_user(chat_id):
            users = settings.setdefault("users", {})
            if chat_id not in users:
                users[chat_id] = {"status": "pending", "screenshots": 0, "profile_ss": False, "history_ss": False}
                save_settings(settings)

        def get_file_id(file_obj):
            if file_obj is None:
                return None
            file_id = getattr(file_obj, "file_id", None)
            if file_id is not None:
                return file_id
            if isinstance(file_obj, dict):
                return file_obj.get("file_id")
            return None

        def ocr_extract_text(image_path):
            try:
                img = Image.open(image_path)
                # pish-pardazesh baraye behbood deghat OCR roo matn haye rize UI:
                # bozorg-nemayi, sepid-siah, kontrast bishtar
                img = img.convert("L")
                w, h = img.size
                if max(w, h) < 2000:
                    scale = 2
                    img = img.resize((w * scale, h * scale), Image.LANCZOS)
                img = ImageOps.autocontrast(img)
                text = pytesseract.image_to_string(img, lang="fas", config="--psm 6")
                return text
            except Exception as e:
                print("[" + bot_label + "] khata dar OCR: " + str(e))
                return ""

        def normalize_fa(s):
            if not s:
                return ""
            # yeksan-sazi harf haye arabi/farsi va hazf nim-fasele/fasele hai ezafe
            replacements = {
                "\u064a": "\u06cc",  # ي -> ی
                "\u0643": "\u06a9",  # ك -> ک
                "\u200c": " ",       # nim-fasele -> space
                "\u200f": "",        # RLM
                "\u200e": "",        # LRM
            }
            for old, new in replacements.items():
                s = s.replace(old, new)
            s = " ".join(s.split())  # collapse whitespace
            return s

        def fa_to_en_digits(s):
            if not s:
                return s
            return s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))

        PERSIAN_MONTHS = {
            "فروردین": 1, "اردیبهشت": 2, "خرداد": 3, "تیر": 4, "مرداد": 5, "شهریور": 6,
            "مهر": 7, "آبان": 8, "آذر": 9, "دی": 10, "بهمن": 11, "اسفند": 12,
        }

        def extract_transaction_time(ocr_text):
            # az roo matne OCR shode, tarikh o saate tarakonesh ro peida mikone
            # (mesal: "یکشنبه ۲۳ فروردین ۱۴۰۵ | ۱۸:۵۹") va be unix timestamp tabdil mikone
            text = fa_to_en_digits(normalize_fa(ocr_text))
            months = "|".join(PERSIAN_MONTHS.keys())
            pattern = r"(\d{1,2})\s+(" + months + r")\s+(\d{4}).{0,15}?(\d{1,2}):(\d{2})"
            m = re.search(pattern, text)
            if not m:
                return None
            try:
                day, month, year = int(m.group(1)), PERSIAN_MONTHS[m.group(2)], int(m.group(3))
                hour, minute = int(m.group(4)), int(m.group(5))
                jdt = jdatetime.datetime(year, month, day, hour, minute)
                gdt = jdt.togregorian()
                # sa'ate neshan dade shode dar app be vaghte Tehran hast,
                # bayad sarih tayin she vagarna .timestamp() ba tayimzone
                # server (mesalan UTC) eshtebah tafsir mishe (~3.5 saat khata)
                tehran_dt = gdt.replace(tzinfo=ZoneInfo("Asia/Tehran"))
                return tehran_dt.timestamp()
            except Exception:
                return None

        def detect_screenshot_type(ocr_text):
            # in kalamat ro donbal migardim (mamkene OCR kamel dorost nabashe pas chandta variation check mikonim)
            norm_text = normalize_fa(ocr_text)

            # neshane haye kolli safhe (sarf nazar az inke tayid shode ya na)
            profile_page_markers = [
                "پروفایل", "میلی پرو", "دریافت هدیه", "تنظیمات امنیتی",
                "مدیریت حساب", "اشتراک گذاری", "درباره میلی",
            ]
            history_page_markers = ["تراکنش ها", "فیلتر"]

            # shart ghati baraye tayid har safhe
            profile_verified_markers = ["احراز هویت شده", "احراز هویت", "هویت شد"]
            history_gift_words = ["هدیه", "تکمیل", "پروفایل", "معرف"]

            is_profile_page = any(normalize_fa(m) in norm_text for m in profile_page_markers)
            history_gift_found = all(normalize_fa(w) in norm_text for w in history_gift_words)
            is_history_page = any(normalize_fa(m) in norm_text for m in history_page_markers) or history_gift_found

            profile_verified = any(normalize_fa(m) in norm_text for m in profile_verified_markers)

            if is_profile_page and is_history_page:
                return {"page": "both", "profile_ok": profile_verified, "history_ok": history_gift_found}
            if is_profile_page:
                return {"page": "profile", "ok": profile_verified}
            if is_history_page:
                return {"page": "history", "ok": history_gift_found}
            return {"page": "unknown"}

        async def download_screenshot(bot: Robot, file_id):
            try:
                url = await bot.get_url_file(file_id)
                print("[" + bot_label + "] file url: " + str(url))
            except Exception as e:
                print("[" + bot_label + "] khata dar get_url_file: " + str(e))
                url = None

            local_path = "downloaded_" + str(file_id)[:20] + ".jpg"
            try:
                result = await bot.download(file_id, local_path)
                print("[" + bot_label + "] download result: " + str(result))
                if os.path.exists(local_path):
                    print("[" + bot_label + "] file save shod, size: " + str(os.path.getsize(local_path)))
                    return local_path
            except Exception as e:
                print("[" + bot_label + "] khata dar download: " + str(e))

            return None

        async def forward_screenshot_to_reviewer(bot: Robot, message: Message, file_id, ocr_text="", ss_type="unknown"):
            review_admin = settings.get("review_admin_chat_id")
            if not review_admin:
                print("[" + bot_label + "] hich review_admin tanzim nashode.")
                return

            info_text = (
                "chat_id: " + str(message.chat_id) +
                "\ntashkhis OCR: " + ss_type +
                "\nmatn OCR (100 harf aval): " + ocr_text[:100].replace("\n", " ") +
                "\nbaraye tayid dasti: /approve " + str(message.chat_id) +
                "\nbaraye rad: /reject " + str(message.chat_id)
            )

            try:
                await bot.forward_message(
                    from_chat_id=message.chat_id,
                    message_id=message.message_id,
                    to_chat_id=review_admin,
                )
                try:
                    await bot.send_message(chat_id=review_admin, text=info_text)
                except Exception:
                    pass
                return
            except Exception as e:
                print("[" + bot_label + "] khata dar forward_message: " + str(e))

            try:
                await bot.send_message(
                    chat_id=review_admin,
                    text="Screenshot jadid (nashod forward konam)\n" + info_text,
                )
            except Exception as e2:
                print("[" + bot_label + "] khata dar ersal matn be review_admin: " + str(e2))


        def _extract_message_id(sent_response):
            """talash mikonim message_id ro az javabe API ya shy'e barghardoonde
            shode az reply/reply_image dar biarim, chon format daghigh momkene
            dict('data': {...}) ya object ba attribute bashe."""
            if sent_response is None:
                return None
            if isinstance(sent_response, dict):
                data = sent_response.get("data", sent_response)
                if isinstance(data, dict):
                    return data.get("message_id") or data.get("new_message_id")
                return None
            return getattr(sent_response, "message_id", None)

        async def _auto_delete_start_message(bot: Robot, chat_id, message_id, delay_seconds):
            try:
                await asyncio.sleep(delay_seconds)
                try:
                    await bot.delete_message(chat_id, message_id)
                except Exception as e:
                    print("[" + bot_label + "] khata dar delete kardane payam start: " + str(e))

                expired_text = (
                    "⏳ پیام قبلی منقضی شد و برای امنیت پاک شد.\n\n"
                    "برای دریافت دوباره‌ی اطلاعات، فقط کافیه یک بار دیگه بزنی روی /start 🔁"
                )

                resend_keypad = InlineBuilder().row(
                    InlineBuilder().button_simple(id="resend_start", text="🔄 دریافت مجدد")
                ).build()
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=expired_text,
                        inline_keypad=resend_keypad,
                    )
                    print("[" + bot_label + "] payam-e ghashang-e resend baraye chat_id=" + str(chat_id) + " ersal shod.")
                except Exception as e:
                    print("[" + bot_label + "] khata dar ersal payam resend: " + str(e))
            except Exception as e:
                print("[" + bot_label + "] khata dar auto-delete task: " + str(e))

        async def send_start_screen(bot: Robot, message: Message):
            chat_id = message.chat_id
            register_user(chat_id)
            users = settings.setdefault("users", {})
            users.setdefault(chat_id, {"status": "pending", "screenshots": 0, "profile_ss": False, "history_ss": False})
            users[chat_id]["start_time"] = time.time()
            save_settings(settings)

            image_source = (settings.get("start_image_path") or "").strip() or (settings.get("start_image_url") or "").strip()
            sent = None
            try:
                if image_source:
                    try:
                        sent = await message.reply_image(path=image_source, text=settings["start_message"])
                    except Exception as e:
                        print("[" + bot_label + "] khata dar ersal start_image, fallback be matn saade: " + str(e))
                        sent = await message.reply(settings["start_message"])
                else:
                    sent = await message.reply(settings["start_message"])
            except Exception as e:
                print("[" + bot_label + "] khata dar start: " + str(e))
                return

            delay_seconds = settings.get("start_delete_seconds", 60)
            message_id = _extract_message_id(sent)
            if message_id and delay_seconds:
                asyncio.create_task(_auto_delete_start_message(bot, chat_id, message_id, delay_seconds))

        @bot.on_message(commands=["start"])
        async def start(bot: Robot, message: Message):
            chat_id = message.chat_id
            cooldown = settings.get("start_cooldown_seconds", 10)
            now = time.time()
            last = _last_start_at.get(chat_id, 0)
            if cooldown and (now - last) < cooldown:
                print("[" + bot_label + "] /start spam jelogiri shod baraye chat_id=" + str(chat_id))
                return
            _last_start_at[chat_id] = now

            if not is_admin(chat_id) and settings.get("channel_guid"):
                member = await check_membership_live_with_message(message)
                if not member:
                    await send_not_joined(message)
                    return

            await send_start_screen(bot, message)

        @bot.on_message(commands=["testbtn"])
        async def test_button(bot: Robot, message: Message):
            """DEBUG: dokme ro daghighan tebghe olgooye rasmi mostanadat
            rubka, ba message.reply (na bot.send_message) mifarestim, ta
            befahmim moshkel az bot.send_message(inline_keypad=...) hast
            ya az chizi digast."""
            test_keypad = InlineBuilder().row(
                InlineBuilder().button_simple(id="test_click", text="🧪 تست دکمه")
            ).build()
            try:
                await message.reply("این یه پیام تستیه، رو دکمه بزن:", inline_keypad=test_keypad)
                print("[" + bot_label + "] /testbtn dokme az tarighe message.reply ersal shod, chat_id=" + str(message.chat_id))
            except Exception as e:
                import traceback
                print("[" + bot_label + "] khata dar /testbtn: " + str(e))
                traceback.print_exc()

        def _extract_button_id(message):
            """button_id ro az har format-e mohtamel-e aux_data dar biar.
            dar rubka, aux_data mamulan yek object-e ba attribute e button_id
            hast (na dict), pas bayad ham attribute ham dict ro check konim."""
            btn_id = getattr(message, "button_id", None)
            if btn_id:
                return btn_id
            aux_data = getattr(message, "aux_data", None)
            if aux_data is None:
                return None
            if isinstance(aux_data, dict):
                return aux_data.get("button_id") or aux_data.get("id") or aux_data.get("data")
            return (
                getattr(aux_data, "button_id", None)
                or getattr(aux_data, "id", None)
                or getattr(aux_data, "data", None)
            )

        @bot.on_callback()
        async def on_any_callback(bot: Robot, message: Message):
            """debug: har callback-i ke miad ro log mikonim, hata age
            hich handler-e khasi baraye button_id-esh sabt nashode bashe.
            hamchenin fallback-e asli baraye resend_start hamin-jast."""
            btn_id = _extract_button_id(message)
            print("[" + bot_label + "] on_callback (ANY) daryafat shod, chat_id=" + str(message.chat_id) + " button_id=" + str(btn_id))
            if btn_id == "resend_start":
                try:
                    await send_start_screen(bot, message)
                    print("[" + bot_label + "] resend_start az on_any_callback ejra shod, chat_id=" + str(message.chat_id))
                except Exception as e:
                    import traceback
                    print("[" + bot_label + "] khata dar on_any_callback resend_start: " + str(e))
                    traceback.print_exc()
            elif btn_id == "check_join":
                try:
                    await handle_check_join_callback(bot, message)
                except Exception as e:
                    import traceback
                    print("[" + bot_label + "] khata dar on_any_callback check_join: " + str(e))
                    traceback.print_exc()

        @bot.on_callback("resend_start")
        async def on_resend_start(bot: Robot, message: Message):
            print("[" + bot_label + "] on_callback resend_start triggered, chat_id=" + str(message.chat_id))
            try:
                await send_start_screen(bot, message)
            except Exception as e:
                import traceback
                print("[" + bot_label + "] khata dar on_resend_start: " + str(e))
                traceback.print_exc()

        @bot.on_callback("check_join")
        async def on_check_join(bot: Robot, message: Message):
            print("[" + bot_label + "] on_callback check_join triggered, chat_id=" + str(message.chat_id))
            try:
                await handle_check_join_callback(bot, message)
            except Exception as e:
                import traceback
                print("[" + bot_label + "] khata dar on_check_join: " + str(e))
                traceback.print_exc()

        @bot.on_message()
        async def handle_message(bot: Robot, message: Message):
            try:
                await _handle_message_inner(bot, message)
            except Exception as e:
                print("[" + bot_label + "] khata dar handle_message: " + str(e))

        async def _handle_message_inner(bot: Robot, message: Message):
            # DEBUG: har chi ke miad ro bi-ghide-o-shart chap mikonim, hata
            # ghabl az har parse-i, ta bebinim click roo dokme asan be inja
            # miresad ya na, va shekle daghigh-e aux_data/raw_data chie.
            try:
                raw = getattr(message, "raw_data", None)
                print(
                    "[" + bot_label + "] RAW on_message -> chat_id=" + str(getattr(message, "chat_id", None)) +
                    " text=" + repr(getattr(message, "text", None)) +
                    " button_id_attr=" + repr(getattr(message, "button_id", None)) +
                    " aux_data=" + repr(getattr(message, "aux_data", None)) +
                    " raw_data=" + repr(raw)
                )
            except Exception as _dbg_e:
                print("[" + bot_label + "] khata dar debug print: " + str(_dbg_e))

            text = (message.text or "").strip()
            chat_id = message.chat_id

            _registry_entry["loop"] = asyncio.get_running_loop()

            # fallback: age dokme shishei az tarighe on_callback nagirim,
            # shayad hamin handler-e omoomi button_id ro bede.
            btn_id = _extract_button_id(message)
            if btn_id == "resend_start":
                print("[" + bot_label + "] (fallback) dokme resend_start az handle_message gerefte shod, chat_id=" + str(chat_id))
                try:
                    await send_start_screen(bot, message)
                except Exception as e:
                    import traceback
                    print("[" + bot_label + "] khata dar fallback resend_start: " + str(e))
                    traceback.print_exc()
                return
            if btn_id == "check_join":
                print("[" + bot_label + "] (fallback) dokme check_join az handle_message gerefte shod, chat_id=" + str(chat_id))
                try:
                    await handle_check_join_callback(bot, message)
                except Exception as e:
                    import traceback
                    print("[" + bot_label + "] khata dar fallback check_join: " + str(e))
                    traceback.print_exc()
                return

            if text == "/checkjoin_debug":
                channel_guid = settings.get("channel_guid")
                lines = []
                lines.append("channel_guid: " + str(channel_guid or "(تنظیم نشده)"))
                lines.append("channel_link: " + str(settings.get("channel_link") or "(خالی)"))
                lines.append("realcheck_enabled: " + str(settings.get("realcheck_enabled")))
                lines.append("pyrubi session موجوده: " + str(os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi")))
                if channel_guid:
                    real_guid = get_cached_real_guid(chat_id)
                    if not real_guid:
                        try:
                            real_guid = await _resolve_real_sender_guid(message)
                        except Exception as e:
                            real_guid = None
                            lines.append("خطا در resolve real_guid: " + str(e))
                    lines.append("real_guid resolve شده: " + str(real_guid or "(پیدا نشد)"))
                    if real_guid:
                        try:
                            live = await asyncio.to_thread(_pyrubi_is_member_blocking, channel_guid, real_guid)
                            lines.append("نتیجه‌ی چک زنده: " + str(live))
                        except Exception as e:
                            lines.append("خطا در چک زنده: " + str(e))
                    lines.append("joined_flag ذخیره‌شده: " + str(has_joined(chat_id)))
                await message.reply("\n".join(lines))
                return

            # fallback baraye join ejbari: age karbar ye post az kanal
            # forward karde bashe, azash be onvane tayid ozviyat estefade konim
            if not is_admin(chat_id) and settings.get("channel_guid"):
                try:
                    if await try_mark_joined_from_forward(message):
                        await message.reply("✅ عضویت شما تایید شد!")
                        await send_start_screen(bot, message)
                        return
                except Exception:
                    pass

                if not await check_membership_live_with_message(message):
                    await send_not_joined(message)
                    return

            print("[" + bot_label + "] payam az chat_id=" + str(chat_id) + " text=" + text)

            # hamashon (admin ya nabashe) mitoonan chat_id khodesho bebine
            if text == "/myid":
                await message.reply("chat_id shoma: " + str(chat_id))
                return

            # ------------------------------------------------
            # panel modiriyat - faghat baraye admin ha
            # ------------------------------------------------
            if is_admin(chat_id):
                if text == "/listmethods":
                    all_methods = [m for m in dir(bot) if not m.startswith("_")]
                    relevant = [m for m in all_methods if "file" in m.lower() or "download" in m.lower() or "media" in m.lower()]
                    await message.reply(
                        "Motod haye marbut be file:\n" + "\n".join(relevant) +
                        "\n\nHame motod ha:\n" + ", ".join(all_methods)
                    )
                    return

                if text == "/help":
                    help_text = (
                        "📋 دستورات ادمین:\n\n"
                        "/setstart <متن> - تغییر پیام استارت\n"
                        "/setstartimage <لینک> - تنظیم عکس از لینک (یا بدون لینک بفرست تا عکس رو مستقیم ازت درخواست کنه)\n"
                        "/removestartimage - حذف عکس از پیام استارت\n"
                        "/setreply <متن> - تغییر پیام «در حال بررسی»\n"
                        "/setreviewadmin <chat_id> - تعیین ادمین دریافت‌کننده‌ی اسکرین‌شات‌ها\n"
                        "/approve <chat_id> - تایید کاربر\n"
                        "/reject <chat_id> - رد کاربر\n"
                        "/broadcast <متن> - ارسال پیام همگانی به همه کاربران\n"
                        "/stats - نمایش آمار این ربات\n"
                        "/filterstats - نمایش تعداد ثبت‌نام‌کرده‌ها (روی bot1: مال همه ربات‌ها)\n"
                        "/setbotname <اسم> - تنظیم اسم نمایشی این ربات (مثلاً @botjdjdjd) برای گزارش‌ها\n"
                        "/setautodelete <ثانیه> - تنظیم زمان حذف خودکار پیام استارت (پیش‌فرض ۶۰ ثانیه)\n"
                        "/setstartcooldown <ثانیه> - تنظیم ضد اسپم بین دو تا /start از یک کاربر (پیش‌فرض ۱۰ ثانیه)\n"
                        "/addadmin <chat_id> - اضافه کردن ادمین جدید\n"
                        "/removeadmin <chat_id> - حذف ادمین\n"
                        "/gettexts - نمایش تنظیمات فعلی\n"
                        "/myid - نمایش chat_id خودت\n\n"
                        "🔒 جوین اجباری واقعی:\n"
                        "/setchannel <channel_guid> [لینک] - تنظیم کانال جوین اجباری\n"
                        "/removechannel - غیرفعال کردن جوین اجباری\n"
                        "/realcheck on|off - روشن/خاموش کردن چک زنده‌ی self-bot\n"
                        "/pyrubi_login <شماره با 98> - لاگین سلف‌بات (برای چک واقعی عضویت)\n"
                        "/pyrubi_code <کد> - وارد کردن کد تایید pyrubi\n"
                        "/pyrubi_reset - ریست سشن pyrubi\n"
                        "/setjoindelete <ثانیه> - زمان حذف خودکار پیام «جوین اجباری» (پیش‌فرض ۶۰، صفر یعنی پاک نشه)\n"
                        "/resolvechannel <یوزرنیم بدون @> - پیدا کردن GUID واقعی کانال از روی یوزرنیمش (نیاز به pyrubi_login)"
                    )
                    await message.reply(help_text)
                    return

                if text.startswith("/setjoindelete "):
                    raw_value = text[len("/setjoindelete "):].strip()
                    if not raw_value.isdigit():
                        await message.reply("لطفا یه عدد صحیح (بر حسب ثانیه) بفرست. مثال: /setjoindelete 60")
                        return
                    seconds = int(raw_value)
                    settings["join_delete_seconds"] = seconds
                    save_settings(settings)
                    sync_to_all_bots({"join_delete_seconds": seconds})
                    await message.reply(
                        "زمان حذف خودکار پیام جوین اجباری روی " + str(seconds) + " ثانیه تنظیم شد (روی همه ربات‌ها هم اعمال شد)."
                    )
                    return

                if text.startswith("/setchannel"):
                    parts = text.split(maxsplit=2)
                    if len(parts) < 2:
                        await message.reply(
                            "فرمت: /setchannel c0xxxxxxxxxxxxxxxx [لینک اختیاری]\n\n"
                            "⚠️ باید GUID واقعی کانال باشه (با c0 شروع می‌شه)، نه یوزرنیم یا لینک.\n"
                            "اگه GUID رو نداری، اول بزن: /resolvechannel یوزرنیم_کانال (بدون @)"
                        )
                        return
                    new_guid = parts[1].strip()
                    if not new_guid.startswith(("c0", "g0", "b0")):
                        await message.reply(
                            "⚠️ این مقدار GUID واقعی به نظر نمی‌رسه (باید با c0 شروع بشه).\n"
                            "برای پیدا کردن GUID واقعی کانال از روی یوزرنیمش، بزن:\n"
                            "/resolvechannel یوزرنیم_کانال (بدون @)"
                        )
                        return
                    new_link = parts[2].strip() if len(parts) > 2 else settings.get("channel_link", "")
                    settings["channel_guid"] = new_guid
                    settings["channel_link"] = new_link
                    save_settings(settings)
                    sync_to_all_bots({"channel_guid": new_guid, "channel_link": new_link})
                    await message.reply(
                        "✅ جوین اجباری روی کانال " + new_guid + " فعال شد (روی همه ربات‌ها هم اعمال شد).\n"
                        "برای چک زنده‌ی واقعی، اگه هنوز نکردی /pyrubi_login رو هم بزن."
                    )
                    return

                if text.startswith("/resolvechannel"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2:
                        await message.reply("فرمت: /resolvechannel یوزرنیم_کانال (بدون @)\nمثال: /resolvechannel MedinoX")
                        return
                    if not os.path.exists(PYRUBI_SESSION_NAME + ".pyrubi"):
                        await message.reply("❌ اول باید سلف‌بات pyrubi لاگین باشه. بزن: /pyrubi_login 989xxxxxxxxx")
                        return
                    username = parts[1].strip().lstrip("@")
                    if "rubika.ir/" in username:
                        username = username.split("rubika.ir/")[-1].strip("/")
                    try:
                        result = await asyncio.to_thread(_pyrubi_resolve_channel_username_blocking, username)
                    except Exception as e:
                        await message.reply("❌ خطا در resolve: " + str(e))
                        return
                    found_guid = None
                    if isinstance(result, dict):
                        channel = result.get("channel") or result.get("chat") or {}
                        if isinstance(channel, dict):
                            found_guid = channel.get("channel_guid") or channel.get("guid") or channel.get("object_guid")
                    if found_guid:
                        await message.reply(
                            "✅ GUID پیدا شد: " + found_guid + "\n\n"
                            "برای فعال کردن جوین اجباری بزن:\n"
                            "/setchannel " + found_guid + " https://rubika.ir/" + username
                        )
                    else:
                        await message.reply(
                            "خروجی خام pyrubi (نتونستم GUID رو خودکار پیدا کنم، این‌جا دنبال یه فیلد شبیه "
                            "channel_guid بگرد):\n" + repr(result)[:3500]
                        )
                    return

                if text == "/removechannel":
                    settings["channel_guid"] = ""
                    settings["channel_link"] = ""
                    save_settings(settings)
                    sync_to_all_bots({"channel_guid": "", "channel_link": ""})
                    await message.reply("جوین اجباری غیرفعال شد (روی همه ربات‌ها هم اعمال شد).")
                    return

                if text.startswith("/realcheck"):
                    parts = text.split()
                    if len(parts) < 2 or parts[1] not in ("on", "off"):
                        await message.reply("فرمت: /realcheck on یا /realcheck off")
                        return
                    value = "1" if parts[1] == "on" else "0"
                    settings["realcheck_enabled"] = value
                    save_settings(settings)
                    sync_to_all_bots({"realcheck_enabled": value})
                    await message.reply("چک زنده‌ی self-bot " + ("فعال" if value == "1" else "غیرفعال") + " شد.")
                    return

                if text.startswith("/pyrubi_login"):
                    if _pyrubi_login_state.get("in_progress"):
                        await message.reply("⚠️ یه لاگین pyrubi دیگه در حال انجامه. اگه گیر کرده: /pyrubi_reset")
                        return
                    parts = text.split()
                    if len(parts) < 2:
                        await message.reply("فرمت: /pyrubi_login 989123456789")
                        return
                    phone = parts[1]
                    code_queue = queue.Queue()
                    result_queue = queue.Queue()
                    _pyrubi_login_state["code_queue"] = code_queue
                    _pyrubi_login_state["in_progress"] = True
                    t = threading.Thread(
                        target=_pyrubi_login_worker, args=(phone, code_queue, result_queue), daemon=True
                    )
                    t.start()
                    await message.reply("📲 منتظر کد تایید pyrubi هستم. وقتی رسید بفرست:\n/pyrubi_code 12345")
                    _spawn(_watch_pyrubi_login_for_bot(bot, chat_id, result_queue))
                    return

                if text.startswith("/pyrubi_code"):
                    parts = text.split()
                    if len(parts) < 2:
                        await message.reply("فرمت: /pyrubi_code 12345")
                        return
                    q = _pyrubi_login_state.get("code_queue")
                    if not q:
                        await message.reply("❌ اول /pyrubi_login رو بزن.")
                        return
                    q.put(parts[1])
                    await message.reply("⏳ در حال بررسی کد pyrubi...")
                    return

                if text == "/pyrubi_reset":
                    _pyrubi_login_state.clear()
                    removed = []
                    for fname in sorted(set(glob.glob(PYRUBI_SESSION_NAME + "*"))):
                        if not os.path.isfile(fname):
                            continue
                        try:
                            os.remove(fname)
                            removed.append(fname)
                        except Exception:
                            pass
                    if removed:
                        await message.reply("✅ ریست شد. فایل‌های حذف‌شده: " + ", ".join(removed) + "\nحالا دوباره /pyrubi_login بزن.")
                    else:
                        await message.reply("✅ ریست شد (فایل session‌ای پیدا نشد).\nحالا دوباره /pyrubi_login بزن.")
                    return

                if text.startswith("/setstart "):
                    settings["start_message"] = text[len("/setstart "):]
                    save_settings(settings)
                    sync_to_all_bots({"start_message": settings["start_message"]})
                    await message.reply("متن استارت آپدیت شد (روی همه ربات‌ها هم اعمال شد).")
                    return

                if text.startswith("/setstartimage"):
                    new_url = text[len("/setstartimage"):].strip()
                    if new_url:
                        settings["start_image_url"] = new_url
                        settings["start_image_path"] = ""
                        save_settings(settings)
                        sync_to_all_bots({"start_image_url": new_url, "start_image_path": ""})
                        await message.reply("عکس پیام start (از لینک) تنظیم شد و روی همه ربات‌ها هم اعمال شد. برای تست، /start رو بزن.")
                    else:
                        awaiting_start_image.add(chat_id)
                        await message.reply("باشه، حالا همون عکسی که می‌خوای رو مستقیم برام بفرست 📷")
                    return

                if text == "/removestartimage":
                    settings["start_image_url"] = ""
                    settings["start_image_path"] = ""
                    save_settings(settings)
                    sync_to_all_bots({"start_image_url": "", "start_image_path": ""})
                    awaiting_start_image.discard(chat_id)
                    await message.reply("عکس پیام start حذف شد (روی همه ربات‌ها هم اعمال شد)، از این به بعد فقط متن ارسال می‌شه.")
                    return

                if text.startswith("/setreply "):
                    settings["screenshot_reply"] = text[len("/setreply "):]
                    save_settings(settings)
                    sync_to_all_bots({"screenshot_reply": settings["screenshot_reply"]})
                    await message.reply("متن پاسخ اسکرین‌شات آپدیت شد (روی همه ربات‌ها هم اعمال شد).")
                    return

                if text.startswith("/setreviewadmin "):
                    new_admin = text[len("/setreviewadmin "):].strip()
                    settings["review_admin_chat_id"] = new_admin
                    save_settings(settings)
                    sync_to_all_bots({"review_admin_chat_id": new_admin})
                    await message.reply("ادمین بررسی‌کننده روی همه ربات‌ها تنظیم شد به: " + new_admin)
                    return

                if text.startswith("/approve "):
                    target = text[len("/approve "):].strip()
                    users = settings.setdefault("users", {})
                    users.setdefault(target, {"status": "pending", "screenshots": 0})
                    users[target]["status"] = "approved"
                    settings["stats"]["approved"] = settings["stats"].get("approved", 0) + 1
                    save_settings(settings)
                    try:
                        await bot.send_message(chat_id=target, text=settings["approved_message"])
                    except Exception as e:
                        print("[" + bot_label + "] khata dar ersal approve be user: " + str(e))
                    await message.reply("Karbar " + target + " tayid shod.")
                    return

                if text.startswith("/reject "):
                    target = text[len("/reject "):].strip()
                    users = settings.setdefault("users", {})
                    users.setdefault(target, {"status": "pending", "screenshots": 0})
                    users[target]["status"] = "rejected"
                    settings["stats"]["rejected"] = settings["stats"].get("rejected", 0) + 1
                    save_settings(settings)
                    try:
                        await bot.send_message(chat_id=target, text=settings["rejected_message"])
                    except Exception as e:
                        print("[" + bot_label + "] khata dar ersal reject be user: " + str(e))
                    await message.reply("Karbar " + target + " rad shod.")
                    return

                if text.startswith("/broadcast "):
                    broadcast_text = text[len("/broadcast "):]
                    users = settings.get("users", {})
                    success = 0
                    failed = 0
                    for user_chat_id in list(users.keys()):
                        try:
                            await bot.send_message(chat_id=user_chat_id, text=broadcast_text)
                            success += 1
                        except Exception:
                            failed += 1

                    other_ok, other_bad = await broadcast_to_all_bots(broadcast_text)
                    reply_text = (
                        "پیام همگانی روی این ربات ارسال شد.\n"
                        "موفق: " + str(success) + "\n"
                        "ناموفق: " + str(failed)
                    )
                    if bot_label == "bot1":
                        reply_text += (
                            "\n\nروی بقیه ربات‌ها هم ارسال شد:\n"
                            "موفق: " + str(other_ok) + "\n"
                            "ناموفق: " + str(other_bad)
                        )
                    await message.reply(reply_text)
                    return

                if text == "/stats":
                    users = settings.get("users", {})
                    stats = settings.get("stats", {})
                    total_users = len(users)
                    pending = sum(1 for u in users.values() if u.get("status") == "pending")
                    passed_filter = count_filter_passed(users)
                    await message.reply(
                        "📊 آمار ربات:\n"
                        "کل کاربران: " + str(total_users) + "\n"
                        "در انتظار: " + str(pending) + "\n"
                        "تایید شده: " + str(stats.get("approved", 0)) + "\n"
                        "رد شده: " + str(stats.get("rejected", 0)) + "\n"
                        "کل اسکرین‌شات‌ها: " + str(stats.get("total_screenshots", 0)) + "\n"
                        "ثبت‌نام‌کرده‌ها (پروفایل+تاریخچه تایید شده): " + str(passed_filter)
                    )
                    return

                if text.startswith("/setbotname "):
                    new_name = text[len("/setbotname "):].strip()
                    settings["bot_display_name"] = new_name
                    save_settings(settings)
                    await message.reply("اسم نمایشی این ربات تنظیم شد به: " + new_name)
                    return

                if text.startswith("/setautodelete "):
                    raw_value = text[len("/setautodelete "):].strip()
                    if not raw_value.isdigit():
                        await message.reply("لطفا یه عدد صحیح (بر حسب ثانیه) بفرست. مثال: /setautodelete 60")
                        return
                    seconds = int(raw_value)
                    settings["start_delete_seconds"] = seconds
                    save_settings(settings)
                    sync_to_all_bots({"start_delete_seconds": seconds})
                    await message.reply(
                        "زمان حذف خودکار پیام استارت روی " + str(seconds) + " ثانیه تنظیم شد (روی همه ربات‌ها هم اعمال شد)."
                    )
                    return

                if text.startswith("/setstartcooldown "):
                    raw_value = text[len("/setstartcooldown "):].strip()
                    if not raw_value.isdigit():
                        await message.reply("لطفا یه عدد صحیح (بر حسب ثانیه) بفرست. مثال: /setstartcooldown 10")
                        return
                    seconds = int(raw_value)
                    settings["start_cooldown_seconds"] = seconds
                    save_settings(settings)
                    sync_to_all_bots({"start_cooldown_seconds": seconds})
                    await message.reply(
                        "ضد اسپم /start روی " + str(seconds) + " ثانیه تنظیم شد (روی همه ربات‌ها هم اعمال شد)."
                    )
                    return

                if text == "/filterstats":
                    if bot_label == "bot1":
                        with _registry_lock:
                            all_entries = list(_ALL_BOTS_REGISTRY)
                        lines = []
                        for entry in all_entries:
                            their_users = entry["settings"].get("users", {})
                            count = count_filter_passed(their_users)
                            display_name = entry["settings"].get("bot_display_name") or entry["label"]
                            lines.append("توی ربات " + display_name + ": " + str(count) + " نفر ثبت‌نام کردن")
                        await message.reply("📊 آمار فیلتر همه ربات‌ها:\n\n" + "\n".join(lines))
                    else:
                        users = settings.get("users", {})
                        count = count_filter_passed(users)
                        display_name = settings.get("bot_display_name") or bot_label
                        await message.reply("توی ربات " + display_name + ": " + str(count) + " نفر ثبت‌نام کردن")
                    return

                if text.startswith("/addadmin "):
                    new_admin = text[len("/addadmin "):].strip()
                    admins = settings.setdefault("admins", [])
                    if new_admin not in admins:
                        admins.append(new_admin)
                        save_settings(settings)
                        sync_to_all_bots({"admins": list(admins)})
                        await message.reply("ادمین جدید اضافه شد (روی همه ربات‌ها هم اعمال شد): " + new_admin)
                    else:
                        await message.reply("این chat_id از قبل ادمین بود.")
                    return

                if text.startswith("/removeadmin "):
                    target = text[len("/removeadmin "):].strip()
                    admins = settings.setdefault("admins", [])
                    if target in admins:
                        admins.remove(target)
                        save_settings(settings)
                        sync_to_all_bots({"admins": list(admins)})
                        await message.reply("ادمین حذف شد (روی همه ربات‌ها هم اعمال شد): " + target)
                    else:
                        await message.reply("چنین ادمینی پیدا نشد.")
                    return

                if text == "/gettexts":
                    await message.reply(
                        "Start:\n" + settings["start_message"] +
                        "\n\nScreenshot reply:\n" + settings["screenshot_reply"] +
                        "\n\nReview admin chat_id:\n" + str(settings.get("review_admin_chat_id")) +
                        "\n\nAdmins:\n" + str(settings.get("admins"))
                    )
                    return

            # ------------------------------------------------
            # aks jadid baraye payam start (admin dar hale ersal aks)
            # ------------------------------------------------
            if chat_id in awaiting_start_image and getattr(message, "file", None):
                file_id = get_file_id(message.file)
                local_path = "start_image_" + bot_label + ".jpg"
                try:
                    await bot.download(file_id, local_path)
                    settings["start_image_path"] = local_path
                    settings["start_image_url"] = ""
                    save_settings(settings)
                    sync_start_image_file_to_all_bots(local_path)
                    awaiting_start_image.discard(chat_id)
                    reply_msg = "عکس ذخیره شد ✅ برای تست، /start رو بزن."
                    if bot_label == "bot1":
                        reply_msg += "\n(روی همه ربات‌ها هم اعمال شد)"
                    await message.reply(reply_msg)
                except Exception as e:
                    print("[" + bot_label + "] khata dar zakhire start_image: " + str(e))
                    await message.reply("مشکلی پیش اومد، دوباره امتحان کن.")
                return

            # ------------------------------------------------
            # screenshot az karbare adi
            # ------------------------------------------------
            if getattr(message, "file", None):
                register_user(chat_id)
                users = settings.setdefault("users", {})
                users.setdefault(chat_id, {"status": "pending", "screenshots": 0, "profile_ss": False, "history_ss": False})
                users[chat_id].setdefault("profile_ss", False)
                users[chat_id].setdefault("history_ss", False)
                users[chat_id]["screenshots"] += 1
                settings["stats"]["total_screenshots"] = settings["stats"].get("total_screenshots", 0) + 1

                file_id = get_file_id(message.file)
                local_path = await download_screenshot(bot, file_id)

                ocr_text = ""
                detection = {"page": "unknown"}
                if local_path:
                    ocr_text = ocr_extract_text(local_path)
                    detection = detect_screenshot_type(ocr_text)
                    print("[" + bot_label + "] OCR natije: " + str(detection) + " | matn: " + ocr_text[:200].replace("\n", " "))

                page = detection.get("page")
                problem_msg = None  # age screenshot moshkel dasht, in por mishe

                # chek kardan inke tarakonesh ghabl az akharin /start karbar nabude
                if page in ("history", "both"):
                    tx_time = extract_transaction_time(ocr_text)
                    start_time = users[chat_id].get("start_time")
                    if start_time:
                        if tx_time is None:
                            await message.reply(
                                "نتونستم تاریخ و ساعت تراکنش رو توی این اسکرین‌شات به درستی تشخیص بدم. "
                                "لطفا یه اسکرین‌شات واضح‌تر بفرست که تاریخ و ساعت تراکنش کامل و خوانا توش دیده بشه."
                            )
                            return
                        # tarikh sabt nam bayad "ba'd" az tarikh /start bashe, na mosavi va na ghabl
                        if tx_time <= start_time:
                            await message.reply(settings["old_registration_message"])
                            return

                if page == "profile":
                    if detection.get("ok"):
                        users[chat_id]["profile_ss"] = True
                    else:
                        problem_msg = "توی این اسکرین‌شات پروفایل، هنوز احراز هویتت تایید نشده. اول احراز هویتت رو تکمیل کن بعد دوباره اسکرین‌شات بفرست."
                elif page == "history":
                    if detection.get("ok"):
                        users[chat_id]["history_ss"] = True
                    else:
                        problem_msg = "توی این اسکرین‌شات تاریخچه، تراکنش «هدیه بابت تکمیل پروفایل با کد معرف» پیدا نشد. مطمئن شو این تراکنش انجام شده و توی تاریخچه هست."
                elif page == "both":
                    if detection.get("profile_ok"):
                        users[chat_id]["profile_ss"] = True
                    if detection.get("history_ok"):
                        users[chat_id]["history_ss"] = True
                    if not detection.get("profile_ok") and not detection.get("history_ok"):
                        problem_msg = "توی این اسکرین‌شات نه احراز هویت تایید شده رو دیدم نه تراکنش هدیه‌ی تکمیل پروفایل رو. لطفا مطمئن شو هر دو انجام شدن."
                    elif not detection.get("profile_ok"):
                        problem_msg = "احراز هویتت هنوز توی این اسکرین‌شات تایید نشده."
                    elif not detection.get("history_ok"):
                        problem_msg = "تراکنش «هدیه بابت تکمیل پروفایل با کد معرف» توی تاریخچه‌ت پیدا نشد."
                else:
                    problem_msg = "این اسکرین‌شات از صفحه پروفایل یا تاریخچه نیست. لطفا دقیقا از صفحه «پروفایل» یا از صفحه «تاریخچه» اسکرین‌شات بگیر و بفرست."

                save_settings(settings)

                has_profile = users[chat_id]["profile_ss"]
                has_history = users[chat_id]["history_ss"]

                if has_profile and has_history:
                    save_settings(settings)
                    await message.reply("هر دو اسکرین‌شات دریافت شد و درخواستت در حال بررسیه. نتیجه به‌زودی بهت اطلاع داده می‌شه. ⏳")
                elif problem_msg:
                    await message.reply(problem_msg)
                else:
                    # in screenshot khodesh dorost bood (profile ya history tayid shod) vali oni digash hanooz nayomade
                    just_confirmed = "تاریخچه" if page in ("history", "both") else "پروفایل"
                    missing = "پروفایل" if has_history and not has_profile else "تاریخچه"
                    await message.reply(
                        "این اسکرین‌شات " + just_confirmed + " تایید شد. حالا لطفا از " + missing + " هم برام اسکرین‌شات بفرست."
                    )

                review_admin = settings.get("review_admin_chat_id")
                if review_admin and chat_id != review_admin:
                    await forward_screenshot_to_reviewer(bot, message, file_id, ocr_text, str(detection))
                return

            # sayer payam haye karbar adi (sabt kardan be onvane karbar)
            if not is_admin(chat_id):
                register_user(chat_id)

        print("[" + bot_label + "] ejra shod")
        bot.run()

    return run


# ===========================================================
# تعریف ربات‌ها
# ===========================================================
#
# هر ربات فقط با متغیرهای محیطی (Environment Variables) که در
# Railway تنظیم می‌کنی ساخته می‌شه. برای اضافه کردن ربات جدید:
# 1) توی Railway دو متغیر جدید بساز (مثلاً BOT3_TOKEN و ADMIN3_CHAT_ID)
# 2) یک run_botN جدید مثل نمونه‌های زیر بساز
# 3) اون run_botN رو به لیست BOT_FUNCTIONS اضافه کن
# دیگه نیازی به هیچ ربات مادر یا دستور /addbot نیست.

run_bot1 = make_verification_bot(
    bot_label="bot1",
    token_env="RUBIKA_BOT_TOKEN",
    super_admin_env="ADMIN_CHAT_ID",
    settings_filename="settings_bot1.json",
)

run_bot2 = make_verification_bot(
    bot_label="bot2",
    token_env="BOT2_TOKEN",
    super_admin_env="ADMIN2_CHAT_ID",
    settings_filename="settings_bot2.json",
)

run_bot3 = make_verification_bot(
    bot_label="bot3",
    token_env="BOT3_TOKEN",
    super_admin_env="ADMIN3_CHAT_ID",
    settings_filename="settings_bot3.json",
)

run_bot4 = make_verification_bot(
    bot_label="bot4",
    token_env="BOT4_TOKEN",
    super_admin_env="ADMIN4_CHAT_ID",
    settings_filename="settings_bot4.json",
)

run_bot5 = make_verification_bot(
    bot_label="bot5",
    token_env="BOT5_TOKEN",
    super_admin_env="ADMIN5_CHAT_ID",
    settings_filename="settings_bot5.json",
)

BOT_FUNCTIONS = [
    run_bot1,
    run_bot2,
    run_bot3,
    run_bot4,
    run_bot5,
]


def main():
    _restore_pyrubi_session()
    threads = []
    for bot_func in BOT_FUNCTIONS:
        t = threading.Thread(target=bot_func, daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
