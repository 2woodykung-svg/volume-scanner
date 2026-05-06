"""
SET Insider Transaction Alert + Short Selling Tracker
- Insider: แจ้งทันทีเมื่อผู้บริหารซื้อ/ขายหุ้นตัวเองเกิน 5 ล้านบาท
- Short: แจ้งทันทีเมื่อ short position เพิ่มขึ้น 50%+
- ทั้งคู่มี accumulation ย้อนหลัง 5/10/20/45 วัน
- สรุปรายวันตอน 17:45
ค่าใช้จ่าย: ฟรี 100%
"""

import asyncio
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import httpx

# ── Config ──────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID        = os.environ["TELEGRAM_CHAT_ID"]

BANGKOK_TZ              = ZoneInfo("Asia/Bangkok")
SCAN_INTERVAL_MIN       = 60
INSIDER_THRESHOLD_THB   = 5_000_000     # 5 ล้านบาท
SHORT_SURGE_THRESHOLD   = 0.50          # เพิ่มขึ้น 50%
ACCUMULATION_DAYS       = [5, 10, 20, 45]
EVENING_HOUR            = 17
EVENING_MIN             = 45

# SET50 สำหรับ short tracking
SET50_TICKERS = [
    "ADVANC","AOT","AWC","BAY","BBL","BDMS","BEM","BGRIM","BJC","BTS",
    "CBG","CENTEL","COM7","CPALL","CPF","CPN","CRC","DELTA","EA","EGCO",
    "GULF","HMPRO","INTUCH","IVL","KBANK","KKP","KTB","KTC","LH","MAJOR",
    "MAKRO","MINT","MTC","OR","OSP","PTT","PTTEP","PTTGC","RATCH","SAWAD",
    "SCB","SCC","SPALI","TISCO","TOP","TRUE","TU","TTB","VGI","WHA",
]

# ── Utilities ────────────────────────────────────────────────────────────
def now_bkk() -> str:
    return datetime.now(BANGKOK_TZ).strftime("%d/%m/%Y %H:%M")

def is_weekday() -> bool:
    return datetime.now(BANGKOK_TZ).weekday() < 5

def fmt_thb(v: float) -> str:
    if abs(v) >= 1_000_000_000: return f"{v/1_000_000_000:+.2f}B"
    return f"{v/1_000_000:+.1f}M"

def acc_bar(v: float) -> str:
    abs_v = abs(v) / 1_000_000
    n = min(int(abs_v / 10), 8) + 1
    return ("█" * n) if v >= 0 else ("▓" * n)

def acc_signal(v: float, mode: str = "insider") -> str:
    """แปล accumulation เป็น signal"""
    if mode == "insider":
        mb = v / 1_000_000
        if mb >= 50:    return "🔥🔥 ผู้บริหารสะสมหนักมาก"
        elif mb >= 20:  return "🔥 ผู้บริหารสะสมหนัก"
        elif mb >= 5:   return "📈 ผู้บริหารทยอยซื้อ"
        elif mb >= 0:   return "➡️ ทรงตัว"
        elif mb >= -5:  return "⚠️ ผู้บริหารทยอยขาย"
        elif mb >= -20: return "📉 ผู้บริหารขายหนัก"
        else:           return "🚨 ผู้บริหารขายทิ้งหนักมาก"
    else:  # short
        if v >= 200:    return "🥶🥶 Short สะสมหนักมาก — Bearish"
        elif v >= 100:  return "🥶 Short เพิ่มขึ้นต่อเนื่อง"
        elif v >= 50:   return "⚠️ Short เพิ่มขึ้น"
        elif v >= 0:    return "➡️ ทรงตัว"
        else:           return "✅ Short ลดลง — Bearish covering"


# ══════════════════════════════════════════════════════════════════════
# MODULE 1 — INSIDER TRANSACTION
# ══════════════════════════════════════════════════════════════════════

async def fetch_insider_rss(client: httpx.AsyncClient) -> list[dict]:
    """
    ดึง Form 59 (รายงานการถือครองหลักทรัพย์) จาก ก.ล.ต. RSS
    URL: https://www.sec.or.th/TH/Pages/SEC_News_Detail.aspx
    """
    urls = [
        # ก.ล.ต. RSS ข่าวการถือครองหลักทรัพย์
        "https://www.sec.or.th/TH/RssFeeds/Pages/RssFeedsDetail.aspx?RSSID=7",
        # SET news RSS — รายงาน Form 59
        "https://www.set.or.th/th/market/news-and-alert/news/rss",
        # Backup — สำนักงาน ก.ล.ต. แบบรายงาน
        "https://market.sec.or.th/public/idisc/th/Idisc/ds-form59",
    ]

    results = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    for url in urls:
        try:
            r = await client.get(url, headers=headers, timeout=12,
                                  follow_redirects=True)
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            ns   = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//item") or root.findall(".//atom:entry", ns)

            for item in items[:20]:
                title = (item.findtext("title") or
                         item.findtext("atom:title", namespaces=ns) or "").strip()
                desc  = (item.findtext("description") or
                         item.findtext("summary") or "").strip()[:500]
                link  = item.findtext("link") or ""

                # กรองเฉพาะข่าว insider / Form 59
                keywords = ["form 59", "แบบ 59", "ถือครอง", "ผู้บริหาร",
                            "กรรมการ", "ซื้อหุ้น", "ขายหุ้น", "insider"]
                if any(kw in (title + desc).lower() for kw in keywords):
                    parsed = parse_insider_transaction(title, desc)
                    if parsed:
                        parsed["link"] = link
                        results.append(parsed)
        except Exception as ex:
            print(f"[Insider RSS] {url[:50]}: {ex}")
            continue

    return results


def parse_insider_transaction(title: str, desc: str) -> dict | None:
    """
    Parse ข้อมูล insider transaction จาก title/description
    รูปแบบ: "[ชื่อบริษัท] [ชื่อผู้บริหาร] [ซื้อ/ขาย] [จำนวน] หุ้น มูลค่า [x] บาท"
    """
    text = title + " " + desc

    # หา ticker
    ticker_match = re.search(r'\b([A-Z]{2,6})\b', title)
    ticker = ticker_match.group(1) if ticker_match else None

    # หา action — ซื้อหรือขาย
    action = None
    if any(w in text for w in ["ซื้อ", "ได้มา", "acquired", "bought", "purchase"]):
        action = "BUY"
    elif any(w in text for w in ["ขาย", "จำหน่าย", "sold", "dispose", "sell"]):
        action = "SELL"
    if not action:
        return None

    # หา มูลค่า (บาท)
    value = 0.0
    value_patterns = [
        r"มูลค่า\s*([\d,]+(?:\.\d+)?)\s*(?:ล้าน)?บาท",
        r"([\d,]+(?:\.\d+)?)\s*(?:ล้าน\s*)?บาท",
        r"THB\s*([\d,]+(?:\.\d+)?)",
        r"value[:\s]+([\d,]+(?:\.\d+)?)",
    ]
    for pat in value_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val_str = m.group(1).replace(",", "")
            value = float(val_str)
            if "ล้าน" in text[max(0, m.start()-5):m.end()+5]:
                value *= 1_000_000
            break

    # หา จำนวนหุ้น
    shares = 0
    share_patterns = [
        r"([\d,]+)\s*หุ้น",
        r"([\d,]+)\s*shares",
        r"จำนวน\s*([\d,]+)",
    ]
    for pat in share_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            shares = int(m.group(1).replace(",", ""))
            break

    # หาชื่อผู้บริหาร (คร่าวๆ)
    person = ""
    person_match = re.search(
        r"(?:นาย|นาง|น\.ส\.|Mr\.|Ms\.|Mrs\.)\s*[\w\s]+", text)
    if person_match:
        person = person_match.group(0).strip()[:30]

    if not ticker and value < INSIDER_THRESHOLD_THB:
        return None

    return {
        "ticker":  ticker or "N/A",
        "action":  action,
        "value":   value,
        "shares":  shares,
        "person":  person,
        "title":   title,
        "date":    datetime.now(BANGKOK_TZ).date(),
    }


def fmt_insider_alert(tx: dict, acc: dict) -> str:
    """Format Insider Transaction Alert"""
    action_th = "ซื้อ 📥" if tx["action"] == "BUY" else "ขาย 📤"
    emoji     = "🟢" if tx["action"] == "BUY" else "🔴"

    acc_lines = ""
    for days in ACCUMULATION_DAYS:
        val    = acc.get(days, 0)
        bar    = acc_bar(val)
        signal = acc_signal(val, "insider")
        acc_lines += f"  {days:2d} วัน: {bar} <b>{fmt_thb(val)}</b>  {signal}\n"

    person_line = f"👤 {tx['person']}\n" if tx["person"] else ""
    shares_line = f"📦 จำนวน: {tx['shares']:,} หุ้น\n" if tx["shares"] else ""

    return (
        f"🔔 <b>INSIDER TRANSACTION ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>{tx['ticker']}</b> — ผู้บริหาร<b>{action_th}</b>\n\n"
        f"{person_line}"
        f"💰 มูลค่า: <b>{fmt_thb(tx['value'])} บาท</b>\n"
        f"{shares_line}\n"
        f"📊 <b>Accumulation NET (ซื้อ - ขาย)</b>\n"
        f"{acc_lines}\n"
        f"🕐 {now_bkk()} (Bangkok)\n"
        f"🔗 <a href='{tx.get('link', '')}'>ดูรายละเอียด ก.ล.ต.</a>"
    )


# ══════════════════════════════════════════════════════════════════════
# MODULE 2 — SHORT SELLING TRACKER
# ══════════════════════════════════════════════════════════════════════

async def fetch_short_data(ticker: str,
                            client: httpx.AsyncClient) -> dict | None:
    """
    ดึงข้อมูล short selling จาก Yahoo Finance
    ใช้ shortRatio และ shortPercentOfFloat เป็น proxy
    """
    url = (f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}.BK"
           f"?modules=defaultKeyStatistics,summaryDetail")
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data   = r.json()
        result = data.get("quoteSummary", {}).get("result", [])
        if not result:
            return None

        stats = result[0].get("defaultKeyStatistics", {})
        short_ratio   = stats.get("shortRatio",          {}).get("raw", 0) or 0
        short_pct     = stats.get("shortPercentOfFloat", {}).get("raw", 0) or 0
        shares_short  = stats.get("sharesShort",         {}).get("raw", 0) or 0
        shares_short_prior = stats.get("sharesShortPriorMonth", {}).get("raw", 0) or 0

        # คำนวณ % เปลี่ยนแปลง
        change_pct = 0.0
        if shares_short_prior and shares_short_prior > 0:
            change_pct = (shares_short - shares_short_prior) / shares_short_prior

        return {
            "ticker":       ticker,
            "short_ratio":  short_ratio,
            "short_pct":    short_pct * 100,      # เป็น %
            "shares_short": shares_short,
            "change_pct":   change_pct * 100,     # เป็น %
            "prior_short":  shares_short_prior,
        }
    except Exception:
        return None


async def fetch_all_short_data(client: httpx.AsyncClient) -> list[dict]:
    """ดึง short data ทุกตัวใน SET50"""
    tasks   = [fetch_short_data(t, client) for t in SET50_TICKERS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid   = [r for r in results
               if isinstance(r, dict) and r.get("short_ratio") is not None]
    return valid


def fmt_short_alert(stock: dict, acc: dict) -> str:
    """Format Short Selling Alert"""
    ticker     = stock["ticker"]
    short_pct  = stock["short_pct"]
    change_pct = stock["change_pct"]
    ratio      = stock["short_ratio"]

    acc_lines = ""
    for days in ACCUMULATION_DAYS:
        val    = acc.get(days, 0.0)
        bar    = acc_bar(val)
        signal = acc_signal(val, "short")
        acc_lines += (
            f"  {days:2d} วัน: {bar} "
            f"<b>{val:+.1f}%</b>  {signal}\n"
        )

    danger = "🚨 อันตราย" if short_pct > 20 else ("⚠️ ระวัง" if short_pct > 10 else "")

    return (
        f"🩳 <b>SHORT SURGE ALERT</b> {danger}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📉 <b>{ticker}</b> — Short เพิ่มขึ้นผิดปกติ!\n\n"
        f"🩳 Short % of Float: <b>{short_pct:.1f}%</b>\n"
        f"📈 เพิ่มขึ้น: <b>+{change_pct:.1f}%</b> จากเดือนก่อน\n"
        f"📅 Days to Cover: <b>{ratio:.1f} วัน</b>\n\n"
        f"📊 <b>Short Accumulation (% เปลี่ยนแปลงสะสม)</b>\n"
        f"{acc_lines}\n"
        f"💡 Short Ratio สูง = ต้องใช้เวลา {ratio:.0f} วันในการปิด position\n"
        f"⚡ Short squeeze อาจเกิดถ้าราคาขึ้นแรง\n"
        f"🕐 {now_bkk()} (Bangkok)"
    )


def fmt_daily_summary(insider_txs: list[dict],
                       short_surges: list[dict],
                       insider_acc_map: dict,
                       short_acc_map: dict) -> str:
    """สรุปรายวัน 17:45 รวมทั้ง Insider และ Short"""
    now     = datetime.now(BANGKOK_TZ)
    day_th  = ["จันทร์","อังคาร","พุธ","พฤหัส","ศุกร์","เสาร์","อาทิตย์"]
    date_str = now.strftime(f"วัน{day_th[now.weekday()]}ที่ %d/%m/%Y")

    lines = [
        f"📋 <b>INSIDER & SHORT DAILY</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📅 {date_str}",
        f"",
    ]

    # ── Insider Summary ──────────────────────────────────────────────
    lines.append(f"🔔 <b>Insider Transactions วันนี้</b>")
    if insider_txs:
        buys  = [t for t in insider_txs if t["action"] == "BUY"]
        sells = [t for t in insider_txs if t["action"] == "SELL"]
        total_buy  = sum(t["value"] for t in buys)
        total_sell = sum(t["value"] for t in sells)

        lines += [
            f"  📥 ซื้อ: {len(buys)} รายการ  มูลค่า {fmt_thb(total_buy)}",
            f"  📤 ขาย: {len(sells)} รายการ  มูลค่า {fmt_thb(total_sell)}",
            f"",
        ]
        for tx in sorted(insider_txs,
                          key=lambda x: x["value"], reverse=True)[:5]:
            e = "📥" if tx["action"] == "BUY" else "📤"
            lines.append(
                f"  {e} <b>{tx['ticker']}</b>  "
                f"{fmt_thb(tx['value'])}  "
                f"{'ซื้อ' if tx['action']=='BUY' else 'ขาย'}"
            )

        # Top insider accumulation
        if insider_acc_map:
            lines.append(f"")
            lines.append(f"  📊 <b>Accumulation NET สะสม (5/10/20/45 วัน)</b>")
            top_acc = sorted(insider_acc_map.items(),
                              key=lambda x: x[1].get(20, 0),
                              reverse=True)[:3]
            for ticker, acc in top_acc:
                a5  = acc.get(5, 0)
                a20 = acc.get(20, 0)
                lines.append(
                    f"  <b>{ticker}</b>  "
                    f"5d:{fmt_thb(a5)}  20d:{fmt_thb(a20)}"
                )
    else:
        lines.append("  ไม่มีรายงานวันนี้")

    lines.append(f"")

    # ── Short Summary ────────────────────────────────────────────────
    lines.append(f"🩳 <b>Short Selling วันนี้</b>")
    if short_surges:
        lines += [
            f"  เจอสัญญาณผิดปกติ <b>{len(short_surges)} ตัว</b>",
            f"",
        ]
        for s in sorted(short_surges,
                         key=lambda x: x["change_pct"],
                         reverse=True)[:5]:
            lines.append(
                f"  🩳 <b>{s['ticker']}</b>  "
                f"Short {s['short_pct']:.1f}%  "
                f"เพิ่มขึ้น +{s['change_pct']:.1f}%"
            )

        # Most shorted
        lines.append(f"")
        lines.append(f"  📊 <b>Accumulation (% short เปลี่ยนแปลงสะสม)</b>")
        top_short = sorted(short_acc_map.items(),
                            key=lambda x: x[1].get(20, 0),
                            reverse=True)[:3]
        for ticker, acc in top_short:
            a5  = acc.get(5, 0.0)
            a20 = acc.get(20, 0.0)
            lines.append(
                f"  <b>{ticker}</b>  "
                f"5d:{a5:+.1f}%  20d:{a20:+.1f}%"
            )
    else:
        lines.append("  ไม่พบสัญญาณผิดปกติวันนี้ ✅")

    lines += [
        f"",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"<i>Insider threshold: ≥{INSIDER_THRESHOLD_THB/1_000_000:.0f}M บาท</i>",
        f"<i>Short threshold: เพิ่ม ≥{SHORT_SURGE_THRESHOLD*100:.0f}%</i>",
    ]
    return "\n".join(lines)


# ── Telegram ─────────────────────────────────────────────────────────
async def send_telegram(msg: str, client: httpx.AsyncClient) -> bool:
    try:
        r = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        return r.status_code == 200
    except Exception as ex:
        print(f"[Telegram] {ex}")
        return False


# ══════════════════════════════════════════════════════════════════════
# MAIN TRACKER
# ══════════════════════════════════════════════════════════════════════
class InsiderShortTracker:
    def __init__(self):
        # Insider history: {ticker: [{"date": date, "net": float}, ...]}
        self.insider_history: dict[str, list[dict]] = defaultdict(list)
        # Short history: {ticker: [{"date": date, "change_pct": float}, ...]}
        self.short_history:   dict[str, list[dict]] = defaultdict(list)

        self.seen_insider:      set[str] = set()   # dedup
        self.alerted_short:     set[str] = set()   # ไม่ส่งซ้ำในวัน
        self.summary_sent_date: date | None = None
        self.last_date:         date | None = None

        # buffer สำหรับ daily summary
        self.today_insider: list[dict] = []
        self.today_shorts:  list[dict] = []

    def reset_if_new_day(self):
        today = datetime.now(BANGKOK_TZ).date()
        if self.last_date != today:
            self.alerted_short.clear()
            self.today_insider.clear()
            self.today_shorts.clear()
            self.last_date = today

    def get_insider_acc(self, ticker: str) -> dict[int, float]:
        hist = self.insider_history.get(ticker, [])
        return {
            d: sum(h["net"] for h in hist[:d])
            for d in ACCUMULATION_DAYS
        }

    def get_short_acc(self, ticker: str) -> dict[int, float]:
        hist = self.short_history.get(ticker, [])
        return {
            d: sum(h["change_pct"] for h in hist[:d])
            for d in ACCUMULATION_DAYS
        }

    async def run(self):
        print(f"[{now_bkk()}] 🚀 Insider & Short Tracker started")
        async with httpx.AsyncClient() as client:
            await send_telegram(
                f"🔔 <b>Insider & Short Tracker เริ่มทำงาน</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Insider Alert: มูลค่า ≥{INSIDER_THRESHOLD_THB/1_000_000:.0f}M บาท\n"
                f"🩳 Short Alert: เพิ่มขึ้น ≥{SHORT_SURGE_THRESHOLD*100:.0f}%\n"
                f"📊 Accumulation: 5/10/20/45 วัน\n"
                f"📅 สรุปรายวัน: {EVENING_HOUR:02d}:{EVENING_MIN:02d}\n"
                f"💰 ค่าใช้จ่าย: <b>ฟรี 100%</b>",
                client,
            )
            while True:
                try:
                    await self.tick(client)
                except Exception as ex:
                    print(f"[Error] {ex}")
                await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

    async def tick(self, client: httpx.AsyncClient):
        if not is_weekday():
            return

        self.reset_if_new_day()
        now   = datetime.now(BANGKOK_TZ)
        today = now.date()

        # ── Scan Insider Transactions ──────────────────────────────
        print(f"[{now_bkk()}] 🔍 Scanning insider transactions...")
        insider_txs = await fetch_insider_rss(client)

        for tx in insider_txs:
            key = f"{tx['ticker']}_{tx['action']}_{tx['value']:.0f}"
            if key in self.seen_insider:
                continue
            if tx["value"] < INSIDER_THRESHOLD_THB:
                continue

            self.seen_insider.add(key)
            self.today_insider.append(tx)

            # อัปเดต history
            net = tx["value"] if tx["action"] == "BUY" else -tx["value"]
            self.insider_history[tx["ticker"]].insert(0, {
                "date": today, "net": net
            })
            self.insider_history[tx["ticker"]] = \
                self.insider_history[tx["ticker"]][:60]

            # ส่ง alert
            acc = self.get_insider_acc(tx["ticker"])
            msg = fmt_insider_alert(tx, acc)
            ok  = await send_telegram(msg, client)
            action_th = "ซื้อ" if tx["action"] == "BUY" else "ขาย"
            print(f"  🔔 Insider {tx['ticker']} {action_th} "
                  f"{fmt_thb(tx['value'])} → {'✅' if ok else '❌'}")
            await asyncio.sleep(1)

        # ── Scan Short Selling ─────────────────────────────────────
        print(f"[{now_bkk()}] 🩳 Scanning short positions...")
        short_data = await fetch_all_short_data(client)

        for s in short_data:
            ticker     = s["ticker"]
            change_pct = s["change_pct"]

            # อัปเดต history
            self.short_history[ticker].insert(0, {
                "date": today, "change_pct": change_pct
            })
            self.short_history[ticker] = self.short_history[ticker][:60]

            # ตรวจสอบ threshold
            if (change_pct >= SHORT_SURGE_THRESHOLD * 100 and
                    ticker not in self.alerted_short):
                self.alerted_short.add(ticker)
                self.today_shorts.append(s)
                acc = self.get_short_acc(ticker)
                msg = fmt_short_alert(s, acc)
                ok  = await send_telegram(msg, client)
                print(f"  🩳 Short surge {ticker} "
                      f"+{change_pct:.1f}% → {'✅' if ok else '❌'}")
                await asyncio.sleep(1)

        # ── Daily Summary ──────────────────────────────────────────
        if (now.hour == EVENING_HOUR and
                now.minute >= EVENING_MIN and
                self.summary_sent_date != today):

            insider_acc_map = {
                tx["ticker"]: self.get_insider_acc(tx["ticker"])
                for tx in self.today_insider
            }
            short_acc_map = {
                s["ticker"]: self.get_short_acc(s["ticker"])
                for s in self.today_shorts
            }

            msg = fmt_daily_summary(
                self.today_insider, self.today_shorts,
                insider_acc_map, short_acc_map,
            )
            ok = await send_telegram(msg, client)
            if ok:
                self.summary_sent_date = today
                print(f"[{now_bkk()}] Daily summary sent ✅")


if __name__ == "__main__":
    tracker = InsiderShortTracker()
    asyncio.run(tracker.run())
