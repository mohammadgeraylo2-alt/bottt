import os
import json
import re
import time
import threading
import multiprocessing as mp
import jdatetime
from zoneinfo import ZoneInfo
import pytesseract
from PIL import Image, ImageOps
from rubka import Robot
from rubka.context import Message


def make_verification_bot(
    bot_label,
    settings_filename,
    token_env=None,
    super_admin_env=None,
    direct_token=None,
    direct_admin=None,
    manager_api=None,
):
    """
    ربات احراز هویت با پنل ادمین کامل:
    - /start -> پیام ثبت‌نام
    - کاربر اسکرین‌شات می‌ده -> پیام "در حال بررسی" + فوروارد برای ادمین بررسی‌کننده
    - تایید/رد کاربر، پیام همگانی، آمار، چند ادمین همزمان

    token / admin ro mishe be 2 tarigh moshakhas kard:
    - token_env / super_admin_env: az environment variable khoonde mishe (baraye robat haye
      az ghabl tarif shode dar code, masalan bot1, bot2)
    - direct_token / direct_admin: mostaghiman pass mishe (baraye robat hayi ke dar zaman
      ejra az tarigh /addbot ezafe mishan, chon in ha environment variable nadaran)

    manager_api (faghat baraye "robat asli/madar") shamele in function hast:
    add_bot, list_bots, stop_bot, restart_bot, delete_bot -> in ha be admin ejaze midan
    robat haye jadid ro az tarigh khode robat modiriyat konan.
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
            "users": {},
            "stats": {"total_screenshots": 0, "approved": 0, "rejected": 0},
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

        awaiting_start_image = set()  # chat_id haye admin ke montazere ersale aks hastan

        def is_admin(chat_id):
            return chat_id in settings.get("admins", [])

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


        @bot.on_message(commands=["start"])
        async def start(bot: Robot, message: Message):
            try:
                register_user(message.chat_id)
                users = settings.setdefault("users", {})
                users[message.chat_id]["start_time"] = time.time()
                save_settings(settings)
                image_source = (settings.get("start_image_path") or "").strip() or (settings.get("start_image_url") or "").strip()
                if image_source:
                    try:
                        await message.reply_image(path=image_source, text=settings["start_message"])
                    except Exception as e:
                        print("[" + bot_label + "] khata dar ersal start_image, fallback be matn saade: " + str(e))
                        await message.reply(settings["start_message"])
                else:
                    await message.reply(settings["start_message"])
            except Exception as e:
                print("[" + bot_label + "] khata dar start: " + str(e))

        @bot.on_message()
        async def handle_message(bot: Robot, message: Message):
            try:
                await _handle_message_inner(bot, message)
            except Exception as e:
                print("[" + bot_label + "] khata dar handle_message: " + str(e))

        async def _handle_message_inner(bot: Robot, message: Message):
            text = (message.text or "").strip()
            chat_id = message.chat_id

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
                        "Dastorat admin:\n"
                        "/setstart <matn> - avaz kardan payam start\n"
                        "/setstartimage <url> - tanzim aks az link (ya bedoone link befrest ta aks ro mostaghim darkhast kone)\n"
                        "/removestartimage - hazf kardane aks az payame start\n"
                        "/setreply <matn> - avaz kardan payam dar hale barresi\n"
                        "/setreviewadmin <chat_id> - taein konande daryaft screenshot ha\n"
                        "/approve <chat_id> - tayid karbar\n"
                        "/reject <chat_id> - rad karbar\n"
                        "/broadcast <matn> - ersal payam be hame karbara\n"
                        "/stats - amar\n"
                        "/addadmin <chat_id> - ezafe kardan admin jadid\n"
                        "/removeadmin <chat_id> - hazf admin\n"
                        "/gettexts - didan tanzimat felli\n"
                        "/myid - didan chat_id khodet"
                    )
                    if manager_api:
                        help_text += (
                            "\n\nDastorat modiriyat robat ha (faghat robat asli):\n"
                            "/addbot <token> <admin_chat_id> - sakhte robat jadid\n"
                            "/bots - didan liste robat ha\n"
                            "/stopbot <bot_id> - motevaghef kardan robat\n"
                            "/restartbot <bot_id> - restart kardan robat\n"
                            "/deletebot <bot_id> - hazf kamele robat"
                        )
                    await message.reply(help_text)
                    return

                if text.startswith("/setstart "):
                    settings["start_message"] = text[len("/setstart "):]
                    save_settings(settings)
                    await message.reply("Matn start update shod.")
                    return

                if text.startswith("/setstartimage"):
                    new_url = text[len("/setstartimage"):].strip()
                    if new_url:
                        settings["start_image_url"] = new_url
                        settings["start_image_path"] = ""
                        save_settings(settings)
                        await message.reply("عکس پیام start (از لینک) تنظیم شد. برای تست، /start رو بزن.")
                    else:
                        awaiting_start_image.add(chat_id)
                        await message.reply("باشه، حالا همون عکسی که می‌خوای رو مستقیم برام بفرست 📷")
                    return

                if text == "/removestartimage":
                    settings["start_image_url"] = ""
                    settings["start_image_path"] = ""
                    save_settings(settings)
                    awaiting_start_image.discard(chat_id)
                    await message.reply("عکس پیام start حذف شد، از این به بعد فقط متن ارسال می‌شه.")
                    return

                if text.startswith("/setreply "):
                    settings["screenshot_reply"] = text[len("/setreply "):]
                    save_settings(settings)
                    await message.reply("Matn pasokh screenshot update shod.")
                    return

                if text.startswith("/setreviewadmin "):
                    new_admin = text[len("/setreviewadmin "):].strip()
                    settings["review_admin_chat_id"] = new_admin
                    save_settings(settings)
                    await message.reply("Review admin update shod be: " + new_admin)
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
                    await message.reply(
                        "Broadcast tamoom shod.\nMovafagh: " + str(success) + "\nNamovafagh: " + str(failed)
                    )
                    return

                if text == "/stats":
                    users = settings.get("users", {})
                    stats = settings.get("stats", {})
                    total_users = len(users)
                    pending = sum(1 for u in users.values() if u.get("status") == "pending")
                    await message.reply(
                        "Amar rabat:\n"
                        "Kol karbara: " + str(total_users) + "\n"
                        "Dar entezar: " + str(pending) + "\n"
                        "Tayid shode: " + str(stats.get("approved", 0)) + "\n"
                        "Rad shode: " + str(stats.get("rejected", 0)) + "\n"
                        "Kol screenshot ha: " + str(stats.get("total_screenshots", 0))
                    )
                    return

                if text.startswith("/addadmin "):
                    new_admin = text[len("/addadmin "):].strip()
                    admins = settings.setdefault("admins", [])
                    if new_admin not in admins:
                        admins.append(new_admin)
                        save_settings(settings)
                        await message.reply("Admin jadid ezafe shod: " + new_admin)
                    else:
                        await message.reply("In chat_id az ghabl admin bood.")
                    return

                if text.startswith("/removeadmin "):
                    target = text[len("/removeadmin "):].strip()
                    admins = settings.setdefault("admins", [])
                    if target in admins:
                        admins.remove(target)
                        save_settings(settings)
                        await message.reply("Admin hazf shod: " + target)
                    else:
                        await message.reply("Chenin admini peida nashod.")
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
                # dastorat modiriyat robat ha - faghat robat asli in
                # ha ro dare (manager_api pass shode be in instance)
                # ------------------------------------------------
                if manager_api:
                    if text.startswith("/addbot"):
                        parts = text[len("/addbot"):].strip().split()
                        if len(parts) < 2:
                            await message.reply(
                                "Formate dorost:\n/addbot <token> <admin_chat_id>"
                            )
                            return
                        new_token, new_admin = parts[0], parts[1]
                        bot_id, err = manager_api["add_bot"](new_token, new_admin)
                        if err:
                            await message.reply("Khata: " + err)
                        else:
                            await message.reply(
                                "Robat jadid sakhte shod va ejra shod.\n"
                                "Shenase robat: " + bot_id + "\n"
                                "Baraye control: /stopbot " + bot_id + " ya /deletebot " + bot_id
                            )
                        return

                    if text == "/bots":
                        lines = manager_api["list_bots"]()
                        if not lines:
                            await message.reply("Ta halan hich robati az tarighe /addbot ezafe nashode.")
                        else:
                            await message.reply("Liste robat ha:\n" + "\n".join(lines))
                        return

                    if text.startswith("/stopbot "):
                        target_id = text[len("/stopbot "):].strip()
                        ok, err = manager_api["stop_bot"](target_id)
                        await message.reply(
                            ("Robat " + target_id + " motevaghef shod.") if ok else ("Khata: " + str(err))
                        )
                        return

                    if text.startswith("/restartbot "):
                        target_id = text[len("/restartbot "):].strip()
                        ok, err = manager_api["restart_bot"](target_id)
                        await message.reply(
                            ("Robat " + target_id + " restart shod.") if ok else ("Khata: " + str(err))
                        )
                        return

                    if text.startswith("/deletebot "):
                        target_id = text[len("/deletebot "):].strip()
                        ok, err = manager_api["delete_bot"](target_id)
                        await message.reply(
                            ("Robat " + target_id + " be tamami hazf shod.") if ok else ("Khata: " + str(err))
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
                    awaiting_start_image.discard(chat_id)
                    await message.reply("عکس ذخیره شد ✅ برای تست، /start رو بزن.")
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
# ===========================================================
# sistame modiriyat robat ha (bots.json)
#
# - har robate jadid ke az tarighe /addbot ezafe mishe, dar yek
#   process jodagane (multiprocessing) ejra mishe. Ba in kar
#   mitoonim har robat ro mostaghel motevaghef/restart/hazf konim
#   bedoone inke robat haye dige ta'sir bebinan.
# - in bakhsh faghat dar process asli (jayi ke main() run mishe)
#   zende hast, pas faghat robate "madar" (bot1) manager_api ro
#   migire va dastorat /addbot va... ro dare.
# ===========================================================

BOTS_FILE = "bots.json"

_manager_lock = threading.Lock()
_bot_processes = {}  # bot_id -> multiprocessing.Process


def load_bots():
    if os.path.exists(BOTS_FILE):
        try:
            with open(BOTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("khata dar khoondane " + BOTS_FILE + ": " + str(e))
            return []
    return []


def save_bots(bots_list):
    with open(BOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(bots_list, f, ensure_ascii=False, indent=2)


def _find_bot_entry(bots_list, bot_id):
    for b in bots_list:
        if b.get("id") == bot_id:
            return b
    return None


def _gen_bot_id(bots_list):
    n = 2  # bot1 ghablan gerefte shode (robate asli)
    while True:
        candidate = "bot" + str(n)
        if not _find_bot_entry(bots_list, candidate):
            return candidate
        n += 1


def _start_bot_process(entry):
    run_func = make_verification_bot(
        bot_label=entry["id"],
        settings_filename=entry["settings_file"],
        direct_token=entry["token"],
        direct_admin=entry["admin_chat_id"],
    )
    p = mp.Process(target=run_func, daemon=True, name=entry["id"])
    p.start()
    _bot_processes[entry["id"]] = p
    return p


def _stop_bot_process(bot_id):
    proc = _bot_processes.get(bot_id)
    if proc and proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
    _bot_processes.pop(bot_id, None)


def manager_add_bot(token, admin_chat_id):
    token = (token or "").strip()
    admin_chat_id = (admin_chat_id or "").strip()
    if not token or not admin_chat_id:
        return None, "token va admin_chat_id nemitoonan khali bashan."
    with _manager_lock:
        bots_list = load_bots()
        if any(b.get("token") == token for b in bots_list):
            return None, "in token ghablan baraye yek robate dige sabt shode."
        bot_id = _gen_bot_id(bots_list)
        entry = {
            "id": bot_id,
            "token": token,
            "admin_chat_id": admin_chat_id,
            "settings_file": "settings_" + bot_id + ".json",
            "status": "running",
        }
        bots_list.append(entry)
        save_bots(bots_list)
        try:
            _start_bot_process(entry)
        except Exception as e:
            entry["status"] = "stopped"
            save_bots(bots_list)
            return None, "robat sakhte shod vali ejra nashod: " + str(e)
        return bot_id, None


def manager_list_bots():
    bots_list = load_bots()
    lines = []
    for b in bots_list:
        proc = _bot_processes.get(b["id"])
        alive = bool(proc and proc.is_alive())
        real_status = "running" if alive else "stopped"
        lines.append(
            b["id"] + " | admin: " + str(b.get("admin_chat_id")) + " | status: " + real_status
        )
    return lines


def manager_stop_bot(bot_id):
    with _manager_lock:
        bots_list = load_bots()
        entry = _find_bot_entry(bots_list, bot_id)
        if not entry:
            return False, "robati ba in shenase peida nashod."
        _stop_bot_process(bot_id)
        entry["status"] = "stopped"
        save_bots(bots_list)
        return True, None


def manager_restart_bot(bot_id):
    with _manager_lock:
        bots_list = load_bots()
        entry = _find_bot_entry(bots_list, bot_id)
        if not entry:
            return False, "robati ba in shenase peida nashod."
        _stop_bot_process(bot_id)
        try:
            _start_bot_process(entry)
        except Exception as e:
            entry["status"] = "stopped"
            save_bots(bots_list)
            return False, "restart nashod: " + str(e)
        entry["status"] = "running"
        save_bots(bots_list)
        return True, None


def manager_delete_bot(bot_id):
    with _manager_lock:
        bots_list = load_bots()
        entry = _find_bot_entry(bots_list, bot_id)
        if not entry:
            return False, "robati ba in shenase peida nashod."
        _stop_bot_process(bot_id)
        bots_list = [b for b in bots_list if b.get("id") != bot_id]
        save_bots(bots_list)
        try:
            if os.path.exists(entry["settings_file"]):
                os.remove(entry["settings_file"])
        except Exception as e:
            print("khata dar hazfe faile settings: " + str(e))
        return True, None


def start_saved_bots():
    """Dar shorou'e barnameh, har robati ke ghablan az tarighe /addbot sabt
    shode va vaz'iyatesh 'running' bode ro dobare ejra mikone."""
    bots_list = load_bots()
    for entry in bots_list:
        if entry.get("status") == "running":
            try:
                _start_bot_process(entry)
            except Exception as e:
                print("khata dar ejra kardane " + str(entry.get("id")) + ": " + str(e))


MANAGER_API = {
    "add_bot": manager_add_bot,
    "list_bots": manager_list_bots,
    "stop_bot": manager_stop_bot,
    "restart_bot": manager_restart_bot,
    "delete_bot": manager_delete_bot,
}


# ===========================================================
# تعریف ربات‌ها
# ===========================================================

# bot1 = robate asli/madar. Faghat in robat manager_api ro dare, pas
# faghat az daroone hamin robat mishe /addbot, /bots, /stopbot,
# /restartbot va /deletebot ro seda zad.
run_bot1 = make_verification_bot(
    bot_label="bot1",
    token_env="RUBIKA_BOT_TOKEN",
    super_admin_env="ADMIN_CHAT_ID",
    settings_filename="settings_bot1.json",
    manager_api=MANAGER_API,
)

# age hanooz robate dovom ro az tarighe environment variable mikhay
# tarif koni ham hamchenan kar mikone (ekhtiyari), vali digar niazi
# be in nist chon mishe az tarighe /addbot dar bot1 ezafash kard.
run_bot2 = make_verification_bot(
    bot_label="bot2",
    token_env="BOT2_TOKEN",
    super_admin_env="ADMIN2_CHAT_ID",
    settings_filename="settings_bot2.json",
)

BOT_FUNCTIONS = [
    run_bot1,
    run_bot2,
]


def main():
    threads = []
    for bot_func in BOT_FUNCTIONS:
        t = threading.Thread(target=bot_func, daemon=True)
        t.start()
        threads.append(t)

    # robat hayi ke ghablan az tarighe /addbot sakhte shodan ro
    # dobare ejra kon (baraye zaman badaz restart shodane server)
    start_saved_bots()

    for t in threads:
        t.join()

    # age (be har dalili) tamame thread haye static tamoom shodan,
    # baz ham process asli ro zende negah dar chon process haye
    # robat haye dynamic (mp.Process) be in process vabaste hastan.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    # ruye Linux 'fork' pishfarze, vali sarih tayin mikonim chon
    # closure haye make_verification_bot faghat ba 'fork' bedoone
    # pickle kardan kar mikonan (nemitoonan ba 'spawn' serialize shan).
    try:
        mp.set_start_method("fork")
    except (RuntimeError, ValueError):
        pass
    main()
