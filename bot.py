import os, re, json, httpx, logging, asyncio, difflib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

logging.basicConfig(level=logging.INFO)
SHEET_ID             = "1xfTGJ6akoa-AkgP414gqsFvhzzhrOPBoQYwaqIl6eY8"
SERVICE_ACCOUNT_FILE = "credentials.json"  # e.g. "foodbot-key.json"
MY_MAP_URL           = "https://shinerchua.github.io/food-map/"   # the full mymaps.google.com/... link
TELEGRAM_TOKEN = "8072591082:AAGXun1XCnRUIuT-lmczSgPqWIgVPfH98zY"
GROQ_API_KEY   = "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"
GMAPS_API_KEY  = "AIzaSyCekaXF69kJc_ui6XkQd9CHpqTtj_mOnjI"


URL_PATTERN = re.compile(r"https?://[^\s]+")
IG_PATTERN  = re.compile(r"instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)")

# ══════════════════════════════════════════════════════════════════════════════
# GOOGLE SHEETS
# ══════════════════════════════════════════════════════════════════════════════

def get_google_creds():
    from google.oauth2 import service_account
    import json as _json
    # On Render: paste the entire JSON key file content as SERVICE_ACCOUNT_JSON env var
    raw = os.environ.get("SERVICE_ACCOUNT_JSON", "")
    if raw:
        return service_account.Credentials.from_service_account_info(
            _json.loads(raw),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
    # Local dev: use the key file on disk
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

async def append_to_sheet(place: dict):
    from googleapiclient.discovery import build
    service = build("sheets", "v4", credentials=get_google_creds(), cache_discovery=False)
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range="Sheet1!A:F",
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

async def read_sheet() -> list[dict]:
    from googleapiclient.discovery import build
    service = build("sheets", "v4", credentials=get_google_creds(), cache_discovery=False)
    result = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Sheet1!A:F"
    ).execute()
    rows = result.get("values", [])
    if len(rows) <= 1:
        return []
    places = []
    for i, row in enumerate(rows[1:], start=2):
        while len(row) < 6:
            row.append("")
        places.append({
            "row": i, "name": row[0], "address": row[1],
            "category": row[2], "note": row[3], "lat": row[4], "lng": row[5],
        })
    return places

async def delete_sheet_row(row_number: int):
    from googleapiclient.discovery import build
    service = build("sheets", "v4", credentials=get_google_creds(), cache_discovery=False)
    service.spreadsheets().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"requests": [{"deleteDimension": {"range": {
            "sheetId": 0, "dimension": "ROWS",
            "startIndex": row_number - 1, "endIndex": row_number,
        }}}]}
    ).execute()

async def update_sheet_row(row_number: int, place: dict):
    from googleapiclient.discovery import build
    service = build("sheets", "v4", credentials=get_google_creds(), cache_discovery=False)
    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=f"Sheet1!A{row_number}:F{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [[
            place.get("name", ""), place.get("address", ""),
            place.get("category", ""), place.get("note", ""),
            place.get("lat", ""), place.get("lng", ""),
        ]]}
    ).execute()

# ══════════════════════════════════════════════════════════════════════════════
# DUPLICATE DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def is_duplicate(new_name: str, existing: list) -> bool:
    new_clean = new_name.lower().strip()
    for p in existing:
        ratio = difflib.SequenceMatcher(None, new_clean, p.get("name", "").lower().strip()).ratio()
        if ratio > 0.85:
            return True
    return False

# ══════════════════════════════════════════════════════════════════════════════
# COORDINATE RESOLUTION
# ══════════════════════════════════════════════════════════════════════════════
#
#  How it works (in order, first hit wins):
#
#  Step 1 — Extract any 6-digit postal code already in the address
#            → look it up on OneMap (most precise)
#
#  Step 2 — Google search for  "<name> Singapore"  and
#                               "<address> Singapore"
#            → scrape the first result snippets for a 6-digit postal code
#            → if found, pin it via OneMap
#
#  Step 3 — Google search for  "<name> <address> Singapore address"
#            → scrape snippets for a street address or postal code
#            → run that through OneMap
#
#  Step 4 — OneMap text search on address, then name (SG government geocoder)
#
#  Step 5 — Nominatim (OpenStreetMap) — name+address, then name only
#
#  Step 6 — Photon (Komoot) — most lenient, good last resort
#
# ══════════════════════════════════════════════════════════════════════════════

NOM_HEADERS  = {"User-Agent": "FoodMapBot/1.0 (food-map-bot)"}
DDG_HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
POSTAL_RE    = re.compile(r"\b([0-9]{6})\b")
SG_ADDR_RE   = re.compile(
    r"\b\d+[A-Za-z]?\s+[A-Za-z][A-Za-z0-9 ,]+(?:Road|Street|Avenue|Drive|Lane|"
    r"Place|Crescent|Walk|Way|Close|Rise|View|Link|Path|Terrace|Boulevard|Rd|St|"
    r"Ave|Dr|Ln|Pl|Cr)\b",
    re.IGNORECASE,
)

# ── Low-level geocoders ────────────────────────────────────────────────────────

async def _onemap(query: str) -> tuple | None:
    """Singapore government geocoder — most accurate for SG addresses."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://www.onemap.gov.sg/api/common/elastic/search", params={
                "searchVal": query, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1,
            })
            results = r.json().get("results", [])
        if results:
            return float(results[0]["LATITUDE"]), float(results[0]["LONGITUDE"])
    except Exception as e:
        logging.warning(f"OneMap error '{query}': {e}")
    return None

async def _onemap_postal(code: str) -> tuple | None:
    """Look up an exact 6-digit SG postal code on OneMap."""
    return await _onemap(code)

async def _nominatim(query: str) -> tuple | None:
    try:
        async with httpx.AsyncClient(timeout=10, headers=NOM_HEADERS) as c:
            r = await c.get("https://nominatim.openstreetmap.org/search", params={
                "q": query, "format": "json", "limit": 1, "countrycodes": "sg",
            })
            results = r.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        logging.warning(f"Nominatim error '{query}': {e}")
    return None

async def _photon(query: str) -> tuple | None:
    try:
        async with httpx.AsyncClient(timeout=10, headers=NOM_HEADERS) as c:
            r = await c.get("https://photon.komoot.io/api/", params={
                "q": query, "limit": 1,
                "lat": 1.3521, "lon": 103.8198,
                "zoom": 14,
            })
            features = r.json().get("features", [])
        if features:
            lon, lat = features[0]["geometry"]["coordinates"]
            return float(lat), float(lon)
    except Exception as e:
        logging.warning(f"Photon error '{query}': {e}")
    return None

# ── Google / DuckDuckGo search helpers ────────────────────────────────────────

async def _google_search_snippets(query: str) -> str:
    """
    Fetch the first page of DuckDuckGo HTML results for `query` and
    return all visible text (titles + snippets) as one string.
    DuckDuckGo is used because it doesn't require an API key and is
    much more permissive about scraping than Google.
    """
    try:
        url = f"https://html.duckduckgo.com/html/?q={httpx.QueryParams({'q': query})}"
        async with httpx.AsyncClient(timeout=12, headers=DDG_HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get(f"https://html.duckduckgo.com/html/",
                            params={"q": query})
        # Strip HTML tags with a simple regex — no extra lib needed
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text)
        logging.info(f"  DDG snippet length for '{query}': {len(text)}")
        return text[:4000]
    except Exception as e:
        logging.warning(f"DDG search error '{query}': {e}")
        return ""

async def _postal_from_google(name: str, address: str) -> str | None:
    """
    Search Google (via DDG) for the place name and/or street address,
    then extract the first 6-digit SG postal code found in the snippets.
    Tries two queries:
      1. "<name> Singapore"
      2. "<address> Singapore"
    """
    queries = []
    if name:
        queries.append(f"{name} Singapore restaurant address")
    if address:
        queries.append(f"{address} Singapore postal code")
    if name and address:
        queries.append(f"{name} {address} Singapore")

    for q in queries:
        text = await _google_search_snippets(q)
        match = POSTAL_RE.search(text)
        if match:
            code = match.group(1)
            # Basic sanity: SG postal codes start with 01–82
            if 1 <= int(code[:2]) <= 82:
                logging.info(f"  ✓ postal from DDG '{q}': {code}")
                return code
    return None

async def _address_from_google(name: str, address: str) -> str | None:
    """
    Search DDG for the place, then extract a street address from the snippets
    to pass back into OneMap.
    """
    query = f"{name} {address} Singapore address".strip()
    text  = await _google_search_snippets(query)
    match = SG_ADDR_RE.search(text)
    if match:
        found = match.group(0).strip()
        logging.info(f"  ✓ address from DDG '{query}': {found}")
        return found
    return None

# ── Master resolver ────────────────────────────────────────────────────────────

async def resolve_coords(name: str, address: str) -> tuple | None:
    """
    Returns (lat, lng) or None.  Tries every strategy in order.
    """
    name    = (name    or "").strip()
    address = (address or "").strip()
    logging.info(f"Resolving coords — name='{name}'  address='{address}'")

    # ── STEP 1: postal code already in the address string ─────────────────────
    m = POSTAL_RE.search(address)
    if m and 1 <= int(m.group(1)[:2]) <= 82:
        coords = await _onemap_postal(m.group(1))
        if coords:
            logging.info(f"  ✓ inline postal code {m.group(1)} → {coords}")
            return coords

    # ── STEP 2: Google-search for postal code, then pin via OneMap ────────────
    postal = await _postal_from_google(name, address)
    if postal:
        coords = await _onemap_postal(postal)
        if coords:
            logging.info(f"  ✓ Google→postal {postal} → {coords}")
            return coords

    # ── STEP 3: Google-search for street address, then pin via OneMap ─────────
    found_address = await _address_from_google(name, address)
    if found_address:
        coords = await _onemap(found_address)
        if coords:
            logging.info(f"  ✓ Google→address '{found_address}' → {coords}")
            return coords

    # ── STEP 4: OneMap text search ────────────────────────────────────────────
    for q in filter(None, [
        address and f"{address} Singapore",
        name    and f"{name} Singapore",
        name and address and f"{name} {address}",
    ]):
        coords = await _onemap(q)
        if coords:
            logging.info(f"  ✓ OneMap '{q}' → {coords}")
            return coords

    # ── STEP 5: Nominatim ─────────────────────────────────────────────────────
    for q in filter(None, [
        name and address and f"{name} {address} Singapore",
        name    and f"{name} Singapore",
    ]):
        coords = await _nominatim(q)
        if coords:
            logging.info(f"  ✓ Nominatim '{q}' → {coords}")
            return coords

    # ── STEP 6: Photon ────────────────────────────────────────────────────────
    for q in filter(None, [
        name and address and f"{name} {address} Singapore",
        name    and f"{name} Singapore",
    ]):
        coords = await _photon(q)
        if coords:
            logging.info(f"  ✓ Photon '{q}' → {coords}")
            return coords

    logging.warning(f"  ✗ All strategies failed for '{name}' / '{address}'")
    return None

def maps_link(place: dict) -> str:
    if place.get("lat"):
        return f"https://www.google.com/maps?q={place['lat']},{place['lng']}"
    return "https://www.google.com/maps/search/" + place.get("name", "").replace(" ", "+")

# ══════════════════════════════════════════════════════════════════════════════
# INSTAGRAM + PAGE FETCHING
# ══════════════════════════════════════════════════════════════════════════════

async def get_instagram_caption(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(f"https://www.instagram.com/api/v1/oembed/?url={url}")
            data = r.json()
            return f"Post by @{data.get('author_name','')}: {data.get('title','')}"
    except Exception as e:
        logging.warning(f"oEmbed failed: {e}")
        return ""

async def fetch_page(url: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15,
                headers={"User-Agent": "Mozilla/5.0"}) as c:
            return (await c.get(url)).text[:2000]
    except:
        return ""

# ══════════════════════════════════════════════════════════════════════════════
# AI EXTRACTION (Groq)
# ══════════════════════════════════════════════════════════════════════════════

async def extract_place(url: str, extra_hint: str = "") -> dict | None:
    is_instagram = bool(IG_PATTERN.search(url))
    if is_instagram:
        caption = await get_instagram_caption(url)
        context = f"Instagram post caption: {caption}" if caption else "Instagram post (caption unavailable)"
    else:
        page = await fetch_page(url)
        context = f"Page content: {page}" if page else "(page could not be loaded)"
    if extra_hint:
        context += f"\n\nUser hint: {extra_hint}"

    prompt = f"""You are a food place extractor. Extract the restaurant or food place featured.

Return ONLY a raw JSON object — no markdown, no extra text:
{{"name": "...", "address": "...", "category": "...", "note": "..."}}

Fields:
- name: restaurant / café / hawker stall name
- address: full address with street, building, postal code if available; otherwise area/district
- category: cuisine type (e.g. Japanese, Hawker, Café, Bar, Bakery)
- note: must-try dishes, price range, or opening hours if mentioned

If no food place found: {{"error": "not found"}}

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
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"})
        r.raise_for_status()

    text = r.json()["choices"][0]["message"]["content"].strip()
    if "```" in text:
        text = text.split("```")[1].lstrip("json").strip()
    s, e = text.find("{"), text.rfind("}") + 1
    if s != -1 and e:
        text = text[s:e]
    return json.loads(text)

# ══════════════════════════════════════════════════════════════════════════════
# BOT COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍜 *Food Map Bot*\n\n"
        "Paste any food place link and I'll pin it on your map!\n\n"
        "Send /help to see everything I can do.",
        parse_mode="Markdown")

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Everything this bot can do*\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🔗 *Save a place*\n"
        "Paste any link — the bot extracts the name, address, category, "
        "and pins it on your map automatically.\n"
        "Supported: Google Maps · Instagram · Burpple · Chope · "
        "TripAdvisor · any restaurant website\n\n"

        "📸 *Instagram tip*\n"
        "If the bot can't detect the place, add the name after the link:\n"
        "`https://instagram.com/reel/xxx  Odette Restaurant`\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📍 *Smart coordinate lookup*\n"
        "Tries 6 strategies in order until a pin is found:\n"
        "1️⃣ Postal code already in address (OneMap)\n"
        "2️⃣ Search online for postal code by name & address\n"
        "3️⃣ Search online for street address, then pin it\n"
        "4️⃣ OneMap text search (SG government geocoder)\n"
        "5️⃣ Nominatim (OpenStreetMap)\n"
        "6️⃣ Photon (Komoot) — last resort\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "🔁 *Duplicate check*\n"
        "Automatically warns you before saving a place that already exists.\n\n"

        "━━━━━━━━━━━━━━━━━━━\n"
        "📋 *Commands*\n"
        "/list — view all saved places\n"
        "/delete — remove a place from your map\n"
        "/edit — edit name, address, category, or note\n"
        "/help — show this message\n\n"

        f"🗺️ [Open your Food Map]({MY_MAP_URL})",
        parse_mode="Markdown")

async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    places = await read_sheet()
    if not places:
        await update.message.reply_text("No places saved yet! Paste a link to get started. 🍽️")
        return
    lines = [f"📍 *Your saved places ({len(places)} total):*\n"]
    for i, p in enumerate(places, 1):
        pin = "📌" if p.get("lat") else "⚠️"
        lines.append(f"{i}. {pin} *{p['name']}* — {p.get('address', 'no address')}")
    lines.append(f"\n🗺️ [View on map]({MY_MAP_URL})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

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
                f"{'📌' if p.get('lat') else '⚠️'} {p['name']} — {p['address'][:30]}",
                callback_data=f"delete_confirm:{p['row']}:{p['name'][:30]}"
            )] for p in places
        ] + [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
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
                f"{'📌' if p.get('lat') else '⚠️'} {p['name']} — {p['address'][:30]}",
                callback_data=f"edit_select:{p['row']}"
            )] for p in places
        ] + [[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    )

# ══════════════════════════════════════════════════════════════════════════════
# EDIT FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def handle_edit_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    row   = int(q.data.split(":")[1])
    place = ctx.user_data.get("edit_places", {}).get(row)
    if not place:
        await q.edit_message_text("Couldn't find that place. Try /edit again.")
        return
    ctx.user_data["editing_place"] = place
    coord_status = f"`{float(place['lat']):.4f}, {float(place['lng']):.4f}`" if place.get("lat") else "⚠️ no coordinates"
    await q.edit_message_text(
        f"✏️ Editing *{place['name']}*\n\n"
        f"📍 {place.get('address', '—')}\n"
        f"🏷️ {place.get('category', '—')}\n"
        f"📝 {place.get('note', '—')}\n"
        f"🌐 {coord_status}\n\n"
        "Which field do you want to change?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📛 Name",      callback_data=f"edit_field:name:{row}")],
            [InlineKeyboardButton("📍 Address",   callback_data=f"edit_field:address:{row}")],
            [InlineKeyboardButton("🏷️ Category",  callback_data=f"edit_field:category:{row}")],
            [InlineKeyboardButton("📝 Note",      callback_data=f"edit_field:note:{row}")],
            [InlineKeyboardButton("❌ Cancel",    callback_data="cancel")],
        ])
    )

async def handle_edit_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Called for every text message — checks if we're mid-edit first."""
    editing = ctx.user_data.get("editing")
    if not editing:
        await handle_message(update, ctx)
        return

    field     = editing["field"]
    row       = editing["row"]
    new_value = update.message.text.strip()
    place     = ctx.user_data.get("editing_place", {})
    place[field] = new_value

    # Re-resolve coordinates whenever name or address changes
    if field in ("name", "address"):
        resolving_msg = await update.message.reply_text("📍 Re-resolving coordinates…")
        coords = await resolve_coords(place.get("name", ""), place.get("address", ""))
        if coords:
            place["lat"], place["lng"] = str(coords[0]), str(coords[1])
            await resolving_msg.edit_text(f"📍 Coordinates updated: `{coords[0]:.4f}, {coords[1]:.4f}`", parse_mode="Markdown")
        else:
            await resolving_msg.edit_text("⚠️ Couldn't resolve new coordinates — pin position unchanged.")

    await update_sheet_row(row, place)
    ctx.user_data.pop("editing", None)
    ctx.user_data.pop("editing_place", None)

    await update.message.reply_text(
        f"✅ *{place.get('name')}* updated!\n"
        f"*{field}* is now: _{new_value}_\n\n"
        f"🗺️ [View your map]({MY_MAP_URL})",
        parse_mode="Markdown")

# ══════════════════════════════════════════════════════════════════════════════
# LINK PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

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
                "📸 Couldn't detect the food place from this Instagram post.\n\n"
                "Reply with the *restaurant name* and I'll look it up!\n"
                "Example: `Odette Restaurant Singapore`",
                parse_mode="Markdown")
        else:
            await msg.edit_text("❌ Couldn't find a food place at that link. Try another URL.")
        return

    # Resolve coordinates using all 5 strategies
    name    = place.get("name", "")
    address = place.get("address", "")
    await msg.edit_text(f"📍 Found *{name}*, looking up coordinates…", parse_mode="Markdown")

    coords = await resolve_coords(name, address)
    if coords:
        place["lat"], place["lng"] = coords
        coord_line = f"\n🌐 `{coords[0]:.4f}, {coords[1]:.4f}`"
    else:
        coord_line = "\n⚠️ _Coordinates not found — tap below to try with name only_"

    ctx.user_data["pending"] = place

    card = (
        f"🍽️ *{name}*\n"
        f"📍 {address or 'Address not found'}\n"
        f"🏷️ {place.get('category', '')}"
    )
    if place.get("note"):
        card += f"\n📝 {place['note']}"
    card += coord_line

    # Show "retry with name only" button if coords failed
    buttons = [[
        InlineKeyboardButton("✅ Save to map", callback_data="save"),
        InlineKeyboardButton("❌ Cancel",      callback_data="cancel"),
    ]]
    if not coords:
        buttons.insert(0, [InlineKeyboardButton(
            "🔍 Retry coords by name only", callback_data="retry_coords"
        )])

    await msg.edit_text(card, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(buttons))

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text or ""
    match = URL_PATTERN.search(text)

    if not match:
        # User might be replying with a restaurant name for a failed Instagram post
        if ctx.user_data.get("pending_url"):
            await process_link(update, ctx, ctx.user_data.pop("pending_url"), hint=text)
            return
        await update.message.reply_text(
            "Please send me a URL to a food place! 🔗\n"
            "Send /help to see all commands.")
        return

    url  = match.group(0)
    hint = text[match.end():].strip()
    await process_link(update, ctx, url, hint=hint)

# ══════════════════════════════════════════════════════════════════════════════
# SAVE FLOW
# ══════════════════════════════════════════════════════════════════════════════

async def do_save(q, place: dict):
    await append_to_sheet(place)
    pin_status = f"`{float(place['lat']):.4f}, {float(place['lng']):.4f}`" if place.get("lat") else "⚠️ no pin (coordinates not found)"
    await q.edit_message_text(
        f"✅ *{place.get('name')}* saved!\n\n"
        f"📍 [Open in Google Maps]({maps_link(place)})\n"
        f"🌐 {pin_status}\n"
        f"🗺️ [View your full map]({MY_MAP_URL})",
        parse_mode="Markdown")

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # ── Cancel ──
    if q.data == "cancel":
        await q.edit_message_text("Cancelled. Send another link anytime! 👋")
        return

    # ── Retry coords by name only ──
    if q.data == "retry_coords":
        place = ctx.user_data.get("pending")
        if not place:
            await q.edit_message_text("Session expired. Please send the link again.")
            return
        await q.edit_message_text(f"🔍 Searching by name: *{place.get('name')}*…", parse_mode="Markdown")
        coords = await resolve_coords(place.get("name", ""), "")  # address intentionally empty
        if coords:
            place["lat"], place["lng"] = coords
            ctx.user_data["pending"] = place
            await q.edit_message_text(
                f"📍 Found coordinates: `{coords[0]:.4f}, {coords[1]:.4f}`\n\n"
                f"Ready to save *{place.get('name')}*?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Save to map", callback_data="save"),
                    InlineKeyboardButton("❌ Cancel",      callback_data="cancel"),
                ]])
            )
        else:
            await q.edit_message_text(
                f"⚠️ Still couldn't find coordinates for *{place.get('name')}*.\n\n"
                "You can still save it — it just won't have a pin on the map.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Save anyway", callback_data="save"),
                    InlineKeyboardButton("❌ Cancel",      callback_data="cancel"),
                ]])
            )
        return

    # ── Delete ──
    if q.data.startswith("delete_confirm:"):
        _, row, name = q.data.split(":", 2)
        await delete_sheet_row(int(row))
        await q.edit_message_text(f"🗑️ *{name}* removed from your map!", parse_mode="Markdown")
        return

    # ── Edit field selected ──
    if q.data.startswith("edit_field:"):
        _, field, row = q.data.split(":")
        ctx.user_data["editing"] = {"field": field, "row": int(row)}
        labels = {"name": "name", "address": "address", "category": "category", "note": "note"}
        await q.edit_message_text(
            f"✏️ Send me the new *{labels[field]}*:",
            parse_mode="Markdown")
        return

    # ── Force save (duplicate override) ──
    if q.data == "force_save":
        place = ctx.user_data.get("pending_duplicate") or ctx.user_data.get("pending")
        if not place:
            await q.edit_message_text("Session expired. Please send the link again.")
            return
        await do_save(q, place)
        return

    # ── Save (with duplicate check) ──
    if q.data == "save":
        place = ctx.user_data.get("pending")
        if not place:
            await q.edit_message_text("Session expired. Please send the link again.")
            return
        await q.edit_message_text("⏳ Checking for duplicates…")
        existing = await read_sheet()
        if is_duplicate(place.get("name", ""), existing):
            ctx.user_data["pending_duplicate"] = place
            await q.edit_message_text(
                f"⚠️ *{place.get('name')}* looks like it's already saved!\n\nAdd it anyway?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add anyway", callback_data="force_save"),
                    InlineKeyboardButton("❌ Cancel",     callback_data="cancel"),
                ]])
            )
            return
        await do_save(q, place)

# ══════════════════════════════════════════════════════════════════════════════
# RUN  —  webhook mode for Render, polling mode for local dev
# ══════════════════════════════════════════════════════════════════════════════

PORT                     = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")

def build_app() -> Application:
    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()
    bot_app.add_handler(CommandHandler("start",  start))
    bot_app.add_handler(CommandHandler("help",   help_cmd))
    bot_app.add_handler(CommandHandler("list",   list_cmd))
    bot_app.add_handler(CommandHandler("delete", delete_cmd))
    bot_app.add_handler(CommandHandler("edit",   edit_cmd))
    bot_app.add_handler(CallbackQueryHandler(handle_edit_select, pattern="^edit_select:"))
    bot_app.add_handler(CallbackQueryHandler(handle_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_text))
    return bot_app

# ── Keep-alive: pings itself every 10 min so Render free tier stays awake ─────
async def keep_alive(hostname: str):
    url = f"https://{hostname}/"
    await asyncio.sleep(60)          # wait 1 min for server to fully start
    while True:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.get(url)
            logging.info("Keep-alive ping sent ✓")
        except Exception as e:
            logging.warning(f"Keep-alive ping failed: {e}")
        await asyncio.sleep(600)     # ping every 10 minutes

# ── Webhook server (used on Render) ───────────────────────────────────────────
async def run_webhook():
    from aiohttp import web

    bot_app = build_app()
    webhook_path   = f"/webhook/{TELEGRAM_TOKEN}"
    webhook_url    = f"https://{RENDER_EXTERNAL_HOSTNAME}{webhook_path}"

    await bot_app.initialize()
    await bot_app.bot.set_webhook(webhook_url)
    logging.info(f"Webhook set → {webhook_url}")

    async def health(request):
        return web.Response(text="OK")

    async def webhook_handler(request):
        data   = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return web.Response(text="OK")

    web_app = web.Application()
    web_app["bot_app"] = bot_app
    web_app.router.add_get("/",              health)
    web_app.router.add_post(webhook_path,    webhook_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info(f"Server listening on port {PORT}")

    # Start keep-alive loop alongside the server
    asyncio.create_task(keep_alive(RENDER_EXTERNAL_HOSTNAME))

    await asyncio.Event().wait()   # run forever

# ── Entry point ───────────────────────────────────────────────────────────────
# Render sets RENDER=true automatically — use that as the reliable signal
IS_RENDER = os.environ.get("RENDER", "").lower() == "true"

if __name__ == "__main__":
    if IS_RENDER or RENDER_EXTERNAL_HOSTNAME:
        logging.info("Starting in WEBHOOK mode (Render)")
        asyncio.run(run_webhook())
    else:
        logging.info("Starting in POLLING mode (local)")
        # Python 3.10+ safe polling using asyncio.run
        async def _poll():
            bot_app = build_app()
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling(drop_pending_updates=True)
            await asyncio.Event().wait()
        asyncio.run(_poll())
