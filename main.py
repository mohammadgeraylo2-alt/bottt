import os
import json
import threading
import pytesseract
from PIL import Image
from rubka import Robot
from rubka.context import Message


def make_verification_bot(bot_label, token_env, super_admin_env, settings_filename):
    """
    ربات احراز هویت با پنل ادمین کامل:
    - /start -> پیام ثبت‌نام
    - کاربر اسکرین‌شات می‌ده -> پیام "در حال بررسی" + فوروارد برای ادمین بررسی‌کننده
    - تایید/رد کاربر، پیام همگانی، آمار، چند ادمین همزمان
    """

    def run():
        token = os.environ.get(token_env)
        if not token:
            print("[" + bot_label + "] " + token_env + " tanzim nashode, in bot run nemishe.")
            return

        seed_admin = os.environ.get(super_admin_env, "").strip()

        default_settings = {
            "start_message": (
                "سلام\n\n"
                "برای استفاده از ربات، لطفا ابتدا توی سایت زیر ثبت نام کن:\n"
                "https://example.com\n\n"
                "بعد از ثبت نام، یه اسکرین شات از صفحه ثبت نامت همینجا برام بفرست تا بررسی کنم."
            ),
            "screenshot_reply": "اسکرین شاتت دریافت شد، در حال بررسی هست. نتیجه رو بهت اطلاع می دم.",
            "approved_message": "تبریک! ثبت نامت تایید شد.",
            "rejected_message": "متاسفانه ثبت نامت تایید نشد. لطفا دوباره تلاش کن.",
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
                text = pytesseract.image_to_string(img, lang="fas")
                return text
            except Exception as e:
                print("[" + bot_label + "] khata dar OCR: " + str(e))
                return ""

        def detect_screenshot_type(ocr_text):
            # in kalamat ro donbal migardim (mamkene OCR kamel dorost nabashe pas chandta variation check mikonim)
            profile_keywords = ["احراز هویت شده", "احراز هویت"]
            history_keywords = ["هدیه میلی", "کد معرف", "تکمیل پروفایل"]

            is_profile = any(k in ocr_text for k in profile_keywords)
            is_history = any(k in ocr_text for k in history_keywords)

            if is_profile and not is_history:
                return "profile"
            if is_history and not is_profile:
                return "history"
            if is_profile and is_history:
                return "both"
            return "unknown"

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
                    await message.reply(
                        "Dastorat admin:\n"
                        "/setstart <matn> - avaz kardan payam start\n"
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
                    return

                if text.startswith("/setstart "):
                    settings["start_message"] = text[len("/setstart "):]
                    save_settings(settings)
                    await message.reply("Matn start update shod.")
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
                ss_type = "unknown"
                if local_path:
                    ocr_text = ocr_extract_text(local_path)
                    ss_type = detect_screenshot_type(ocr_text)
                    print("[" + bot_label + "] OCR natije: " + ss_type + " | matn: " + ocr_text[:200].replace("\n", " "))

                if ss_type in ("profile", "both"):
                    users[chat_id]["profile_ss"] = True
                if ss_type in ("history", "both"):
                    users[chat_id]["history_ss"] = True

                save_settings(settings)

                has_profile = users[chat_id]["profile_ss"]
                has_history = users[chat_id]["history_ss"]

                if has_profile and has_history:
                    users[chat_id]["status"] = "approved"
                    settings["stats"]["approved"] = settings["stats"].get("approved", 0) + 1
                    save_settings(settings)
                    await message.reply(settings["approved_message"])
                elif ss_type == "unknown":
                    await message.reply(
                        settings["screenshot_reply"] +
                        "\n\n(In screenshot tashkhis dade nashod، lotfan screenshot vazeh az profile ya tarikhcheh befrest.)"
                    )
                else:
                    missing = "tarikhcheh" if has_profile else "profile"
                    await message.reply(
                        settings["screenshot_reply"] +
                        "\n\nyeki digash mundeh: screenshot az " + missing + " ham befrest."
                    )

                review_admin = settings.get("review_admin_chat_id")
                if review_admin and chat_id != review_admin:
                    await forward_screenshot_to_reviewer(bot, message, file_id, ocr_text, ss_type)
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

# baraye ezafe kardan robat jadid:
# run_bot3 = make_verification_bot(
#     bot_label="bot3",
#     token_env="BOT3_TOKEN",
#     super_admin_env="ADMIN3_CHAT_ID",
#     settings_filename="settings_bot3.json",
# )

BOT_FUNCTIONS = [
    run_bot1,
    run_bot2,
    # run_bot3,
]


def main():
    threads = []
    for bot_func in BOT_FUNCTIONS:
        t = threading.Thread(target=bot_func, daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
