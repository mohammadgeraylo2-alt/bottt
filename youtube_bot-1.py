from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
import os
import glob
import re

TOKEN = os.environ["YT_BOT_TOKEN"]
RAPIDAPI_KEY = os.environ["RAPIDAPI_KEY"]
CHANNEL = "@downloader_hamechi"
CAPTION = "📥 دانلود شده توسط\n@downloader_hamechi"

user_urls = {}

def extract_video_id(url):
    patterns = [
        r"youtu\.be/([^?&]+)",
        r"youtube\.com/watch\?v=([^&]+)",
        r"youtube\.com/shorts/([^?&]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m: return m.group(1)
    return url

# ─── چک عضویت ────────────────────────────────────────────────
async def is_member(bot, user_id):
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def not_joined_message(update):
    keyboard = [
        [InlineKeyboardButton("عضویت در کانال 📢", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton("عضو شدم ✅", callback_data="check_join")]
    ]
    await update.message.reply_text(
        "برای استفاده از ربات باید عضو کانال ما باشی 👇",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def check_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if await is_member(context.bot, query.from_user.id):
        await query.answer("✅ عضویت تایید شد")
        await query.message.reply_text(
            "خوش اومدی! لینک یوتیوب رو بفرست 👇"
        )
    else:
        await query.answer("هنوز عضو نشدی!", show_alert=True)

# ─── /start ──────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_member(context.bot, update.message.from_user.id):
        await not_joined_message(update)
        return
    await update.message.reply_text(
        "🎬 *ربات دانلود یوتیوب*\n\n"
        "لینک ویدیوی یوتیوب رو بفرست تا دانلود کنم!\n\n"
        "📹 ویدیو با کیفیت‌های مختلف\n"
        "🎵 فقط صدا (MP3)",
        parse_mode="Markdown"
    )

# ─── دریافت لینک ─────────────────────────────────────────────
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if not await is_member(context.bot, user_id):
        await not_joined_message(update)
        return

    text = update.message.text.strip()
    yt_pattern = r"(https?://)?(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)[\S]+"
    if not re.search(yt_pattern, text):
        await update.message.reply_text("❌ لینک یوتیوب نیست!\n\nیه لینک youtube.com یا youtu.be بفرست.")
        return

    msg = await update.message.reply_text("⏳ دارم اطلاعات ویدیو رو میگیرم...")
    try:
        host = "youtube-media-downloader.p.rapidapi.com"
        headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host}
        r = requests.get(f"https://{host}/v2/video/details",
            headers=headers, params={"videoId": extract_video_id(text)}, timeout=20)
        info = r.json()

        if not info.get("status"):
            await msg.edit_text("❌ ویدیو پیدا نشد، لینک رو چک کن.")
            return

        title = info.get("title", "ویدیو")[:50]
        duration = info.get("lengthSeconds", 0)
        mins = int(duration) // 60
        secs = int(duration) % 60
        channel_name = info.get("author", {}).get("title", "")

        user_urls[user_id] = {"url": text, "title": title, "info": info}

        # کیفیت‌های ویدیو
        videos = info.get("videos", {}).get("items", [])
        seen = set()
        video_buttons = []
        for v in videos:
            height = v.get("height")
            if not height or height in seen: continue
            seen.add(height)
            video_buttons.append(
                InlineKeyboardButton(f"📹 {height}p", callback_data=f"dl_video_{height}")
            )
            if len(video_buttons) >= 4: break

        keyboard = []
        for i in range(0, len(video_buttons), 2):
            keyboard.append(video_buttons[i:i+2])
        keyboard.append([InlineKeyboardButton("🎵 فقط صدا (MP3)", callback_data="dl_audio")])

        await msg.edit_text(
            f"🎬 *{title}*\n\n"
            f"👤 کانال: {channel_name}\n"
            f"⏱ مدت: {mins}:{secs:02d}\n\n"
            f"کیفیت دانلود رو انتخاب کن 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        await msg.edit_text(f"❌ خطا: {str(e)[:200]}")

# ─── callback دانلود ─────────────────────────────────────────
async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    data = query.data
    user_data = user_urls.get(user_id)
    if not user_data:
        await query.message.reply_text("❌ لینک پیدا نشد، دوباره بفرست.")
        return

    url = user_data["url"]
    title = user_data["title"]
    info = user_data["info"]
    msg = await query.message.reply_text("⬇️ دارم دانلود میکنم، صبر کن...")

    host = "youtube-media-downloader.p.rapidapi.com"
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": host}

    try:
        if data == "dl_audio":
            audios = info.get("audios", {}).get("items", [])
            if not audios:
                await msg.edit_text("❌ فایل صوتی پیدا نشد."); return
            audio_url = audios[0].get("url")
            content = requests.get(audio_url, timeout=60).content
            mp3_path = f"yt_{user_id}.mp3"
            with open(mp3_path, "wb") as f: f.write(content)
            await query.message.reply_audio(audio=open(mp3_path,"rb"), title=title, caption=CAPTION)
            await msg.delete()

        elif data.startswith("dl_video_"):
            height = int(data.replace("dl_video_", ""))
            videos = info.get("videos", {}).get("items", [])
            # پیدا کردن نزدیک‌ترین کیفیت
            target = None
            for v in sorted(videos, key=lambda x: abs((x.get("height") or 0) - height)):
                if v.get("url"):
                    target = v
                    break
            if not target:
                await msg.edit_text("❌ کیفیت مورد نظر پیدا نشد."); return

            video_url = target.get("url")
            content = requests.get(video_url, timeout=120, stream=True)
            out_path = f"yt_{user_id}.mp4"
            with open(out_path, "wb") as f:
                for chunk in content.iter_content(chunk_size=1024*1024):
                    f.write(chunk)

            size_mb = os.path.getsize(out_path) / (1024*1024)
            if size_mb > 50:
                await msg.edit_text(f"❌ فایل خیلی بزرگه ({size_mb:.0f} MB)\nیه کیفیت پایین‌تر انتخاب کن.")
                return

            await query.message.reply_video(video=open(out_path,"rb"), caption=CAPTION, supports_streaming=True)
            await msg.delete()

    except Exception as e:
        await msg.edit_text(f"❌ خطا در دانلود:\n{str(e)[:300]}")
    finally:
        for f in glob.glob(f"yt_{user_id}.*"):
            try: os.remove(f)
            except: pass


# ─── handlers ─────────────────────────────────────────────────
app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(check_join_callback, pattern="^check_join$"))
app.add_handler(CallbackQueryHandler(download_callback, pattern="^dl_"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
app.run_polling()
