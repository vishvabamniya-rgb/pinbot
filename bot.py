import re
import time
import random
import io
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ============================================================
# 1) CONFIG
# ============================================================

# ⚠️ IMPORTANT:
# - DO NOT paste real token publicly.
# - BotFather: /revoke -> get new token -> paste here.
BOT_TOKEN = "8669466068:AAFLiY2J2b25vruqF0LCGG40GVoBDqd2MjI"

APP_NAME = "Pinnacle Academy"

CATEGORIES_API = "https://auth.ssccglpinnacle.com/categories"
COURSES_BY_CATEGORY_API = "https://auth.ssccglpinnacle.com/api/videoCourses/{category}"
CHAPTERS_BY_COURSE_API = "https://auth.ssccglpinnacle.com/api/youtubeChapters/course/{course_id}"
PDF_DETAIL_API = "https://auth.ssccglpinnacle.com/api/pdfs/{pdf_id}"
COURSE_DETAIL_API = "https://auth.ssccglpinnacle.com/course/{course_id}"

HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://videos.ssccglpinnacle.com",
    "referer": "https://videos.ssccglpinnacle.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}

# Performance/stability
MAX_WORKERS = 8
REQUEST_TIMEOUT = 20
RETRIES = 3
BACKOFF_BASE = 0.6
RETRY_STATUS = {429, 500, 502, 503, 504}

# Telegram message safety
LIST_LIMIT = 70

# ============================================================
# 2) BASIC VALIDATION
# ============================================================

if not BOT_TOKEN or "PASTE_NEW_TOKEN_HERE" in BOT_TOKEN:
    raise SystemExit("❌ Set BOT_TOKEN first (BotFather se new token paste karo).")


# ============================================================
# 3) HELPERS
# ============================================================

def safe_filename(name: str) -> str:
    """
    Make a safe filename for Telegram documents.
    (We still don't save to disk; just used as the download name.)
    """
    name = (name or "export").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return (name[:120] or "export")


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def http_get_json(session: requests.Session, url: str) -> Optional[Any]:
    """
    GET JSON with retry + backoff.
    """
    last_err = None
    for attempt in range(RETRIES):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code in RETRY_STATUS:
                raise requests.HTTPError(f"Retryable HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.25))

    print(f"❌ Error fetching {url}: {last_err}")
    return None


def get_course_detail(session: requests.Session, course_id: str) -> Optional[Dict[str, Any]]:
    if not course_id:
        return None
    url = COURSE_DETAIL_API.format(course_id=course_id)
    data = http_get_json(session, url)
    return data if isinstance(data, dict) else None


def get_real_pdf_url(pdf_id: str) -> Optional[str]:
    """
    Thread-safe PDF fetch:
    - new session per worker (requests.Session is NOT thread-safe)
    """
    if not pdf_id:
        return None

    s = make_session()
    data = http_get_json(s, PDF_DETAIL_API.format(pdf_id=pdf_id))
    if isinstance(data, dict):
        return (data.get("cloudFrontUrl") or "").strip() or None
    return None


def count_lines(lines: List[str]) -> Tuple[int, int, int]:
    total = len(lines)
    videos = sum(1 for x in lines if " VIDEO:" in x)
    pdfs = sum(1 for x in lines if " PDF:" in x)
    return total, videos, pdfs


def fmt_list(items: List[str], limit: int = LIST_LIMIT) -> str:
    out = "\n".join(items[:limit])
    if len(items) > limit:
        out += f"\n… +{len(items) - limit} more"
    return out


def pick_cover_url(course_detail: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Prefer English cover, fallback Hindi cover.
    """
    if not course_detail:
        return None
    url = (course_detail.get("englishCoverImage") or course_detail.get("hindiCoverImage") or "").strip()
    return url or None


# ============================================================
# 4) FLOW (State Machine)
# ============================================================
# context.user_data keys:
#   step: "cat" | "course" | "exporting" | None
#   categories: list[dict]
#   courses: list[dict]
#   chosen_cat_title: str
#
# Behavior:
#   /pinnacle shows categories
#   user sends number -> shows courses
#   user sends number -> extracts and sends txt
#
# No disk saving. No encryption.

# ============================================================
# 5) COMMAND HANDLERS
# ============================================================




async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Cancelled. Send /pinnacle again.")


async def pinnacle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ✅ prevent duplicates like your screenshot (multiple /pinnacle)
    if context.user_data.get("step") in ("cat", "course", "exporting"):
        await update.message.reply_text("⚠️ Already running. Send number or /cancel.")
        return

    context.user_data.clear()
    context.user_data["step"] = "cat"


    session = make_session()
    cats = http_get_json(session, CATEGORIES_API)

    if not cats or not isinstance(cats, list):
        context.user_data.clear()
        await update.message.reply_text("❌ Categories load failed.")
        return

    categories = [c for c in cats if c.get("categoryTitle")]
    if not categories:
        context.user_data.clear()
        await update.message.reply_text("❌ No categories found.")
        return

    context.user_data["categories"] = categories

    lines = [f"🔸 {i} → {c['categoryTitle']}" for i, c in enumerate(categories, start=1)]
    await update.message.reply_text(
        "Enter category number:\n\n" + fmt_list(lines),
        disable_web_page_preview=True
    )


# ============================================================
# 6) TEXT INPUT HANDLER
# ============================================================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("step")
    if step not in ("cat", "course"):
        return

    msg = (update.message.text or "").strip()
    if not msg.isdigit():
        await update.message.reply_text("⚠️ Send a valid number (or /cancel).")
        return

    idx = int(msg) - 1

    # ------------------------------------------------------------
    # STEP 1: CATEGORY -> COURSES
    # ------------------------------------------------------------
    if step == "cat":
        categories = context.user_data.get("categories") or []
        if not (0 <= idx < len(categories)):
            await update.message.reply_text("⚠️ Number out of range.")
            return

        chosen = categories[idx]
        cat_title = (chosen.get("categoryTitle") or "").strip()
        if not cat_title:
            await update.message.reply_text("❌ Category title missing.")
            context.user_data.clear()
            return

        context.user_data["chosen_cat_title"] = cat_title

        await update.message.reply_text("⏳ Loading batches...")

        session = make_session()
        courses = http_get_json(session, COURSES_BY_CATEGORY_API.format(category=quote(cat_title)))

        if not courses or not isinstance(courses, list):
            await update.message.reply_text("❌ No courses found (or load failed).")
            context.user_data.clear()
            return

        context.user_data["step"] = "course"
        context.user_data["courses"] = courses

        lines = []
        for i, c in enumerate(courses, start=1):
            title = (c.get("courseTitle") or "Untitled").strip()
            price = c.get("price") or c.get("amount") or ""
            if price:
                lines.append(f"🔹 {i} → {title}  ₹{price}")
            else:
                lines.append(f"🔹 {i} → {title}")

        await update.message.reply_text(
            "Select batch number:\n\n" + fmt_list(lines),
            disable_web_page_preview=True
        )
        return

    # ------------------------------------------------------------
    # STEP 2: COURSE -> EXPORT TXT (MEMORY ONLY) + COVER LINE
    # ------------------------------------------------------------
    if step == "course":
        courses = context.user_data.get("courses") or []
        if not (0 <= idx < len(courses)):
            await update.message.reply_text("⚠️ Number out of range.")
            return

        course = courses[idx]
        course_title = (course.get("courseTitle") or "Course").strip()
        course_id = course.get("_id")

        if not course_id:
            await update.message.reply_text("❌ course_id missing.")
            context.user_data.clear()
            return

        context.user_data["step"] = "exporting"

        cat_title = context.user_data.get("chosen_cat_title", "Category")
        start_t = time.time()

        await update.message.reply_text(f"⏳ Extracting: {course_title} ...")

        # Use one session for sequential calls (detail + chapters)
        session = make_session()

        # 1) Course detail (cover image line)
        course_detail = get_course_detail(session, course_id)
        cover_url = pick_cover_url(course_detail)

        header_lines: List[str] = []
        if cover_url:
            header_lines.append(f"[Course Cover] {course_title} : {cover_url}")

        # 2) Chapters list
        chapters = http_get_json(session, CHAPTERS_BY_COURSE_API.format(course_id=course_id))
        if not chapters or not isinstance(chapters, list):
            await update.message.reply_text("❌ Chapters load failed / empty.")
            context.user_data.clear()
            return

        # Collect topics in order, collect unique pdf ids
        ordered_items: List[Dict[str, Optional[str]]] = []
        unique_pdf_ids: set[str] = set()

        for ch in chapters:
            chapter_title = (ch.get("chapterTitle") or "Chapter").strip()
            topics = ch.get("topics") or []

            for t in topics:
                vtitle = (t.get("videoTitle") or "No Title").strip()
                vurl = (t.get("videoYoutubeLink") or "").strip()
                pdf_id = t.get("selectedPdf") or t.get("pdf")
                pdf_id = str(pdf_id) if pdf_id else None

                ordered_items.append({
                    "chapter_title": chapter_title,
                    "vtitle": vtitle,
                    "vurl": vurl,
                    "pdf_id": pdf_id,
                })

                if pdf_id:
                    unique_pdf_ids.add(pdf_id)

        # 3) Fetch PDFs concurrently (proper mapping)
        pdf_cache: Dict[str, Optional[str]] = {}
        if unique_pdf_ids:
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                fut_map = {ex.submit(get_real_pdf_url, pid): pid for pid in unique_pdf_ids}
                for fut in as_completed(fut_map):
                    pid = fut_map[fut]
                    try:
                        pdf_cache[pid] = fut.result()
                    except Exception:
                        pdf_cache[pid] = None

        # 4) Build txt lines (header first)
        lines: List[str] = []
        lines.extend(header_lines)

        for item in ordered_items:
            ch_title = item["chapter_title"] or "Chapter"
            vtitle = item["vtitle"] or "No Title"
            vurl = item["vurl"] or ""
            pdf_id = item["pdf_id"]

            if vurl:
                lines.append(f"[{ch_title}] VIDEO: {vtitle} -> {vurl}")

            if pdf_id:
                real = pdf_cache.get(pdf_id)
                if real:
                    lines.append(f"[{ch_title}] PDF: {vtitle} -> {real}")

        total, videos, pdfs = count_lines(lines)
        took = round(time.time() - start_t, 2)

        # 5) Create in-memory TXT (NO DISK SAVE)
        txt_bytes = "\n".join(lines).encode("utf-8")
        bio = io.BytesIO(txt_bytes)

        out_name = safe_filename(f"{cat_title}_{course_title}") + ".txt"
        bio.name = out_name  # filename shown in Telegram

        # Caption like your screenshot
        price = course.get("price") or course.get("amount") or ""
        price_str = f"₹{price}" if price else "N/A"

        caption = (
            "<b>📦 Batch Extracted!</b>\n"
            f"<pre><b>📱 App:</b> {APP_NAME}</pre>\n\n"
            "<b>📦 Batch:</b>\n"
            f"┣ 📛 <code>{course_title}</code>\n"
            f"┗ 💵 {price_str}\n\n"
            "<b>📚 Content:</b>\n"
            f"┣ 🔗 Total: <b>{total}</b>\n"
            f"┣ 🎬 Videos: <b>{videos}</b>\n"
            f"┗ 📄 PDFs: <b>{pdfs}</b>\n\n"
            f"⏱ <b>Time Taken:</b> {took}s"
        )

        await update.message.reply_document(
            document=InputFile(bio, filename=out_name),
            caption=caption,
            parse_mode=ParseMode.HTML
        )

        context.user_data.clear()
        return


# ============================================================
# 7) MAIN
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

   

    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("pinnacle", pinnacle))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("✅ Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

