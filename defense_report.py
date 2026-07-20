#!/usr/bin/env python3
"""
BUCKEYE OPPONENT DEFENSE REPORT — standalone port of the Relay workflow
"A.I OPP. INFO GATHERER" (Relay is shutting down).

Pipeline (mirrors the Relay flow):
  1. Opponent table (same 12 teams, same queries/hashtags as Relay)
  2. Per opponent: Google News (last 24h) + optional X/Twitter (last 24h)
  3. Claude writes the scarlet-themed HTML report (your exact Relay prompt)
  4. Emails the report via Gmail

SETUP (one time):
  pip install anthropic
  Set environment variables (or hardcode below):
    ANTHROPIC_API_KEY   - required for the AI report (console.anthropic.com)
    X_BEARER_TOKEN      - optional; X API v2 token enables the tweets layer
    GMAIL_APP_PASSWORD  - optional; Gmail App Password enables email delivery
                          (Google Account > Security > 2-Step > App passwords)

RUN:  python defense_report.py
  Falls back gracefully: no X token -> news only; no Anthropic key ->
  raw link digest instead of AI-written report; no Gmail password ->
  saves HTML to disk only.

SCHEDULE:  cron (7:00 AM daily, matching Relay):
  0 7 * * * /usr/bin/python3 /path/to/defense_report.py
  Or run it in the cloud free via GitHub Actions - see defense_report.yml.
"""

import json
import os
import re
import smtplib
import ssl
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ============================================================
# CONFIG
# ============================================================

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
X_BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")          # optional
GMAIL_ADDRESS = "thomasmorton44@gmail.com"
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")  # optional
EMAIL_TO = ["thomasmorton44@gmail.com"]

CLAUDE_MODEL = "claude-sonnet-4-6"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# Opponent table — carried over from the Relay createTable step verbatim.
# "Instagram hashtag" kept for when a licensed listening tool is wired in.
OPPONENTS = [
    {"Team": "Ball State Cardinals",      "Google query": "Ball State Cardinals football defense",              "Twitter query": "Ball State football defense",     "Instagram hashtag": "ballstatefootball"},
    {"Team": "Texas Longhorns",           "Google query": "Texas Longhorns football defense",                   "Twitter query": "Texas Longhorns defense",         "Instagram hashtag": "texasfootball"},
    {"Team": "Kent State Golden Flashes", "Google query": "Kent State Golden Flashes football defense",         "Twitter query": "Kent State football defense",     "Instagram hashtag": "kentstatefootball"},
    {"Team": "Illinois Fighting Illini",  "Google query": "Illinois Fighting Illini football defense",          "Twitter query": "Illinois Illini defense",         "Instagram hashtag": "illinifootball"},
    {"Team": "Iowa Hawkeyes",             "Google query": "Iowa Hawkeyes football defense Phil Parker",         "Twitter query": "Iowa Hawkeyes defense",           "Instagram hashtag": "hawkeyefootball"},
    {"Team": "Maryland Terrapins",        "Google query": "Maryland Terrapins football defense",                "Twitter query": "Maryland Terrapins defense",      "Instagram hashtag": "marylandfootball"},
    {"Team": "Indiana Hoosiers",          "Google query": "Indiana Hoosiers football defense",                  "Twitter query": "Indiana Hoosiers defense",        "Instagram hashtag": "indianafootball"},
    {"Team": "USC Trojans",               "Google query": "USC Trojans football defense D'Anton Lynn",          "Twitter query": "USC Trojans defense",             "Instagram hashtag": "uscfb"},
    {"Team": "Oregon Ducks",              "Google query": "Oregon Ducks football defense",                      "Twitter query": "Oregon Ducks defense",            "Instagram hashtag": "oregonfootball"},
    {"Team": "Northwestern Wildcats",     "Google query": "Northwestern Wildcats football defense",             "Twitter query": "Northwestern Wildcats defense",   "Instagram hashtag": "nufbfamily"},
    {"Team": "Nebraska Cornhuskers",      "Google query": "Nebraska Cornhuskers football defense",              "Twitter query": "Nebraska Cornhuskers defense",    "Instagram hashtag": "huskerfootball"},
    {"Team": "Michigan Wolverines",       "Google query": "Michigan Wolverines football defense",               "Twitter query": "Michigan Wolverines defense",     "Instagram hashtag": "umichfootball"},
]

# The report prompt — your Relay Step 7 prompt, unchanged.
REPORT_PROMPT = """You are a college football beat reporter covering Ohio State. Produce a daily intelligence report focused specifically on the DEFENSES of each 2026 Ohio State opponent, synthesizing the last 24 hours of tweets, Instagram posts, and Google news articles in the attached data.

Prioritize: defensive coordinator news/scheme/philosophy; defensive personnel (starters, depth chart, transfers in/out, injuries, suspensions); returning vs. departing defensive production (sacks, INTs, TFLs); notable defensive performances/stats/projections; quotes from defensive coaches and players; anything Ohio State's offense would care about when gameplanning.

IMPORTANT — OUTPUT FORMAT:
Output a COMPLETE, self-contained HTML email document (no markdown, no code fences). Use ONLY inline CSS (style="..." attributes) and table-based layout so it renders reliably in email clients. Use a max content width of 600px, centered.

THEME — Ohio State football (scarlet & gray), the look their official social accounts use:
- Primary scarlet: #BB0000
- Dark gray: #666666 ; light gray background: #F4F4F4 ; borders: #DDDDDD
- Text: #1A1A1A on white content cards (#FFFFFF)
- Font stack: 'Helvetica Neue', Arial, sans-serif

STRUCTURE:
1. Outer wrapper: full-width table, background #F4F4F4, padding 24px 0.
2. Header banner: full-width scarlet (#BB0000) bar, padding 28px 24px, containing the title in WHITE, bold, UPPERCASE, letter-spacing 1px, ~24px: "BUCKEYE OPPONENT DEFENSE REPORT", and below it a smaller white/light-gray date line: {date}.
3. Content card: white (#FFFFFF) container under the banner, padding 24px, with subtle border #DDDDDD.
4. "TODAY'S TOP STORYLINES" block: a light-gray (#F4F4F4) box with a scarlet left border (4px solid #BB0000), heading in scarlet uppercase bold, then 3–5 tight bullets of the biggest defensive developments across all opponents.
5. One section per opponent that HAS meaningful defensive news:
   - Team name as an H2-style heading: scarlet (#BB0000), bold, UPPERCASE, with a 2px solid #DDDDDD bottom border under it.
   - A bold "The gist:" line (dark gray) summarizing where the defense stands.
   - 2–4 bullets of the most relevant defensive intel; each bullet ends with a source link styled as an <a> with color #BB0000, e.g. (<a href="https://example.com" style="color:#BB0000;">source</a>).
   - If a real quote exists, a blockquote: italic, with a scarlet left border (3px solid #BB0000), light-gray background, padding 8px 12px — "quote" — Name, role.
   - A "Sources:" line in small (12px) gray text with scarlet links separated by ·.
   - Separate opponent sections with a 1px solid #DDDDDD horizontal divider.
6. "DEFENSES TO WATCH" closing block (same scarlet-accented box style as Top Storylines): 2–3 opponent defenses with the most momentum, one line each.
7. For opponents with no meaningful defensive news, do NOT make a section — list them at the bottom in small gray text: "Quiet today: Team A, Team B, Team C."
8. Footer: centered small (11px) gray text: "Go Bucks — automated daily defensive intel • Sources linked inline."

RULES:
- Only use real content from the attachments. Never invent news, quotes, or links.
- For every <a> tag, the href value MUST be the raw URL only, exactly as it appears in the attachment fields, e.g. href="https://x.com/user/status/123". NEVER wrap the URL in markdown link syntax, square brackets, or parentheses inside the href. The href must contain a single clean URL and nothing else.
- Ignore non-defensive content unless it directly affects the defense (e.g., a DC hire or DL transfer).
- Keep every opponent section consistent and scannable. Tone: sharp, factual, scout-like — intel, not hype.
- Output ONLY the HTML document, nothing else.
- SUBSTANCE OVER LINKS: every bullet must state the actual information — names, numbers, scheme details, injury specifics, what was said and by whom — drawn from the "article_text" field. Write bullets a coach could act on without clicking anything. The source link is a small trailing citation, never the content itself. If all you can say about an item is its headline, leave it out.
- Summarize and synthesize in your own words; do not copy passages verbatim.
- HARD FILTER: content about an opponent's OFFENSE (QB battles, WR/RB/OL news, offensive scheme) must be EXCLUDED entirely unless it directly affects their defense (e.g., a two-way player, an offensive player moving to defense). This report covers defenses only."""

UA = {"User-Agent": "Mozilla/5.0 (compatible; DefenseReport/1.0)"}

# ============================================================
# FETCH LAYER
# ============================================================

DEFENSE_TERMS = [
    "defense", "defensive", "coordinator", "linebacker", "cornerback",
    "safety", "safeties", "edge", "defensive end", "defensive tackle",
    "defensive line", "secondary", "pass rush", "coverage", "blitz",
    "scheme", "front", "nickel", "sack", "interception", "tackle",
    "takeaway", "injury", "depth chart", "transfer",
]


def looks_defensive(*texts):
    blob = " ".join(t.lower() for t in texts if t)
    return any(term in blob for term in DEFENSE_TERMS)


def fetch_google_news(query, hours=24):
    """News via Bing News RSS — returns DIRECT publisher URLs that the
    article reader can actually open (Google News RSS wraps links in
    redirects that block scripts, which starved the report of content)."""
    url = ("https://www.bing.com/news/search?q=" + urllib.parse.quote(query)
           + "&qft=interval%3d%227%22&format=rss")  # interval 7 = last 24h
    items = []
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            root = ET.fromstring(r.read())
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            link = link.split("&ntb=")[0]  # strip tracking suffix if present
            items.append({
                "title": (item.findtext("title") or "").strip(),
                "url": link,
                "published": (item.findtext("pubDate") or "").strip(),
                "snippet": re.sub(r"<[^>]+>", "", (item.findtext("description") or "")).strip(),
                "source": urllib.parse.urlparse(link).netloc.replace("www.", ""),
            })
    except Exception as e:
        print(f"  [warn] news fetch failed ({query}): {e}")
    return items[:25]


ARTICLES_TO_READ = 6        # per opponent: how many articles to open and read fully
ARTICLE_CHAR_LIMIT = 4000   # max characters of body text kept per article


def fetch_article_text(url):
    """Follow the article link and extract readable body text so the report
    is written from the article contents, not just headlines."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""
    # strip scripts/styles/tags, favor paragraph content
    html = re.sub(r"(?is)<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>", " ", html)
    paragraphs = re.findall(r"(?is)<p[^>]*>(.*?)</p>", html)
    text = " ".join(re.sub(r"<[^>]+>", " ", p) for p in paragraphs)
    text = re.sub(r"&[a-z#0-9]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # drop boilerplate-only pages
    if len(text) < 300:
        return ""
    return text[:ARTICLE_CHAR_LIMIT]


def enrich_news_with_text(news_items):
    """Open articles, attach body text, and keep only defense-relevant items.
    Items whose article couldn't be read AND whose headline/snippet isn't
    clearly defensive are dropped entirely — no filler links in the report."""
    kept, enriched = [], 0
    for item in news_items:
        body = ""
        if enriched < ARTICLES_TO_READ:
            body = fetch_article_text(item["url"])
        if body:
            if not looks_defensive(item["title"], item["snippet"], body):
                continue  # offense/general story — drop it
            item["article_text"] = body
            enriched += 1
            kept.append(item)
        elif looks_defensive(item["title"], item["snippet"]):
            kept.append(item)  # defensive but unreadable page — keep as lead
    print(f"   articles read in full: {enriched}")
    return kept


def fetch_tweets(query, hours=24):
    """Replaces Relay's Twitter scraping step, via official X API v2 recent search.
    Requires X_BEARER_TOKEN (Basic tier or above). Skipped if not set."""
    if not X_BEARER_TOKEN:
        return []
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = urllib.parse.urlencode({
        "query": f"{query} -is:retweet lang:en",
        "start_time": start,
        "max_results": 25,
        "tweet.fields": "created_at,public_metrics,author_id",
        "expansions": "author_id",
        "user.fields": "username,name",
    })
    url = "https://api.twitter.com/2/tweets/search/recent?" + params
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {X_BEARER_TOKEN}", **UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"  [warn] X fetch failed ({query}): {e}")
        return []
    users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
    out = []
    for t in data.get("data", []):
        u = users.get(t.get("author_id"), {})
        handle = u.get("username", "unknown")
        out.append({
            "author": f"{u.get('name', '')} (@{handle})",
            "text": t.get("text", ""),
            "created_at": t.get("created_at", ""),
            "url": f"https://x.com/{handle}/status/{t['id']}",
            "metrics": t.get("public_metrics", {}),
        })
    return out


def fetch_instagram(hashtag):
    """STUB. Relay's Instagram provider has no compliant standalone equivalent —
    Meta's API only serves owned accounts, and scraping violates their ToS.
    If the department licenses a listening tool (Meltwater/Brandwatch/Sprout),
    wire its export/API here and return a list of
    {author, text, created_at, url} dicts."""
    return []

# ============================================================
# AI REPORT (replaces Relay ai.prompt.text step)
# ============================================================

def write_report_with_claude(payload, date_str):
    try:
        import anthropic
    except ImportError:
        print("[warn] anthropic package not installed - run: pip install anthropic")
        return None
    if not ANTHROPIC_API_KEY:
        print("[warn] ANTHROPIC_API_KEY not set - skipping AI report")
        return None
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = REPORT_PROMPT.replace("{date}", date_str)
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            messages=[{"role": "user", "content":
                       prompt + "\n\n=== ATTACHED DATA (last 24 hours) ===\n"
                       + json.dumps(payload, indent=1)}],
        )
    except Exception as e:
        print("=" * 60)
        print("[ERROR] Claude API call FAILED - falling back to link digest.")
        print(f"[ERROR] Reason: {e}")
        print("[ERROR] Common causes: wrong/expired API key in the")
        print("[ERROR] ANTHROPIC_API_KEY secret, or $0 credit balance")
        print("[ERROR] at console.anthropic.com > Billing.")
        print("=" * 60)
        return None
    html = "".join(b.text for b in msg.content if b.type == "text").strip()
    html = re.sub(r"^```(?:html)?|```$", "", html).strip()
    return html if html.lower().startswith(("<!doctype", "<html", "<table")) else html


def fallback_digest(payload, date_str):
    """No-AI fallback: raw grouped links so the pipeline never dies silently."""
    parts = [f"<html><body style='font-family:Arial'><h1 style='border-bottom:4px solid #BB0000'>"
             f"OPPONENT DEFENSE FEED — {date_str}</h1>"]
    for team, data in payload.items():
        parts.append(f"<h2 style='background:#BB0000;color:#fff;padding:6px 10px'>{team}</h2>")
        rows = data.get("news", []) + data.get("tweets", [])
        if not rows:
            parts.append("<p style='color:#999'><i>Nothing new.</i></p>")
        for r in rows:
            title = r.get("title") or r.get("text", "")[:140]
            parts.append(f"<p><a href='{r.get('url','')}'>{title}</a>"
                         f"<br><span style='color:#666;font-size:12px'>"
                         f"{r.get('source') or r.get('author','')} · {r.get('published') or r.get('created_at','')}</span></p>")
    parts.append("</body></html>")
    return "\n".join(parts)

# ============================================================
# EMAIL (replaces Relay gmail.sendEmail step)
# ============================================================

def send_email(subject, html_body):
    if not GMAIL_APP_PASSWORD:
        print("[info] GMAIL_APP_PASSWORD not set - skipping email, HTML saved to disk")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html_body, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_ADDRESS, EMAIL_TO, msg.as_string())
    return True

# ============================================================
# MAIN
# ============================================================

def main():
    now = datetime.now()
    date_str = now.strftime("%A, %B %-d, %Y") if os.name != "nt" else now.strftime("%A, %B %#d, %Y")

    payload = {}
    for opp in OPPONENTS:
        team = opp["Team"]
        print(f"== {team} ==")
        news = enrich_news_with_text(fetch_google_news(opp["Google query"]))
        tweets = fetch_tweets(opp["Twitter query"])
        insta = fetch_instagram(opp["Instagram hashtag"])
        payload[team] = {"news": news, "tweets": tweets, "instagram": insta}
        print(f"   news: {len(news)}  tweets: {len(tweets)}  instagram: {len(insta)}")

    html = write_report_with_claude(payload, date_str)
    ai_worked = html is not None
    if not ai_worked:
        html = fallback_digest(payload, date_str)

    out_path = os.path.join(OUTPUT_DIR, f"defense_report_{now.strftime('%Y-%m-%d')}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nReport saved: {out_path}")

    subject = f"Ohio State 2026 Opponent Defense Report — {date_str}"
    if not ai_worked:
        subject = "[AI STEP FAILED - LINKS ONLY] " + subject
    if send_email(subject, html):
        print("Email sent.")


if __name__ == "__main__":
    main()
