import os
import json
from rubka import Robot
from rubka.context import Message

BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

TEXTS_FILE = "texts.json"

DEFAULT_TEXTS = {
    "start_message": (
        "سلام\n\n"
        "برای استفاده از ربات، لطفا ابتدا توی سایت زیر ثبت نام کن:\n"
        "https://example.com\n\n"
        "بعد از ثبت نام، یه اسکرین شات از صفحه ثبت نامت همینجا برام بفرست تا بررسی کنم."
    ),
    "screenshot_reply": "اسکرین شاتت دریافت شد، در حال بررسی هست. نتیجه رو بهت اطلاع می دم.",
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


def get_file_id(file_obj):
    if file_obj is None:
        return None
    file_id = getattr(file_obj, "file_id", None)
    if file_id is not None:
        return file_id
    if isinstance(file_obj, dict):
        return file_obj.get("file_id")
    return None


@bot.on_message(commands=["start"])
async def start(bot: Robot, message: Message):
    try:
        await message.reply(texts["start_message"])
    except Exception as e:
        print("khata dar start: " + str(e))


@bot.on_message()
async def handle_message(bot: Robot, message: Message):
    try:
        await _handle_message_inner(bot, message)
    except Exception as e:
        print("khata dar handle_message: " + str(e))


async def _handle_message_inner(bot: Robot, message: Message):
    text = (message.text or "").strip()

    print("payam az chat_id=" + str(message.chat_id) + " text=" + text)

    if ADMIN_CHAT_ID and message.chat_id == ADMIN_CHAT_ID:
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

        if ADMIN_CHAT_ID and message.chat_id != ADMIN_CHAT_ID:
            file_id = get_file_id(message.file)
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text="Screenshot jadid az user\nchat_id: " + str(message.chat_id) +
                    "\nfile_id: " + str(file_id),
                )
            except Exception as e:
                print("khata dar ersal be admin: " + str(e))
        return


if __name__ == "__main__":
    print("Rubika bot started")
    bot.run()
