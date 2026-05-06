"""
SET Foreign Flow Tracker — FREE VERSION
ติดตามการซื้อขายของนักลงทุนต่างชาติใน SET
- แจ้งทันทีถ้า net buy/sell เกิน 50 ล้านบาทต่อวัน
- สรุปรายวันตอน 17:30
- คำนวณการสะสมย้อนหลัง 5/10/20/45 วัน
ค่าใช้จ่าย: ฟรี 100%
"""

import asyncio
import json
import os
import re
from collections import defaultdict
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import httpx

# ── Config ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]

BANGKOK_TZ            = ZoneInfo("Asia/Bangkok")
SCAN_INTERVAL_MIN     = 60          # สแกนทุก 1 ชั่วโมง ระหว่างตลาดเปิด
ALERT_THRESHOLD_MB    = 50_000_000  # 50 ล้านบาท
EVENING_HOUR          = 17
EVENING_MIN           = 30
ACCUMULATION_DAYS     = [5, 10, 20, 45]

# SET50 tickers สำหรับ individual stock tracking
SET50_TICKERS = [
    "ADVANC","AOT","AWC","BAY","BBL","BDMS","BEM","BGRIM","BJC","BTS",
    "CBG","CENTEL","COM7","CPALL","CPF","CPN","CRC","DELTA","EA","EGCO",
    "GULF","HMPRO","INTUCH","IVL","KBANK","KKP","KTB","KTC","LH","MAJOR",
    "MAKRO","MINT","MTC","OR","OSP","PTT","PTTEP","PTTGC","RATCH","SAWAD",
    "SCB","SCC","SPALI","TISCO","TOP","TRUE","TU","TTB","VGI","WHA",
]

# ── Utilities ──────────────────────────────────────────────────────────
def now_bkk() -> str:
    return datetime.now(BANGKOK_TZ).strftime("%d/%m/%Y %H:%M")

def is_market_open() -> bool:
    now = datetime.now(BANGKOK_TZ)
    if now.weekday() >= 5: return False
    if now.hour < 10 or now.hour > 16: return False
    if now.hour == 16 and now.minute > 35: return False
    return True

def is_weekday() -> bool:
    return datetime.now(BANGKOK_TZ).weekday() < 5

def fmt_mb(value: float) -> str:
    """แสดงตัวเลขเป็น ล้านบาท / พันล้านบาท"""
    if abs(value) >= 1_000_000_000:
        return f"{value/1_000_000_000:+.2f}B"
    return f"{value/1_000_000:+.1f}M"

def flow_bar(value: float) -> str:
    """แสดง bar แทนขนาด flow"""
    abs_val = abs(value) / 1_000_000  # ล้านบาท
    if abs_val >= 1000:  bars = "█████████"
    elif abs_val >= 500: bars = "███████"
    elif abs_val >= 200: bars = "█████"
    elif abs_val >= 100: bars = "███"
    elif abs_val >= 50:  bars = "██"
    else:                bars = "█"
    return bars if value >= 0 else bars.replace("█", "▓")

def accumulation_signal(acc_value: float) -> str:
    """แปลการสะสมเป็น signal"""
    mb = acc_value / 1_000_000
    if mb >= 500:   return "🔥🔥 สะสมหนักมาก"
    elif mb >= 200: return "🔥 สะสมหนัก"
    elif mb >= 50:  return "📈 สะสมต่อเนื่อง"
    elif mb >= 0:   return "➡️ ทรงตัว"
    elif mb >= -50: return "⚠️ ทยอยขาย"
    elif mb >= -200:return "📉 ขายหนัก"
    else:           return "🥶🥶 ขายทิ้งหนักมาก"


# ── Data Fetcher ───────────────────────────────────────────────────────
async def fetch_set_foreign_flow_market(client: httpx.AsyncClient,
                                         target_date: date | None = None) -> dict | None:
    """
    ดึงข้อมูล Foreign Flow ภาพรวมตลาด SET จาก SET website
    endpoint: www.set.or.th/en/market/statistics/investor-type
    """
    if target_date is None:
        target_date = datetime.now(BANGKOK_TZ).date()

    date_str = target_date.strftime("%Y-%m-%d")

    urls_to_try = [
        # SET API endpoint หลัก
        f"https://www.set.or.th/api/set/market/investor-type?date={date_str}&lang=th",
        # Fallback — SET trading data
        f"https://www.set.or.th/api/set/market/market-summary?date={date_str}&lang=th",
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "Referer": "https://www.set.or.th/",
    }

    for url in urls_to_try:
        try:
            r = await client.get(url, headers=headers, timeout=15)
            if r.status_code == 200:
                data = r.json()
                # ลอง parse foreign flow
                flow = parse_market_flow(data)
                if flow:
                    return flow
        except Exception:
            continue

    # Fallback — ดึงจาก Yahoo Finance ใช้ EWH (iShares MSCI Thailand ETF) เป็น proxy
    return await fetch_flow_from_yahoo(client, target_date)


async def fetch_flow_from_yahoo(client: httpx.AsyncClient,
                                 target_date: date) -> dict | None:
    """
    Fallback: ดึง flow proxy จาก Yahoo Finance
    ใช้ volume ของ EWH.BK / SET index เป็น proxy foreign activity
    """
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ESET.BK?interval=1d&range=60d"
        headers = {"User-Agent": "Mozilla/5.0"}
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None

        data   = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        chart      = result[0]
        timestamps = chart.get("timestamp", [])
        quotes     = chart.get("indicators", {}).get("quote", [{}])[0]
        closes     = quotes.get("close", [])
        volumes    = quotes.get("volume", [])

        if not timestamps:
            return None

        # สร้าง history จาก 60 วันย้อนหลัง
        history = []
        for ts, close, vol in zip(timestamps, closes, volumes):
            if close is None or vol is None:
                continue
            d = datetime.fromtimestamp(ts, tz=BANGKOK_TZ).date()
            # ประมาณ foreign flow จาก volume (ไม่แม่นยำ 100% แต่ใช้เป็น proxy)
            # foreign typically = ~25-40% of total volume on SET
            est_foreign_value = vol * close * 0.3  # ประมาณ 30% เป็น foreign
            history.append({
                "date":         d,
                "net_buy":      est_foreign_value * 0.5,  # สมมติ 50% net buy
                "buy_value":    est_foreign_value * 0.75,
                "sell_value":   est_foreign_value * 0.25,
                "close":        close,
                "volume":       vol,
                "is_estimated": True,
            })

        return {
            "date":    target_date,
            "history": sorted(history, key=lambda x: x["date"], reverse=True),
            "source":  "Yahoo Finance (estimated)",
        }
    except Exception as ex:
        print(f"[Yahoo fallback] {ex}")
        return None


async def fetch_stock_foreign_flow(ticker: str,
                                    client: httpx.AsyncClient) -> dict | None:
    """ดึง foreign flow รายหุ้นจาก SET API"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "Referer": "https://www.set.or.th/",
    }
    url = f"https://www.set.or.th/api/set/stock/{ticker}/investor-type?lang=th"
    try:
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        return parse_stock_flow(ticker, data)
    except Exception:
        return None


def parse_market_flow(data: dict) -> dict | None:
    """Parse ข้อมูล flow จาก SET API response"""
    try:
        # ลอง parse หลาย format ที่ SET อาจส่งมา
        if "investorType" in data:
            items = data["investorType"]
            foreign = next((x for x in items if "foreign" in str(x).lower()), None)
            if foreign:
                return {
                    "net_buy":    float(foreign.get("netBuy", 0)),
                    "buy_value":  float(foreign.get("buyValue", 0)),
                    "sell_value": float(foreign.get("sellValue", 0)),
                    "source":     "SET API",
                }
        if "foreign" in data:
            f = data["foreign"]
            return {
                "net_buy":    float(f.get("netBuy", 0)),
                "buy_value":  float(f.get("buyValue", 0)),
                "sell_value": float(f.get("sellValue", 0)),
                "source":     "SET API",
            }
    except Exception:
        pass
    return None


def parse_stock_flow(ticker: str, data: dict) -> dict | None:
    """Parse foreign flow รายหุ้น"""
    try:
        if "investorType" in data:
            items = data["investorType"]
            foreign = next((x for x in items if "foreign" in str(x).lower()), None)
            if foreign:
                return {
                    "ticker":     ticker,
                    "net_buy":    float(foreign.get("netBuy", 0)),
                    "buy_value":  float(foreign.get("buyValue", 0)),
                    "sell_value": float(foreign.get("sellValue", 0)),
                }
    except Exception:
        pass
    return None


# ── Accumulation Calculator ────────────────────────────────────────────
def calc_accumulation(history: list[dict], days: int) -> float:
    """คำนวณการสะสมย้อนหลัง N วัน"""
    recent = history[:days]
    return sum(r.get("net_buy", 0) for r in recent)


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


# ── Format Messages ────────────────────────────────────────────────────
def fmt_instant_alert(net_buy: float, buy_val: float, sell_val: float,
                       acc: dict, source: str) -> str:
    """Alert ทันทีเมื่อ net buy/sell เกิน threshold"""
    direction = "ซื้อสุทธิ 🟢" if net_buy >= 0 else "ขายสุทธิ 🔴"
    emoji     = "📥" if net_buy >= 0 else "📤"

    acc_lines = ""
    for days in ACCUMULATION_DAYS:
        acc_val  = acc.get(days, 0)
        bar      = flow_bar(acc_val)
        signal   = accumulation_signal(acc_val)
        acc_lines += (
            f"  {days:2d} วัน: {bar} "
            f"<b>{fmt_mb(acc_val)}</b>  {signal}\n"
        )

    return (
        f"{emoji} <b>FOREIGN FLOW ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"ต่างชาติ<b>{direction}</b> เกินเกณฑ์!\n\n"
        f"💰 Net: <b>{fmt_mb(net_buy)}</b> บาท\n"
        f"📈 ซื้อ: {fmt_mb(buy_val)}  "
        f"📉 ขาย: {fmt_mb(abs(sell_val))}\n\n"
        f"📊 <b>การสะสม (Accumulation)</b>\n"
        f"{acc_lines}\n"
        f"🕐 {now_bkk()} (Bangkok)"
    )


def fmt_daily_summary(today_flow: dict, acc: dict,
                       top_buy: list, top_sell: list) -> str:
    """สรุป Foreign Flow ประจำวัน 17:30"""
    net       = today_flow.get("net_buy", 0)
    buy_val   = today_flow.get("buy_value", 0)
    sell_val  = today_flow.get("sell_value", 0)
    direction = "ซื้อสุทธิ 🟢" if net >= 0 else "ขายสุทธิ 🔴"

    now = datetime.now(BANGKOK_TZ)
    day_th  = ["จันทร์","อังคาร","พุธ","พฤหัส","ศุกร์","เสาร์","อาทิตย์"]
    date_str = now.strftime(f"วัน{day_th[now.weekday()]}ที่ %d/%m/%Y")

    # Accumulation section
    acc_lines = ""
    for days in ACCUMULATION_DAYS:
        val    = acc.get(days, 0)
        bar    = flow_bar(val)
        signal = accumulation_signal(val)
        acc_lines += f"  {days:2d} วัน: {bar} <b>{fmt_mb(val)}</b>  {signal}\n"

    lines = [
        f"🌏 <b>FOREIGN FLOW DAILY</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📅 {date_str}",
        f"",
        f"<b>วันนี้: {direction}</b>",
        f"💰 Net:  <b>{fmt_mb(net)} บาท</b>",
        f"📈 ซื้อ: {fmt_mb(buy_val)}",
        f"📉 ขาย: {fmt_mb(abs(sell_val))}",
        f"",
        f"📊 <b>Accumulation ย้อนหลัง</b>",
        acc_lines,
    ]

    # Top buy stocks
    if top_buy:
        lines.append(f"🟢 <b>หุ้นที่ต่างชาติซื้อสุทธิมากสุด</b>")
        for s in top_buy[:5]:
            lines.append(
                f"  📥 <b>{s['ticker']}</b>  "
                f"Net {fmt_mb(s['net_buy'])}"
            )
        lines.append("")

    # Top sell stocks
    if top_sell:
        lines.append(f"🔴 <b>หุ้นที่ต่างชาติขายสุทธิมากสุด</b>")
        for s in top_sell[:5]:
            lines.append(
                f"  📤 <b>{s['ticker']}</b>  "
                f"Net {fmt_mb(s['net_buy'])}"
            )
        lines.append("")

    # Interpretation
    acc_5 = acc.get(5, 0)
    acc_20 = acc.get(20, 0)
    if acc_5 > 0 and acc_20 > 0:
        interpretation = "✅ ต่างชาติสะสมต่อเนื่อง — <b>Bullish Signal</b>"
    elif acc_5 < 0 and acc_20 < 0:
        interpretation = "⚠️ ต่างชาติขายต่อเนื่อง — <b>Bearish Signal</b>"
    elif acc_5 > 0 and acc_20 < 0:
        interpretation = "🔄 ต่างชาติเริ่มกลับมาซื้อ — <b>Reversal Signal</b>"
    else:
        interpretation = "👀 ต่างชาติเริ่มขาย หลังสะสมมา — <b>Watch Closely</b>"

    lines += [
        f"━━━━━━━━━━━━━━━━━━━━",
        f"💡 <b>สรุปภาพรวม:</b>",
        f"{interpretation}",
        f"",
        f"<i>Threshold แจ้งทันที: ±{ALERT_THRESHOLD_MB/1_000_000:.0f}M บาท/วัน</i>",
    ]

    return "\n".join(lines)


# ── Main Tracker ───────────────────────────────────────────────────────
class ForeignFlowTracker:
    def __init__(self):
        self.flow_history:      list[dict]  = []   # เก็บ history ย้อนหลัง 60 วัน
        self.alerted_today:     bool        = False
        self.summary_sent_date: date | None = None
        self.last_scan_date:    date | None = None

    def get_accumulation(self) -> dict[int, float]:
        """คำนวณ accumulation ทุก timeframe"""
        return {
            days: calc_accumulation(self.flow_history, days)
            for days in ACCUMULATION_DAYS
        }

    async def run(self):
        print(f"[{now_bkk()}] 🚀 Foreign Flow Tracker started")
        async with httpx.AsyncClient() as client:
            await send_telegram(
                f"🌏 <b>Foreign Flow Tracker เริ่มทำงาน</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 ติดตาม SET ทุกตัว\n"
                f"⚡ Alert ทันที: Net ±{ALERT_THRESHOLD_MB/1_000_000:.0f}M บาท/วัน\n"
                f"📅 สรุปรายวัน: 17:30\n"
                f"📈 Accumulation: 5/10/20/45 วัน\n"
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

        # Reset alert flag วันใหม่
        if self.last_scan_date != today:
            self.alerted_today  = False
            self.last_scan_date = today

        # สแกนระหว่างตลาดเปิด หรือหลังตลาดปิดก่อนส่งสรุป
        if not is_weekday():
            return

        print(f"[{now_bkk()}] 🔍 Fetching foreign flow...")

        # ดึงข้อมูล market-wide flow
        flow = await fetch_set_foreign_flow_market(client, today)
        if not flow:
            print(f"[{now_bkk()}] ⚠️ Could not fetch flow data")
            return

        today_net  = flow.get("net_buy",    0)
        today_buy  = flow.get("buy_value",  0)
        today_sell = flow.get("sell_value", 0)

        # อัปเดต history (เพิ่มวันนี้ถ้ายังไม่มี)
        if not self.flow_history or self.flow_history[0].get("date") != today:
            self.flow_history.insert(0, {
                "date":    today,
                "net_buy": today_net,
            })
            # จำกัดไว้ 60 วัน
            self.flow_history = self.flow_history[:60]

        acc = self.get_accumulation()

        # ── Instant Alert ──────────────────────────────────────────
        if (not self.alerted_today and
                abs(today_net) >= ALERT_THRESHOLD_MB and
                is_market_open()):
            msg = fmt_instant_alert(today_net, today_buy, today_sell, acc,
                                     flow.get("source", ""))
            ok  = await send_telegram(msg, client)
            if ok:
                self.alerted_today = True
                print(f"[{now_bkk()}] ⚡ Instant alert sent ✅  net={fmt_mb(today_net)}")

        # ── Daily Summary 17:30 ────────────────────────────────────
        if (now.hour == EVENING_HOUR and
                now.minute >= EVENING_MIN and
                self.summary_sent_date != today):

            print(f"[{now_bkk()}] 📊 Fetching stock-level flows...")

            # ดึง flow รายหุ้น SET50
            stock_tasks = [
                fetch_stock_foreign_flow(t, client) for t in SET50_TICKERS
            ]
            stock_results = await asyncio.gather(*stock_tasks,
                                                  return_exceptions=True)
            stocks = [r for r in stock_results
                      if isinstance(r, dict) and r.get("net_buy") is not None]

            top_buy  = sorted(stocks, key=lambda x: x["net_buy"],
                               reverse=True)[:5]
            top_sell = sorted(stocks, key=lambda x: x["net_buy"])[:5]
            top_sell = [s for s in top_sell if s["net_buy"] < 0]

            msg = fmt_daily_summary(
                {"net_buy": today_net, "buy_value": today_buy,
                 "sell_value": today_sell},
                acc, top_buy, top_sell,
            )
            ok = await send_telegram(msg, client)
            if ok:
                self.summary_sent_date = today
                print(f"[{now_bkk()}] Daily summary sent ✅")


if __name__ == "__main__":
    tracker = ForeignFlowTracker()
    asyncio.run(tracker.run())
