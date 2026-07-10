import os
import json
import threading
from rubka import Robot
from rubka.context import Message

# ===========================================================
# ربات ۱: ربات احراز هویت (پروژه C)
# توکن: RUBIKA_BOT_TOKEN | ادمین: ADMIN_CHAT_ID
# ===========================================================


def run_verify_bot():
    token = os.environ.get("RUBIKA_BOT_TOKEN")
    if not token:
        print("RUBIKA_BOT_TOKEN tanzim nashode, verify bot run nemishe.")
        return

    admin_chat_id = os.environ.get("ADMIN_CHAT_ID", "").strip()
    texts_file = "texts_verify_bot.json"

    default_texts = {
        "start_message": (
            "سلام\n\n"
            "برای استفاده از ربات، لطفا ابتدا توی سایت زیر ثبت نام کن:\n"
            "https://example.com\n\n"
            "بعد از ثبت نام، یه اسکرین شات از صفحه ثبت نامت همینجا برام بفرست تا بررسی کنم."
        ),
        "screenshot_reply": "اسکرین شاتت دریافت شد، در حال بررسی هست. نتیجه رو بهت اطلاع می دم.",
    }

    def load_texts():
        if os.path.exists(texts_file):
            with open(texts_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return default_texts.copy()

    def save_texts(data):
        with open(texts_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    texts = load_texts()

    bot = Robot(token)

    def get_file_id(file_obj):
        if file_obj is None:
            return None
        file_id = getattr(file_obj, "file_id", None)
        if file_id is not None:
            return file_id
        if isinstance(file_obj, dict):
            return file_obj.get("file_id")
        return None

    async def forward_screenshot_to_admin(bot: Robot, message: Message, file_id):
        try:
            await bot.forward_message(
                from_chat_id=message.chat_id,
                message_id=message.message_id,
                to_chat_id=admin_chat_id,
            )
            return
        except Exception as e:
            print("[verify_bot] khata dar forward_message: " + str(e))

        try:
            await bot.send_message(
                chat_id=admin_chat_id,
                text="Screenshot jadid az user (nashod forward konam)\nchat_id: " + str(message.chat_id) +
                "\nfile_id: " + str(file_id),
            )
        except Exception as e2:
            print("[verify_bot] khata dar ersal matn be admin: " + str(e2))

    @bot.on_message(commands=["start"])
    async def start(bot: Robot, message: Message):
        try:
            await message.reply(texts["start_message"])
        except Exception as e:
            print("[verify_bot] khata dar start: " + str(e))

    @bot.on_message()
    async def handle_message(bot: Robot, message: Message):
        try:
            await _handle_message_inner(bot, message)
        except Exception as e:
            print("[verify_bot] khata dar handle_message: " + str(e))

    async def _handle_message_inner(bot: Robot, message: Message):
        text = (message.text or "").strip()

        print("[verify_bot] payam az chat_id=" + str(message.chat_id) + " text=" + text)

        if admin_chat_id and message.chat_id == admin_chat_id:
            if text.startswith("/setstart "):
                texts["start_message"] = text[len("/setstart "):]
                save_texts(texts)
                await message.reply("Matn start update shod.")
                return

            if text.startswith("/setreply "):
                texts["screenshot_reply"] = text[len("/setreply "):]
                save_texts(texts)
                await message.reply("Matn pasokh screenshot update shod.")
                return

            if text == "/gettexts":
                await message.reply(
                    "Start:\n" + texts["start_message"] +
                    "\n\nScreenshot reply:\n" + texts["screenshot_reply"]
                )
                return

        if getattr(message, "file", None):
            await message.reply(texts["screenshot_reply"])

            if admin_chat_id and message.chat_id != admin_chat_id:
                file_id = get_file_id(message.file)
                await forward_screenshot_to_admin(bot, message, file_id)
            return

    print("[verify_bot] ejra shod")
    bot.run()


# ===========================================================
# ربات ۲: جای خالی برای ربات بعدی
# توکن: BOT2_TOKEN
# ===========================================================


def run_bot2():
    token = os.environ.get("BOT2_TOKEN")
    if not token:
        print("BOT2_TOKEN tanzim nashode, bot2 run nemishe.")
        return

    bot = Robot(token)

    @bot.on_message(commands=["start"])
    async def start(bot: Robot, message: Message):
        try:
            await message.reply("Salam! man bot 2 hastam.")
        except Exception as e:
            print("[bot2] khata dar start: " + str(e))

    print("[bot2] ejra shod")
    bot.run()


# ===========================================================
# اجرای همزمان همه ربات‌ها
# باری اضافه کردن ربات جدید: یه تابع مثل run_bot2 بساز و به لیست زیر اضافه کن
# ===========================================================
BOT_FUNCTIONS = [
    run_verify_bot,
    run_bot2,
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
