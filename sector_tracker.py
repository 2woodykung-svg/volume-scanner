"""
SET Sector Rotation Tracker — FREE VERSION
- รายงานเช้า 08:00 — sector outlook ก่อนตลาดเปิด
- รายงานเย็น 16:30 — สรุป sector performance ประจำวัน
- Intraday Alert — แจ้งทันทีถ้า sector ไหนพุ่งแรงผิดปกติ
ค่าใช้จ่าย: ฟรี 100%
"""

import asyncio
import os
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import httpx

# ── Config ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID     = os.environ["TELEGRAM_CHAT_ID"]

BANGKOK_TZ           = ZoneInfo("Asia/Bangkok")
SCAN_INTERVAL_MIN    = 30      # สแกน intraday ทุก 30 นาที
INTRADAY_SURGE_PCT   = 1.5     # sector ขึ้น/ลง >= 1.5% ถือว่าผิดปกติ
MORNING_HOUR         = 8
MORNING_MIN          = 0
EVENING_HOUR         = 17
EVENING_MIN          = 0

# ── SET Sectors และหุ้นตัวแทน ─────────────────────────────────────────
# ใช้หุ้น market cap ใหญ่สุดของแต่ละ sector เป็นตัวแทน
SECTORS = {
    "BANKING": {
        "name_th": "ธนาคาร 🏦",
        "tickers": ["KBANK", "SCB", "BBL", "KTB", "BAY", "TTB", "TISCO", "KKP"],
        "description": "กลุ่มธนาคารพาณิชย์",
    },
    "ENERGY": {
        "name_th": "พลังงาน ⚡",
        "tickers": ["PTT", "PTTEP", "PTTGC", "TOP", "IRPC", "BCP", "GPSC", "GULF"],
        "description": "น้ำมัน ก๊าซ พลังงานทดแทน",
    },
    "PROPERTY": {
        "name_th": "อสังหาริมทรัพย์ 🏠",
        "tickers": ["LH", "AP", "SIRI", "ORI", "PSH", "QH", "SPALI", "CPN"],
        "description": "บ้าน คอนโด ศูนย์การค้า",
    },
    "COMMERCE": {
        "name_th": "ค้าปลีก 🛒",
        "tickers": ["CPALL", "HMPRO", "BJC", "CRC", "ROBINS", "MAKRO", "COM7"],
        "description": "ร้านค้าปลีก ห้างสรรพสินค้า",
    },
    "ICT": {
        "name_th": "เทคโนโลยี/ICT 📱",
        "tickers": ["ADVANC", "TRUE", "INTUCH", "DELTA", "HANA", "KCE"],
        "description": "โทรคมนาคม อิเล็กทรอนิกส์",
        },
    "FOOD": {
        "name_th": "อาหารและเครื่องดื่ม 🍜",
        "tickers": ["CPF", "TU", "MINT", "OSP", "CBG", "OISHI", "GFPT"],
        "description": "อาหาร เครื่องดื่ม ร้านอาหาร",
    },
    "TRANSPORT": {
        "name_th": "ขนส่ง/สนามบิน ✈️",
        "tickers": ["AOT", "BTS", "BEM", "AAV", "BA"],
        "description": "สนามบิน รถไฟฟ้า สายการบิน",
    },
    "HEALTHCARE": {
        "name_th": "สุขภาพ/รพ. 🏥",
        "tickers": ["BDMS", "BH", "CHG", "BCH", "RJH", "VIBHA"],
        "description": "โรงพยาบาล เวชภัณฑ์",
    },
    "MATERIALS": {
        "name_th": "วัสดุก่อสร้าง 🏗️",
        "tickers": ["SCC", "SCCC", "TOA", "DCC", "TASCO"],
        "description": "ปูนซีเมนต์ เหล็ก วัสดุ",
    },
    "FINANCE": {
        "name_th": "การเงินนอกธนาคาร 💳",
        "tickers": ["MTC", "SAWAD", "TIDLOR", "JMT", "AEONTS", "KTC"],
        "description": "สินเชื่อ ลีสซิ่ง บัตรเครดิต",
    },
}

# ── Utilities ──────────────────────────────────────────────────────────
def now_bkk() -> str:
    return datetime.now(BANGKOK_TZ).strftime("%d/%m/%Y %H:%M")

def is_market_open() -> bool:
    now = datetime.now(BANGKOK_TZ)
    if now.weekday() >= 5:
        return False
    if now.hour < 10 or now.hour > 16:
        return False
    if now.hour == 16 and now.minute > 30:
        return False
    return True

def is_weekday() -> bool:
    return datetime.now(BANGKOK_TZ).weekday() < 5

def performance_bar(pct: float) -> str:
    """แสดง bar กราฟจาก % change"""
    if pct >= 2.0:   return "████████ 🔥"
    elif pct >= 1.0: return "██████"
    elif pct >= 0.5: return "████"
    elif pct >= 0.0: return "██"
    elif pct >= -0.5:return "▓▓"
    elif pct >= -1.0:return "▓▓▓▓"
    elif pct >= -2.0:return "▓▓▓▓▓▓"
    else:             return "▓▓▓▓▓▓▓▓ 🥶"

def rank_emoji(rank: int) -> str:
    return ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"][rank] if rank < 10 else f"{rank+1}."


# ── Yahoo Finance Fetcher ──────────────────────────────────────────────
async def fetch_ticker(symbol_bk: str, client: httpx.AsyncClient) -> dict | None:
    """ดึงราคาและ % change จาก Yahoo Finance"""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol_bk}"
        f"?interval=1d&range=5d"
    )
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None
        meta   = result[0].get("meta", {})
        price  = meta.get("regularMarketPrice")
        prev   = meta.get("chartPreviousClose") or meta.get("previousClose")
        volume = meta.get("regularMarketVolume", 0)
        if not price or not prev or prev == 0:
            return None
        change_pct = (price - prev) / prev * 100
        return {
            "ticker":     symbol_bk.replace(".BK", ""),
            "price":      price,
            "change_pct": change_pct,
            "volume":     volume,
        }
    except Exception:
        return None


async def fetch_sector_data(client: httpx.AsyncClient) -> dict[str, dict]:
    """ดึงข้อมูลทุก sector พร้อมกัน"""
    sector_results = {}

    for sector_key, sector_info in SECTORS.items():
        tickers = sector_info["tickers"]
        tasks   = [fetch_ticker(f"{t}.BK", client) for t in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid = [r for r in results if isinstance(r, dict)]
        if not valid:
            continue

        # คำนวณ sector performance = ค่าเฉลี่ย % change ของหุ้นในกลุ่ม
        avg_change = sum(r["change_pct"] for r in valid) / len(valid)

        # หาหุ้นที่ขึ้น/ลงมากที่สุดใน sector
        top_stock = max(valid, key=lambda x: x["change_pct"])
        bot_stock = min(valid, key=lambda x: x["change_pct"])

        sector_results[sector_key] = {
            "name_th":    sector_info["name_th"],
            "avg_change": avg_change,
            "stocks":     valid,
            "top_stock":  top_stock,
            "bot_stock":  bot_stock,
            "count":      len(valid),
        }

        await asyncio.sleep(0.5)  # หน่วงเล็กน้อยป้องกัน rate limit

    return sector_results


# ── Format Messages ────────────────────────────────────────────────────
async def fetch_set_index(client: httpx.AsyncClient) -> dict | None:
    """ดึงข้อมูล SET Index จาก Yahoo Finance"""
    result = await fetch_ticker("^SET.BK", client)
    if not result:
        # fallback ใช้ SET50 ETF แทน
        result = await fetch_ticker("TDEX.BK", client)
    return result


async def fetch_top_movers(client: httpx.AsyncClient) -> dict:
    """หาหุ้นที่ขึ้น/ลงมากสุดใน SET วันนี้"""
    # รวบรวมหุ้นทุกตัวจากทุก sector
    all_tickers = []
    for s in SECTORS.values():
        all_tickers.extend(s["tickers"])
    all_tickers = list(set(all_tickers))  # dedup

    tasks   = [fetch_ticker(f"{t}.BK", client) for t in all_tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid   = [r for r in results if isinstance(r, dict)]

    if not valid:
        return {"gainers": [], "losers": [], "most_active": []}

    sorted_by_change = sorted(valid, key=lambda x: x["change_pct"], reverse=True)
    sorted_by_volume = sorted(valid, key=lambda x: x["volume"],     reverse=True)

    return {
        "gainers":     sorted_by_change[:5],
        "losers":      sorted_by_change[-5:][::-1],
        "most_active": sorted_by_volume[:5],
    }


def fmt_volume(v: int) -> str:
    if v >= 1_000_000_000: return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:     return f"{v/1_000_000:.0f}M"
    if v >= 1_000:         return f"{v/1_000:.0f}K"
    return str(v)



    """Morning Briefing 08:00 — แนวโน้มก่อนตลาดเปิด"""
    now = datetime.now(BANGKOK_TZ)
    day_th = ["จันทร์","อังคาร","พุธ","พฤหัส","ศุกร์","เสาร์","อาทิตย์"]
    weekday = day_th[now.weekday()]
    date_str = now.strftime(f"วัน{weekday}ที่ %d/%m/%Y")

    # เรียงตาม performance
    sorted_sectors = sorted(
        sector_data.items(),
        key=lambda x: x[1]["avg_change"],
        reverse=True
    )

    lines = [
        f"🌅 <b>SECTOR OUTLOOK</b> — 08:00",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📅 {date_str}",
        f"",
        f"📊 <b>Sector Performance เมื่อวาน</b>",
        f"<i>(ใช้เป็นแนวทางวันนี้)</i>",
        f"",
    ]

    for rank, (key, s) in enumerate(sorted_sectors):
        chg  = s["avg_change"]
        sign = "+" if chg >= 0 else ""
        bar  = performance_bar(chg)
        lines.append(
            f"{rank_emoji(rank)} {s['name_th']}\n"
            f"   {bar}  <b>{sign}{chg:.2f}%</b>"
        )

    # แนะนำ sector ที่น่าสนใจ
    top3    = sorted_sectors[:3]
    bottom3 = sorted_sectors[-3:]

    lines += [
        f"",
        f"🔥 <b>Sector ที่ควรจับตาวันนี้:</b>",
    ]
    for _, s in top3:
        top = s["top_stock"]
        lines.append(f"  • {s['name_th']} นำโดย {top['ticker']} ({'+' if top['change_pct']>=0 else ''}{top['change_pct']:.1f}%)")

    lines += [
        f"",
        f"🥶 <b>Sector ที่อ่อนแอ:</b>",
    ]
    for _, s in bottom3:
        bot = s["bot_stock"]
        lines.append(f"  • {s['name_th']} ({'+' if s['avg_change']>=0 else ''}{s['avg_change']:.1f}%)")

    lines += [
        f"",
        f"<i>⚡ Scanner ทำงานทุก {SCAN_INTERVAL_MIN} นาที ระหว่างตลาดเปิด</i>",
    ]
    return "\n".join(lines)


def fmt_evening_report(sector_data: dict,
                        set_index: dict | None,
                        movers: dict) -> str:
    """Evening Summary 17:00 — สรุปปิดตลาดครบทุกมิติ"""
    sorted_sectors = sorted(
        sector_data.items(),
        key=lambda x: x[1]["avg_change"],
        reverse=True,
    )
    winner = sorted_sectors[0]
    loser  = sorted_sectors[-1]
    breadth_up   = sum(1 for _, s in sorted_sectors if s["avg_change"] > 0)
    breadth_down = len(sorted_sectors) - breadth_up

    now = datetime.now(BANGKOK_TZ)
    day_th  = ["จันทร์","อังคาร","พุธ","พฤหัส","ศุกร์","เสาร์","อาทิตย์"]
    weekday = day_th[now.weekday()]
    date_str = now.strftime(f"วัน{weekday}ที่ %d/%m/%Y")

    lines = [
        f"📊 <b>MARKET CLOSE SUMMARY</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📅 {date_str} | 17:00 Bangkok",
        f"",
    ]

    # ── SET Index ──────────────────────────────────────────────────
    if set_index:
        idx_chg  = set_index["change_pct"]
        idx_sign = "+" if idx_chg >= 0 else ""
        idx_dir  = "📈" if idx_chg >= 0 else "📉"
        lines += [
            f"🇹🇭 <b>SET Index</b>",
            f"   {idx_dir} {set_index['price']:.2f} pts  "
            f"<b>{idx_sign}{idx_chg:.2f}%</b>",
            f"   Vol: {fmt_volume(set_index['volume'])} หุ้น",
            f"",
        ]

    # ── Sector Ranking ─────────────────────────────────────────────
    lines.append(f"🔄 <b>Sector Ranking วันนี้</b>")
    for rank, (key, s) in enumerate(sorted_sectors):
        chg  = s["avg_change"]
        sign = "+" if chg >= 0 else ""
        bar  = performance_bar(chg)
        top  = s["top_stock"] if chg >= 0 else s["bot_stock"]
        lines.append(
            f"{rank_emoji(rank)} {s['name_th']}\n"
            f"   {bar} <b>{sign}{chg:.2f}%</b>  "
            f"{'▲' if chg>=0 else '▼'}{top['ticker']} "
            f"{'+' if top['change_pct']>=0 else ''}{top['change_pct']:.1f}%"
        )

    lines.append(f"")

    # ── Top Gainers ────────────────────────────────────────────────
    if movers.get("gainers"):
        lines.append(f"🚀 <b>Top Gainers</b>")
        for s in movers["gainers"]:
            lines.append(
                f"   📈 <b>{s['ticker']}</b>  "
                f"+{s['change_pct']:.2f}%  "
                f"Vol {fmt_volume(s['volume'])}"
            )
        lines.append(f"")

    # ── Top Losers ─────────────────────────────────────────────────
    if movers.get("losers"):
        lines.append(f"💥 <b>Top Losers</b>")
        for s in movers["losers"]:
            lines.append(
                f"   📉 <b>{s['ticker']}</b>  "
                f"{s['change_pct']:.2f}%  "
                f"Vol {fmt_volume(s['volume'])}"
            )
        lines.append(f"")

    # ── Most Active ────────────────────────────────────────────────
    if movers.get("most_active"):
        lines.append(f"🔥 <b>Most Active (Volume)</b>")
        for s in movers["most_active"]:
            sign = "+" if s["change_pct"] >= 0 else ""
            lines.append(
                f"   📊 <b>{s['ticker']}</b>  "
                f"Vol {fmt_volume(s['volume'])}  "
                f"{sign}{s['change_pct']:.1f}%"
            )
        lines.append(f"")

    # ── Market Summary ─────────────────────────────────────────────
    market_mood = "🟢 ตลาดบวก" if breadth_up > breadth_down else "🔴 ตลาดลบ"
    lines += [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🏆 Sector แชมป์: {winner[1]['name_th']} "
        f"(+{winner[1]['avg_change']:.2f}%)",
        f"📉 Sector อ่อน: {loser[1]['name_th']} "
        f"({loser[1]['avg_change']:.2f}%)",
        f"",
        f"📈 Sector ขึ้น {breadth_up} | "
        f"📉 ลง {breadth_down}  →  {market_mood}",
    ]
    return "\n".join(lines)


def fmt_intraday_alert(sector_key: str, sector_data: dict,
                       prev_change: float) -> str:
    """Intraday Alert เมื่อ sector พุ่งแรงผิดปกติ"""
    s          = sector_data
    chg        = s["avg_change"]
    delta      = chg - prev_change
    sign       = "+" if chg >= 0 else ""
    delta_sign = "+" if delta >= 0 else ""
    direction  = "พุ่งขึ้น 🚀" if delta > 0 else "ร่วงลง 💥"

    top = s["top_stock"] if delta > 0 else s["bot_stock"]

    return (
        f"⚡ <b>SECTOR INTRADAY ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{s['name_th']} <b>{direction}</b>\n\n"
        f"Performance: <b>{sign}{chg:.2f}%</b>  "
        f"(เปลี่ยน {delta_sign}{delta:.2f}% ใน {SCAN_INTERVAL_MIN} นาที)\n\n"
        f"หุ้นนำ: <b>{top['ticker']}</b>  "
        f"{'+' if top['change_pct']>=0 else ''}{top['change_pct']:.1f}%\n"
        f"🕐 {now_bkk()} (Bangkok)"
    )


# ── Telegram ───────────────────────────────────────────────────────────
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


# ── Main Tracker ───────────────────────────────────────────────────────
class SectorRotationTracker:
    def __init__(self):
        self.prev_sector_changes: dict[str, float] = {}
        self.morning_sent_date: date | None  = None
        self.evening_sent_date: date | None  = None

    async def run(self):
        print(f"[{now_bkk()}] 🚀 Sector Rotation Tracker started")
        async with httpx.AsyncClient() as client:
            await send_telegram(
                f"🔄 <b>Sector Rotation Tracker เริ่มทำงาน</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 ติดตาม {len(SECTORS)} sectors ใน SET\n"
                f"🌅 Morning Report: 08:00\n"
                f"🌆 Evening Report: 16:35\n"
                f"⚡ Intraday Alert: เมื่อ sector ขึ้น/ลง ≥{INTRADAY_SURGE_PCT}%\n"
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
        now   = datetime.now(BANGKOK_TZ)
        today = now.date()

        # Morning Report 08:00
        if (is_weekday() and
                now.hour == MORNING_HOUR and now.minute < SCAN_INTERVAL_MIN and
                self.morning_sent_date != today):
            print(f"[{now_bkk()}] 🌅 Morning report...")
            data = await fetch_sector_data(client)
            if data:
                msg = fmt_morning_report(data)
                ok  = await send_telegram(msg, client)
                if ok:
                    self.morning_sent_date = today
                    print(f"[{now_bkk()}] Morning sent ✅")

        # Intraday Alert (ช่วงตลาดเปิด)
        elif is_market_open():
            print(f"[{now_bkk()}] 🔍 Intraday scan...")
            data = await fetch_sector_data(client)
            if not data:
                return

            for sector_key, s in data.items():
                chg  = s["avg_change"]
                prev = self.prev_sector_changes.get(sector_key, chg)
                delta = abs(chg - prev)

                if delta >= INTRADAY_SURGE_PCT:
                    msg = fmt_intraday_alert(sector_key, s, prev)
                    ok  = await send_telegram(msg, client)
                    print(f"  ⚡ {sector_key} delta={delta:.1f}% → {'✅' if ok else '❌'}")
                    await asyncio.sleep(1)

                self.prev_sector_changes[sector_key] = chg

        # Evening Report 17:00
        if (is_weekday() and
                now.hour == EVENING_HOUR and now.minute < SCAN_INTERVAL_MIN and
                self.evening_sent_date != today):
            print(f"[{now_bkk()}] 🌆 Evening report...")
            data      = await fetch_sector_data(client)
            set_idx   = await fetch_set_index(client)
            movers    = await fetch_top_movers(client)
            if data:
                msg = fmt_evening_report(data, set_idx, movers)
                ok  = await send_telegram(msg, client)
                if ok:
                    self.evening_sent_date = today
                    print(f"[{now_bkk()}] Evening sent ✅")


if __name__ == "__main__":
    tracker = SectorRotationTracker()
    asyncio.run(tracker.run())
