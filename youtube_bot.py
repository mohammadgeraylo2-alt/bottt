from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
import os
import glob
import re

TOKEN = os.environ["YT_BOT_TOKEN"]
CHANNEL = "@downloader_hamechi"
CAPTION = "📥 دانلود شده توسط\n@downloader_hamechi"

user_urls = {}

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
        ydl_opts = {"quiet": True, "noplaylist": True, "extract_flat": False}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(text, download=False)

        title = info.get("title", "ویدیو")[:50]
        duration = info.get("duration", 0)
        mins = duration // 60
        secs = duration % 60
        channel_name = info.get("channel") or info.get("uploader", "")

        user_urls[user_id] = {"url": text, "title": title, "info": info}

        # کیفیت‌های موجود
        formats = info.get("formats", [])
        seen = set()
        video_buttons = []
        for f in reversed(formats):
            height = f.get("height")
            ext = f.get("ext")
            if not height or ext not in ("mp4", "webm"):
                continue
            label = f"{height}p"
            if label in seen:
                continue
            seen.add(label)
            video_buttons.append(
                InlineKeyboardButton(f"📹 {label}", callback_data=f"dl_video_{height}")
            )
            if len(video_buttons) >= 4:
                break

        keyboard = []
        # دو تا دو تا
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
        await msg.edit_text(f"❌ خطا در دریافت اطلاعات:\n{str(e)[:200]}")

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
    msg = await query.message.reply_text("⬇️ دارم دانلود میکنم، صبر کن...")

    try:
        if data == "dl_audio":
            # دانلود MP3
            out_tmpl = f"yt_{user_id}.%(ext)s"
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": out_tmpl,
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
                "quiet": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            mp3_path = f"yt_{user_id}.mp3"
            if not os.path.exists(mp3_path):
                found = glob.glob(f"yt_{user_id}.*")
                if found: mp3_path = found[0]

            await query.message.reply_audio(
                audio=open(mp3_path, "rb"),
                title=title,
                caption=CAPTION
            )
            await msg.delete()

        elif data.startswith("dl_video_"):
            height = data.replace("dl_video_", "")
            out_path = f"yt_{user_id}.mp4"
            ydl_opts = {
                "format": f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/best[height<={height}][ext=mp4]/best[height<={height}]/best",
                "outtmpl": out_path,
                "merge_output_format": "mp4",
                "quiet": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            if not os.path.exists(out_path):
                found = glob.glob(f"yt_{user_id}.*")
                if found: os.rename(found[0], out_path)

            size_mb = os.path.getsize(out_path) / (1024 * 1024)
            if size_mb > 50:
                await msg.edit_text(
                    f"❌ فایل خیلی بزرگه ({size_mb:.0f} MB)\n"
                    f"تلگرام فقط تا 50MB قبول میکنه.\n"
                    f"یه کیفیت پایین‌تر انتخاب کن."
                )
                return

            await query.message.reply_video(
                video=open(out_path, "rb"),
                caption=CAPTION,
                supports_streaming=True
            )
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
