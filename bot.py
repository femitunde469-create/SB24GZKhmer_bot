import asyncio
import io
import logging
import os

import cv2
import numpy as np
import qrcode
from qrcode.constants import (
    ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
)
from PIL import Image

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Limit concurrent QR jobs (light work; 3 is fine)
qr_semaphore = asyncio.Semaphore(3)

COLORS = {
    "black": ("black", "white"),
    "blue":  ("#0033cc", "white"),
    "red":   ("#cc0000", "white"),
    "green": ("#007744", "white"),
    "purple": ("#6600cc", "white"),
}

SIZES = [256, 512, 1024]

ECC_LEVELS = {
    "L": ERROR_CORRECT_L,
    "M": ERROR_CORRECT_M,
    "Q": ERROR_CORRECT_Q,
    "H": ERROR_CORRECT_H,
}

MAX_TEXT_LEN = 3000  # safety limit before attempting QR generation


# ---------- QR generation ----------
def _generate_qr(text: str, size: int = 512, fill: str = "black",
                 back: str = "white", ecc: str = "M") -> io.BytesIO:
    """Return a BytesIO with a PNG QR code at approximately the requested size."""
    qr = qrcode.QRCode(
        version=None,
        error_correction=ECC_LEVELS.get(ecc, ERROR_CORRECT_M),
        box_size=10,
        border=4,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill, back_color=back)
    if hasattr(img, "get_image"):
        img = img.get_image()
    img = img.resize((size, size), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{s}px", callback_data=f"size_{s}") for s in SIZES],
        [
            InlineKeyboardButton("⚫", callback_data="color_black"),
            InlineKeyboardButton("🔵", callback_data="color_blue"),
            InlineKeyboardButton("🔴", callback_data="color_red"),
            InlineKeyboardButton("🟢", callback_data="color_green"),
            InlineKeyboardButton("🟣", callback_data="color_purple"),
        ],
        [
            InlineKeyboardButton(f"ECC {lvl}", callback_data=f"ecc_{lvl}")
            for lvl in ("L", "M", "Q", "H")
        ],
    ])


def _caption(text: str, size: int, color: str, ecc: str) -> str:
    preview = text.replace("`", "'").replace("\n", " ").strip()
    if len(preview) > 80:
        preview = preview[:77] + "..."
    return (
        f"**QR Code Ready**\n"
        f"Size: {size}px · Color: {color} · ECC: {ecc}\n\n"
        f"`{preview}`"
    )


async def _send_qr(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   text: str, caption_prefix: str | None = None) -> None:
    """Generate a QR with default state, send it, and store per‑message state."""
    size, color, ecc = 512, "black", "M"
    fill, back = COLORS.get(color, ("black", "white"))
    try:
        buf = _generate_qr(text, size=size, fill=fill, back=back, ecc=ecc)
    except Exception:
        logger.exception("QR generation failed")
        await update.message.reply_text(
            "❌ Could not generate a QR code. The text may be too long."
        )
        return

    caption = _caption(text, size, color, ecc)
    if caption_prefix:
        caption = f"{caption_prefix}\n\n" + caption

    msg = await update.message.reply_photo(
        photo=buf,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=_main_keyboard(),
    )
    context.user_data.setdefault("qr_store", {})[msg.message_id] = {
        "text": text, "size": size, "color": color, "ecc": ecc,
    }


# ---------- Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🔳 **QR Code Bot**\n\n"
        "**Generate** — send me any text or URL and I'll create a QR code.\n"
        "Then use the inline buttons to change size, color, or error correction level.\n\n"
        "**Decode** — send a photo containing a QR code and I'll read it back to you.\n"
        "💡 For best decode results, send the QR code as a **file** (uncompressed).\n\n"
        "**Commands**\n"
        "/start – show this message\n"
        "/wifi  – generate a WiFi QR code (SSID + password)\n"
        "/cancel – clear current operation\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text("✅ Cleared. Send new text to generate a QR code.")


async def wifi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["wifi_step"] = "ssid"
    context.user_data.pop("wifi_ssid", None)
    await update.message.reply_text(
        "📶 **WiFi QR Generator**\n\n"
        "Send the WiFi network name (SSID). Use /cancel to abort."
    )


async def skip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("wifi_step") != "password":
        await update.message.reply_text("Nothing to skip right now.")
        return
    ssid = context.user_data.get("wifi_ssid", "")
    payload = f"WIFI:T:nopass;S:{ssid};;"
    context.user_data.pop("wifi_step", None)
    context.user_data.pop("wifi_ssid", None)
    await _send_qr(update, context, payload, caption_prefix="📶 WiFi QR (open network)")


# ---------- Text handler (generation + WiFi flow) ----------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with qr_semaphore:
        text = update.message.text.strip()
        if not text:
            return

        # --- WiFi multi-step flow ---
        step = context.user_data.get("wifi_step")
        if step == "ssid":
            context.user_data["wifi_ssid"] = text
            context.user_data["wifi_step"] = "password"
            await update.message.reply_text(
                "Now send the WiFi password, or /skip for an open network."
            )
            return
        if step == "password":
            ssid = context.user_data.get("wifi_ssid", "")
            payload = f"WIFI:T:WPA;S:{ssid};P:{text};;"
            context.user_data.pop("wifi_step", None)
            context.user_data.pop("wifi_ssid", None)
            await _send_qr(update, context, payload, caption_prefix="📶 WiFi QR")
            return

        # --- Regular text → QR ---
        if len(text) > MAX_TEXT_LEN:
            await update.message.reply_text(
                f"❌ Text too long for a QR code (max ~{MAX_TEXT_LEN} characters)."
            )
            return
        await _send_qr(update, context, text)


# ---------- Inline button handler (customization) ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    msg_id = query.message.message_id
    store = context.user_data.get("qr_store", {})
    state = store.get(msg_id)
    if not state:
        await query.answer(
            "This QR is from an earlier session. Send new text to create a fresh one.",
            show_alert=True,
        )
        return

    data = query.data
    if data.startswith("size_"):
        state["size"] = int(data.split("_", 1)[1])
    elif data.startswith("color_"):
        state["color"] = data.split("_", 1)[1]
    elif data.startswith("ecc_"):
        state["ecc"] = data.split("_", 1)[1]
    else:
        return

    fill, back = COLORS.get(state["color"], ("black", "white"))
    try:
        buf = _generate_qr(
            state["text"], size=state["size"],
            fill=fill, back=back, ecc=state["ecc"],
        )
    except Exception:
        logger.exception("QR regeneration failed")
        await query.answer("Could not regenerate. Try a smaller size.", show_alert=True)
        return

    try:
        await query.edit_message_media(
            media=InputMediaPhoto(
                media=buf,
                caption=_caption(state["text"], state["size"], state["color"], state["ecc"]),
                parse_mode="Markdown",
            ),
            reply_markup=_main_keyboard(),
        )
    except Exception:
        logger.warning("edit_message_media failed — falling back to new message")
        buf2 = _generate_qr(
            state["text"], size=state["size"],
            fill=fill, back=back, ecc=state["ecc"],
        )
        new_msg = await query.message.reply_photo(
            photo=buf2,
            caption=_caption(state["text"], state["size"], state["color"], state["ecc"]),
            parse_mode="Markdown",
            reply_markup=_main_keyboard(),
        )
        store[new_msg.message_id] = state


# ---------- Image handler (decode with OpenCV) ----------
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with qr_semaphore:
        message = update.effective_message
        if not message:
            return
        try:
            if message.photo:
                file = await message.photo[-1].get_file()
            elif message.document and (message.document.mime_type or "").startswith("image/"):
                file = await message.document.get_file()
            else:
                await message.reply_text("Please send a photo or an image file.")
                return

            file_bytes = await file.download_as_bytearray()

            # Convert bytes to an OpenCV image
            np_arr = np.frombuffer(file_bytes, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

            if img is None:
                await message.reply_text("❌ Could not read that image.")
                return

            detector = cv2.QRCodeDetector()

            # Try the original image first
            data, points, _ = detector.detectAndDecode(img)

            # Fallback: upscale grayscale for small/blurred codes
            if not data:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                h, w = gray.shape
                if max(h, w) < 2000:
                    scale = 2.0
                    gray = cv2.resize(
                        gray, (int(w * scale), int(h * scale)),
                        interpolation=cv2.INTER_LANCZOS4,
                    )
                data, points, _ = detector.detectAndDecode(gray)

            if not data:
                await message.reply_text(
                    "❌ No QR code detected. Try sending the image as a file "
                    "(uncompressed) or use a clearer photo."
                )
                return

            await message.reply_text("🔍 Decoded QR code:\n\n" + data)

        except Exception:
            logger.exception("QR decode failed")
            await message.reply_text("❌ Could not decode that image.")


# ---------- Main ----------
def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("wifi", wifi_command))
    app.add_handler(CommandHandler("skip", skip_command))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.Document.IMAGE) & ~filters.COMMAND,
            handle_image,
        )
    )

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("QR Code Bot starting in polling mode…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
