import os, re, json, httpx, logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
import difflib  # add this at the top with other imports
import os, json, tempfile


PORT                    = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")

logging.basicConfig(level=logging.INFO)
SHEET_ID             = "1xfTGJ6akoa-AkgP414gqsFvhzzhrOPBoQYwaqIl6eY8"

# Render stores the JSON content as an env var; locally we fall back to a file
_SA_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "")
if _SA_JSON:
    _SA_TEMP = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    _SA_TEMP.write(_SA_JSON)
    _SA_TEMP.close()
    SERVICE_ACCOUNT_FILE = _SA_TEMP.name
else:
    SERVICE_ACCOUNT_FILE = "your-service-account-key.json"  # local fallback
MY_MAP_URL           = "https://shinerchua.github.io/food-map/"   # the full mymaps.google.com/... link
TELEGRAM_TOKEN = "8072591082:AAGXun1XCnRUIuT-lmczSgPqWIgVPfH98zY"
GROQ_API_KEY   = "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"
GMAPS_API_KEY  = "AIzaSyCekaXF69kJc_ui6XkQd9CHpqTtj_mOnjI"


URL_PATTERN = re.compile(r"https?://[^\s]+")
IG_PATTERN  = re.compile(r"instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)")

def get_google_creds():
    """Load service account credentials from env variable or file."""
    from google.oauth2 import service_account
    import json

    json_str = os.environ.get("SERVICE_ACCOUNT_JSON", "")
    if json_str:
        info = json.loads(json_str)
        return service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    # Fallback to file for local development
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

# ── Instagram: get caption via oEmbed (no login needed) ───────────────────────
async def get_instagram_caption(url: str) -> str:
    """Fetch the post caption using Instagram's public oEmbed endpoint."""
    try:
        oembed_url = f"https://www.instagram.com/api/v1/oembed/?url={url}"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(oembed_url)
            data = r.json()
            # oEmbed gives author name and title
            title  = data.get("title", "")
            author = data.get("author_name", "")
            return f"Post by @{author}: {title}"
    except Exception as e:
        logging.warning(f"oEmbed failed: {e}")
        return ""

# ── Generic page fetch ─────────────────────────────────────────────────────────
async def fetch_page(url: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}) as c:
            return (await c.get(url)).text[:2000]
    except:
        return ""

# ── AI extraction ──────────────────────────────────────────────────────────────
async def extract_place(url: str, extra_hint: str = "") -> dict | None:
    is_instagram = bool(IG_PATTERN.search(url))

    if is_instagram:
        caption = await get_instagram_caption(url)
        context = f"Instagram post caption: {caption}" if caption else "Instagram post (caption unavailable)"
    else:
        page = await fetch_page(url)
        context = f"Page content: {page}" if page else "(page could not be loaded)"

    if extra_hint:
        context += f"\n\nUser provided hint: {extra_hint}"

    prompt = f"""You are a food place extractor. Extract the restaurant or food place being featured.

Return ONLY a raw JSON object — no markdown, no explanation:
{{"name": "...", "address": "...", "category": "...", "note": "..."}}

- name: the restaurant/café/hawker name
- address: full address if found, otherwise just the city/area
- category: type of food (e.g. Japanese, Hawker, Café)
- note: any useful info like must-try dishes or price range

If you cannot identify a food place, return: {{"error": "not found"}}

URL: {url}
{context}"""

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post("https://api.groq.com/openai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"})
        r.raise_for_status()

    text = r.json()["choices"][0]["message"]["content"].strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start != -1 and end:
        text = text[start:end]
    return json.loads(text)

# ── Google Maps coordinates ────────────────────────────────────────────────────
async def resolve_coords(name: str, address: str) -> tuple | None:
    """Use Nominatim (OpenStreetMap) — completely free, no API key needed."""
    query = f"{name} {address} Singapore".strip()
    logging.info(f"Nominatim query: {query}")

    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={
        "User-Agent": "FoodMapBot/1.0 (your@email.com)"  # Nominatim requires a User-Agent
    }) as c:
        r = await c.get("https://nominatim.openstreetmap.org/search", params={
            "q": query,
            "format": "json",
            "limit": 1,
            "countrycodes": "sg",
        })
        results = r.json()

    if not results:
        # Retry with just the name if name+address gives nothing
        logging.info(f"Nominatim retry with name only: {name}")
        async with httpx.AsyncClient(timeout=10, headers={
            "User-Agent": "FoodMapBot/1.0 (your@email.com)"
        }) as c:
            r = await c.get("https://nominatim.openstreetmap.org/search", params={
                "q": f"{name} Singapore",
                "format": "json",
                "limit": 1,
                "countrycodes": "sg",
            })
            results = r.json()

    if not results:
        logging.warning(f"Nominatim found nothing for: {query}")
        return None

    lat = float(results[0]["lat"])
    lng = float(results[0]["lon"])
    logging.info(f"Nominatim result: {lat}, {lng}")
    return lat, lng

def maps_link(place: dict) -> str:
    if place.get("lat"):
        return f"https://www.google.com/maps?q={place['lat']},{place['lng']}"
    return "https://www.google.com/maps/search/" + place.get("name", "").replace(" ", "+")

# ── Handlers ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍜 *Food Map Bot*\n\n"
        "Paste any food place link and I'll save it to Google Maps!\n\n"
        "✅ Google Maps, Burpple, Chope, TripAdvisor\n"
        "✅ Instagram posts & reels\n"
        "✅ Any restaurant website\n\n"
        "*Tip for Instagram:* Add the place name after the link if the bot can't detect it.\n"
        "Example: `https://instagram.com/reel/xxx  Odette Restaurant`",
        parse_mode="Markdown")

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    match = URL_PATTERN.search(text)
    if not match:
        # Check if user is providing a hint for a pending Instagram post
        if ctx.user_data.get("pending_url"):
            await process_link(update, ctx, ctx.user_data["pending_url"], hint=text)
            ctx.user_data.pop("pending_url", None)
            return
        await update.message.reply_text("Please send me a URL! 🔗")
        return

    url = match.group(0)
    # Anything after the URL is treated as a hint
    hint = text[match.end():].strip()
    await process_link(update, ctx, url, hint=hint)

async def process_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE, url: str, hint: str = ""):
    is_instagram = bool(IG_PATTERN.search(url))
    msg = await update.message.reply_text(
        "🔍 Reading the Instagram post…" if is_instagram else "🔍 Reading the link…"
    )

    try:
        place = await extract_place(url, extra_hint=hint)
    except Exception as e:
        logging.error(f"extract_place error: {e}")
        await msg.edit_text("❌ Something went wrong extracting the place. Try again.")
        return

    if not place or "error" in place:
        if is_instagram:
            ctx.user_data["pending_url"] = url
            await msg.edit_text(
                "📸 I couldn't detect the food place from this Instagram post.\n\n"
                "Please reply with the *restaurant name* and I'll look it up!\n"
                "Example: `Odette Restaurant Singapore`",
                parse_mode="Markdown"
            )
        else:
            await msg.edit_text("❌ Couldn't find a food place at that link. Try another URL.")
        return

    # ── DEBUG: show what was extracted before coords ──
    logging.info(f"Extracted place: {json.dumps(place, indent=2)}")
    await msg.edit_text(f"⏳ Got place: *{place.get('name')}*\nNow resolving coordinates…", parse_mode="Markdown")

    coords = await resolve_coords(place.get("name", ""), place.get("address", ""))
    
    # ── DEBUG: show coords result ──
    logging.info(f"Resolved coords: {coords}")
    
    if coords:
        place["lat"], place["lng"] = coords
        await msg.edit_text(f"⏳ Got coords: `{coords[0]:.4f}, {coords[1]:.4f}`\nPreparing card…", parse_mode="Markdown")
    else:
        await msg.edit_text(f"⚠️ No coords found for *{place.get('name')}*\nCheck terminal for errors.", parse_mode="Markdown")
        logging.warning(f"resolve_coords returned None for: {place.get('name')} | {place.get('address')}")

    ctx.user_data["pending"] = place

    card = (
        f"🍽️ *{place.get('name', '?')}*\n"
        f"📍 {place.get('address', 'Address not found')}\n"
        f"🏷️ {place.get('category', '')}"
    )
    if place.get("note"):
        card += f"\n📝 {place['note']}"
    if place.get("lat"):
        card += f"\n🌐 `{place['lat']:.4f}, {place['lng']:.4f}`"
    else:
        card += f"\n⚠️ _No coordinates — pin may not appear on map_"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Save to Google Maps", callback_data="save"),
        InlineKeyboardButton("❌ Cancel",              callback_data="cancel"),
    ]])
    await msg.edit_text(card, parse_mode="Markdown", reply_markup=kb)

async def append_to_sheet(place: dict):
    """Write one row to Google Sheets."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = get_google_creds()

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    row = [[
        place.get("name", ""),
        place.get("address", ""),
        place.get("category", ""),
        place.get("note", ""),
        place.get("lat", ""),
        place.get("lng", ""),
    ]]
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="Sheet1!A:F",
        valueInputOption="USER_ENTERED",
        body={"values": row},
    ).execute()

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "cancel":
        await q.edit_message_text("Cancelled. Send another link anytime! 👋")
        return

    if q.data.startswith("delete_confirm:"):
        row = int(q.data.split(":")[1])
        name = q.data.split(":")[2]
        await delete_sheet_row(row)
        await q.edit_message_text(f"🗑️ *{name}* removed from your map!", parse_mode="Markdown")
        return

    if q.data == "delete_cancel":
        await q.edit_message_text("Deletion cancelled.")
        return
    
    if q.data == "force_save":
        place = ctx.user_data.get("pending_duplicate") or ctx.user_data.get("pending")
        if not place:
            await q.edit_message_text("Session expired.")
            return
        await do_save(q, place)
        return

    if q.data.startswith("edit_field:"):
        _, field, row = q.data.split(":")
        ctx.user_data["editing"] = {"field": field, "row": int(row)}
        labels = {"name": "name", "address": "address", "category": "category", "note": "note"}
        await q.edit_message_text(
            f"✏️ Send me the new *{labels[field]}*:",
            parse_mode="Markdown"
        )
        return

    # ── Save new place ──
    place = ctx.user_data.get("pending")
    if not place:
        await q.edit_message_text("Session expired. Please send the link again.")
        return

    await q.edit_message_text("⏳ Checking for duplicates…")

    existing = await read_sheet()
    if is_duplicate(place.get("name", ""), existing):
        await q.edit_message_text(
            f"⚠️ *{place.get('name')}* looks like it's already on your map!\n\n"
            "Send the link again and tap Save to add it anyway, or cancel.",
            parse_mode="Markdown"
        )
        ctx.user_data["pending_duplicate"] = place
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add anyway", callback_data="force_save"),
            InlineKeyboardButton("❌ Cancel",     callback_data="cancel"),
        ]])
        await q.edit_message_text(
            f"⚠️ *{place.get('name')}* looks like it's already saved!\n\nAdd it anyway?",
            parse_mode="Markdown",
            reply_markup=kb
        )
        return

    await do_save(q, place)

async def do_save(q, place: dict):
    """Actually append to sheet and confirm."""
    await append_to_sheet(place)
    maps_link_str = maps_link(place)
    await q.edit_message_text(
        f"✅ *{place.get('name')}* pinned on your map!\n\n"
        f"📍 [See it on Google Maps]({maps_link_str})\n"
        f"🗺️ [View your full Food Map]({MY_MAP_URL})",
        parse_mode="Markdown"
    )
    

async def delete_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    places = await read_sheet()
    if not places:
        await update.message.reply_text("Your map is empty!")
        return

    await update.message.reply_text(
        "🗑️ *Which place do you want to remove?*\nTap one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{p['name']} — {p['address'][:30]}",
                callback_data=f"delete_confirm:{p['row']}:{p['name']}"
            )]
            for p in places
        ])
    )

async def edit_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    places = await read_sheet()
    if not places:
        await update.message.reply_text("Your map is empty!")
        return

    ctx.user_data["edit_places"] = {p["row"]: p for p in places}

    await update.message.reply_text(
        "✏️ *Which place do you want to edit?*\nTap one:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{p['name']} — {p['address'][:30]}",
                callback_data=f"edit_select:{p['row']}"
            )]
            for p in places
        ])
    )

async def handle_edit_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """After user picks a place to edit, show which field to change."""
    q = update.callback_query
    await q.answer()
    row = int(q.data.split(":")[1])
    place = ctx.user_data.get("edit_places", {}).get(row)
    if not place:
        await q.edit_message_text("Couldn't find that place. Try /edit again.")
        return

    ctx.user_data["editing_place"] = place
    await q.edit_message_text(
        f"✏️ Editing *{place['name']}*\n\nWhich field?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📛 Name",     callback_data=f"edit_field:name:{row}")],
            [InlineKeyboardButton("📍 Address",  callback_data=f"edit_field:address:{row}")],
            [InlineKeyboardButton("🏷️ Category", callback_data=f"edit_field:category:{row}")],
            [InlineKeyboardButton("📝 Note",     callback_data=f"edit_field:note:{row}")],
        ])
    )

async def handle_edit_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle the user typing the new value for a field."""
    editing = ctx.user_data.get("editing")
    if not editing:
        # Not in edit mode — treat as normal message
        await handle_message(update, ctx)
        return

    field = editing["field"]
    row   = editing["row"]
    new_value = update.message.text.strip()

    place = ctx.user_data.get("editing_place", {})
    place[field] = new_value

    # If name or address changed, re-resolve coordinates
    if field in ("name", "address"):
        await update.message.reply_text("📍 Re-resolving coordinates…")
        coords = await resolve_coords(place.get("name",""), place.get("address",""))
        if coords:
            place["lat"], place["lng"] = str(coords[0]), str(coords[1])

    await update_sheet_row(row, place)
    ctx.user_data.pop("editing", None)
    ctx.user_data.pop("editing_place", None)

    await update.message.reply_text(
        f"✅ Updated! *{place.get('name')}* now has {field}: _{new_value}_\n\n"
        f"🗺️ [View your map]({MY_MAP_URL})",
        parse_mode="Markdown"
    )

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    saved = ctx.bot_data.get("saved", [])
    if not saved:
        await update.message.reply_text("No places saved yet!")
        return
    lines = ["📍 *Your saved places:*\n"]
    for i, p in enumerate(saved[-10:], 1):
        lines.append(f"{i}. *{p['name']}* — {p.get('address', '')}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



# ── Duplicate detection ────────────────────────────────────────────────────────
def is_duplicate(new_name: str, existing_places: list) -> bool:
    """Returns True if a very similar name already exists in the sheet."""
    new_clean = new_name.lower().strip()
    for p in existing_places:
        existing_clean = p.get("name", "").lower().strip()
        ratio = difflib.SequenceMatcher(None, new_clean, existing_clean).ratio()
        if ratio > 0.85:  # 85% similar = likely duplicate
            return True
    return False

async def read_sheet() -> list[dict]:
    """Read all current rows from Google Sheet."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = get_google_creds()

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="Sheet1!A:F"
    ).execute()
    rows = result.get("values", [])
    if len(rows) <= 1:  # only header or empty
        return []
    places = []
    for i, row in enumerate(rows[1:], start=2):  # start=2 because row 1 is header
        while len(row) < 6:
            row.append("")
        places.append({
            "row": i,
            "name": row[0],
            "address": row[1],
            "category": row[2],
            "note": row[3],
            "lat": row[4],
            "lng": row[5],
        })
    return places

async def delete_sheet_row(row_number: int):
    """Delete a specific row from Google Sheet."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = get_google_creds()

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{
            "deleteDimension": {
                "range": {
                    "sheetId": 0,
                    "dimension": "ROWS",
                    "startIndex": row_number - 1,  # 0-indexed
                    "endIndex": row_number,
                }
            }
        }]}
    ).execute()

async def update_sheet_row(row_number: int, place: dict):
    """Update a specific row in Google Sheet."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = get_google_creds()

    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"Sheet1!A{row_number}:F{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [[
            place.get("name", ""),
            place.get("address", ""),
            place.get("category", ""),
            place.get("note", ""),
            place.get("lat", ""),
            place.get("lng", ""),
        ]]}
    ).execute()


# ── Run ────────────────────────────────────────────────────────────────────────
# ── Run ───────────────────────────────────────────────────────────────────────
import sys
from aiohttp import web

async def health(request):
    return web.Response(text="OK")

async def webhook_handler(request):
    app_bot = request.app["bot_app"]
    data = await request.json()
    update = Update.de_json(data, app_bot.bot)
    await app_bot.process_update(update)
    return web.Response(text="OK")

async def main():
    token   = TELEGRAM_TOKEN
    webhook = f"https://{RENDER_EXTERNAL_HOSTNAME}/webhook/{token}"

    bot_app = Application.builder().token(token).build()
    bot_app.add_handler(CommandHandler("start",  start))
    bot_app.add_handler(CommandHandler("list",   list_cmd))
    bot_app.add_handler(CommandHandler("delete", delete_cmd))
    bot_app.add_handler(CommandHandler("edit",   edit_cmd))
    bot_app.add_handler(CallbackQueryHandler(handle_edit_select, pattern="^edit_select:"))
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))

    await bot_app.initialize()
    await bot_app.bot.set_webhook(webhook)
    logging.info(f"Webhook set to: {webhook}")

    web_app = web.Application()
    web_app["bot_app"] = bot_app
    web_app.rou