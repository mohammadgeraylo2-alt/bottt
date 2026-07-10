import os
import json
from rubka import Robot
from rubka.context import Message

# ---------------------------------------------------------
# تنظیمات - از Environment Variables خونده میشن
# ---------------------------------------------------------
BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

# chat_id اکانت خودت (ادمین) که قراره اسکرین‌شات‌ها براش بیاد
# اگه نمی‌دونیش، اول بدون این متغیر اجرا کن، یه پیام به ربات بفرست،
# و از لاگ Railway توی handle_message مقدارش رو پیدا کن
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

TEXTS_FILE = "texts.json"

DEFAULT_TEXTS = {
    "start_message": (
        "سلام 👋\n\n"
        "برای استفاده از ربات، لطفاً ابتدا توی سایت زیر ثبت‌نام کن:\n"
        "🔗 https://example.com\n\n"
        "بعد از ثبت‌نام، یه اسکرین‌شات از صفحه ثبت‌نامت همینجا برام بفرست تا بررسی کنم."
    ),
    "screenshot_reply": "✅ اسکرین‌شاتت دریافت شد، در حال بررسی هست. نتیجه رو بهت اطلاع می‌دم.",
}


def load_texts():
    if os.path.exists(TEXTS_FILE):
        with open(TEXTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return DEFAULT_TEXTS.copy()


def save_texts(data):
    with open(TEXTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


texts = load_texts()

bot = Robot(BOT_TOKEN)


@bot.on_message(commands=["start"])
async def start(bot: Robot, message: Message):
    await message.reply(texts["start_message"])


@bot.on_message()
async def handle_message(bot: Robot, message: Message):
    text = (message.text or "").strip()

    # لاگ chat_id هر کسی که پیام می‌ده (برای پیدا کردن ADMIN_CHAT_ID اولیه)
    print(f"📩 پیام از chat_id={message.chat_id} | text={text!r}")

    # --- دستورات ادمین ---
    if ADMIN_CHAT_ID and message.chat_id == ADMIN_CHAT_ID:
        if text.startswith("/setstart "):
            texts["start_message"] = text[len("/setstart "):]
            save_texts(texts)
            await message.reply("✅ متن استارت بروزرسانی شد.")
            return

        if text.startswith("/setreply "):
            texts["screenshot_reply"] = text[len("/setreply "):]
            save_texts(texts)
            await message.reply("✅ متن پاسخ اسکرین‌شات بروزرسانی شد.")
            return

        if text == "/gettexts":
            await message.reply(
                f"📝 متن استارت:\n{texts['start_message']}\n\n"
                f"📝 متن پاسخ اسکرین‌شات:\n{texts['screenshot_reply']}"
            )
            return

    # --- عکس / اسکرین‌شات ---
    if getattr(message, "file", None):
        await message.reply(texts["screenshot_reply"])

        if ADMIN_CHAT_ID and message.chat_id != ADMIN_CHAT_ID:
            await bot.send_message(
                ADMIN_CHAT_ID,
                f"📸 اسکرین‌شات جدید از کاربر\nchat_id: {message.chat_id}\nfile_id: {message.file.get('file_id')}",
            )
        return


if __name__ == "__main__":
    print("🤖 ربات رسمی روبیکا روش
ن شد...")
    bot.run()
