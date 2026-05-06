"""
SET Volume Surge Alert
สแกนหุ้นไทยทุกตัวใน SET ที่ราคาขึ้น + Volume สูงกว่าค่าเฉลี่ย 3x
ทำงานเฉพาะช่วงตลาดเปิด 10:00-16:30 Bangkok
ค่าใช้จ่าย: ฟรี 100% (Yahoo Finance API)
"""

import asyncio
import os
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import httpx

# ── Config ────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

BANGKOK_TZ          = ZoneInfo("Asia/Bangkok")
SCAN_INTERVAL_MIN   = 15       # สแกนทุก 15 นาที
VOLUME_THRESHOLD    = 3.0      # Volume สูงกว่าค่าเฉลี่ย 3x
MIN_PRICE_CHANGE    = 0.5      # ราคาขึ้นอย่างน้อย 0.5%
MARKET_OPEN_HOUR    = 10
MARKET_CLOSE_HOUR   = 16
MARKET_CLOSE_MIN    = 30

# ── รายชื่อหุ้น SET (Yahoo Finance ใช้ suffix .BK) ───────────────────
# SET50 + หุ้นยอดนิยม รวม ~120 ตัว
SET_TICKERS = [
    # SET50 หลัก
    "ADVANC","AEONTS","AOT","AWC","BANPU","BAY","BBL","BDMS","BEC","BEM",
    "BGRIM","BH","BJC","BLA","BPP","BTS","CBG","CENTEL","CHG","CK",
    "CKP","COM7","CPALL","CPF","CPN","CRC","DELTA","DTAC","EA","EGCO",
    "ERW","ESSO","FORTH","GFPT","GLOBAL","GPSC","GULF","GUNKUL","HANA","HMPRO",
    "ICHI","INTUCH","IRPC","ITD","IVL","JASIF","JMT","KBANK","KCE","KKP",
    "KTB","KTC","LH","LHFG","MAJOR","MAKRO","MC","MINT","MTC","NETBAY",
    "NRF","NSL","NWR","OISHI","OR","OSP","ORI","PAP","PLANB","PRM",
    "PSH","PSL","PTT","PTTEP","PTTGC","QH","RASSET","RATCH","RBF","RS",
    "S","SAK","SAWAD","SCB","SCC","SCCC","SGP","SHR","SINGER","SIRI",
    "SPALI","SPRC","SSP","STEC","STA","SUPER","SYNTEC","TASCO","TCAP","THAI",
    "THANI","THCOM","TISCO","TKN","TMB","TNITY","TOA","TOP","TQM","TRUE",
    "TTA","TTB","TTW","TU","TVO","UV","VGI","VIBHA","WHAUP","WHA",
    # หุ้นยอดนิยมเพิ่มเติม
    "3K-BAT","ACE","AMATA","AP","BEAUTY","BIG","CHAYO","CH","CIMBT",
    "COCOCO","DIF","DOHOME","EASTW","FPT","GLAND","HUMAN","ICC","ILINK",
    "JAS","KAMART","LPN","MFEC","NGPF","OCC","PDJ","PE","PG",
    "PRINC","PTG","SAPPE","SEAFCO","SKY","SNNP","SPC","SSF",
    "TPIPP","TRT","TSTH","TVI","UPOIC","VIH","WICE","WIIK",
]

# ── Utilities ─────────────────────────────────────────────────────────
def now_bkk() -> str:
    return datetime.now(BANGKOK_TZ).strftime("%d/%m/%Y %H:%M")

def is_market_open() -> bool:
    """เช็คว่าตลาดหุ้นไทยเปิดอยู่ไหม (จันทร์-ศุกร์ 10:00-16:30)"""
    now = datetime.now(BANGKOK_TZ)
    if now.weekday() >= 5:  # เสาร์-อาทิตย์
        return False
    if now.hour < MARKET_OPEN_HOUR:
        return False
    if now.hour > MARKET_CLOSE_HOUR:
        return False
    if now.hour == MARKET_CLOSE_HOUR and now.minute > MARKET_CLOSE_MIN:
        return False
    return True


# ── Yahoo Finance Data Fetcher ────────────────────────────────────────
async def fetch_stock_data(ticker: str, client: httpx.AsyncClient) -> dict | None:
    """
    ดึงข้อมูลราคาและ volume จาก Yahoo Finance
    ticker ต้องใช้ suffix .BK สำหรับหุ้นไทย เช่น PTT.BK
    """
    symbol = f"{ticker}.BK"
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=30d"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    try:
        r = await client.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        result = data.get("chart", {}).get("result", [])
        if not result:
            return None

        chart = result[0]
        meta  = chart.get("meta", {})
        indicators = chart.get("indicators", {})
        quote = indicators.get("quote", [{}])[0]

        closes  = quote.get("close", [])
        volumes = quote.get("volume", [])

        # กรอง None ออก
        closes  = [c for c in closes  if c is not None]
        volumes = [v for v in volumes if v is not None]

        if len(closes) < 21 or len(volumes) < 21:
            return None

        current_price  = meta.get("regularMarketPrice") or closes[-1]
        prev_close     = closes[-2] if len(closes) >= 2 else closes[-1]
        current_volume = meta.get("regularMarketVolume") or volumes[-1]

        # คำนวณค่าเฉลี่ย volume ย้อนหลัง 5, 10, 20 วัน
        vol_5  = sum(volumes[-6:-1]) / 5   if len(volumes) >= 6  else None
        vol_10 = sum(volumes[-11:-1]) / 10 if len(volumes) >= 11 else None
        vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else None

        price_change_pct = ((current_price - prev_close) / prev_close * 100
                            if prev_close else 0)

        return {
            "ticker":        ticker,
            "price":         current_price,
            "prev_close":    prev_close,
            "change_pct":    price_change_pct,
            "volume":        current_volume,
            "vol_avg_5":     vol_5,
            "vol_avg_10":    vol_10,
            "vol_avg_20":    vol_20,
            "currency":      meta.get("currency", "THB"),
        }
    except Exception as ex:
        return None


def analyze_volume(data: dict) -> dict | None:
    """
    วิเคราะห์ว่าผ่านเกณฑ์ Volume Surge ไหม
    ต้องผ่านทั้ง: ราคาขึ้น >= 0.5% และ Volume >= 3x ค่าเฉลี่ยอย่างน้อย 1 timeframe
    """
    if data["change_pct"] < MIN_PRICE_CHANGE:
        return None

    vol     = data["volume"]
    v5      = data["vol_avg_5"]
    v10     = data["vol_avg_10"]
    v20     = data["vol_avg_20"]

    ratio_5  = vol / v5  if v5  and v5  > 0 else 0
    ratio_10 = vol / v10 if v10 and v10 > 0 else 0
    ratio_20 = vol / v20 if v20 and v20 > 0 else 0

    # ต้องผ่านเกณฑ์ 3x อย่างน้อย 1 timeframe
    passes = [r >= VOLUME_THRESHOLD for r in [ratio_5, ratio_10, ratio_20]]
    if not any(passes):
        return None

    # คำนวณ signal strength
    max_ratio = max(ratio_5, ratio_10, ratio_20)
    if max_ratio >= 5:
        strength = "🔥🔥🔥 EXTREME"
    elif max_ratio >= 4:
        strength = "🔥🔥 VERY STRONG"
    else:
        strength = "🔥 STRONG"

    return {
        "ratio_5":  ratio_5,
        "ratio_10": ratio_10,
        "ratio_20": ratio_20,
        "strength": strength,
        "max_ratio": max_ratio,
    }


# ── Telegram ──────────────────────────────────────────────────────────
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


def fmt_volume_alert(data: dict, vol_analysis: dict) -> str:
    """Format Volume Surge Alert message"""
    ticker     = data["ticker"]
    price      = data["price"]
    change_pct = data["change_pct"]
    volume     = data["volume"]
    strength   = vol_analysis["strength"]
    r5         = vol_analysis["ratio_5"]
    r10        = vol_analysis["ratio_10"]
    r20        = vol_analysis["ratio_20"]

    # Format volume ให้อ่านง่าย
    def fmt_vol(v):
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000:     return f"{v/1_000:.0f}K"
        return str(int(v))

    # สร้าง ratio bar
    def ratio_bar(r):
        if r == 0: return "N/A"
        stars = "█" * min(int(r), 6)
        return f"{stars} {r:.1f}x"

    return (
        f"📈 <b>VOLUME SURGE ALERT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷️ <b>{ticker}</b> ({strength})\n\n"
        f"💰 ราคา: <b>{price:.2f} บาท</b>  "
        f"📈 +{change_pct:.2f}%\n"
        f"📊 Volume วันนี้: <b>{fmt_vol(volume)} หุ้น</b>\n\n"
        f"<b>เทียบค่าเฉลี่ย:</b>\n"
        f"  5  วัน: {ratio_bar(r5)}\n"
        f"  10 วัน: {ratio_bar(r10)}\n"
        f"  20 วัน: {ratio_bar(r20)}\n\n"
        f"🕐 {now_bkk()} (Bangkok)\n"
        f"🔗 <a href='https://finance.yahoo.com/quote/{ticker}.BK'>Yahoo Finance</a>"
        f" | <a href='https://www.tradingview.com/chart/?symbol=SET:{ticker}'>TradingView</a>"
    )


def fmt_scan_summary(alerts: list[dict]) -> str:
    """สรุปผลการสแกนรอบนี้ (ถ้าเจอหลายตัว)"""
    if not alerts:
        return ""
    lines = [
        f"📊 <b>VOLUME SCAN SUMMARY</b> — {now_bkk()}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"เจอสัญญาณ <b>{len(alerts)} ตัว</b>\n",
    ]
    # เรียงตาม max_ratio สูงสุดก่อน
    sorted_alerts = sorted(alerts, key=lambda x: x["vol"]["max_ratio"], reverse=True)
    for item in sorted_alerts[:10]:
        d  = item["data"]
        va = item["vol"]
        lines.append(
            f"• <b>{d['ticker']}</b>  "
            f"+{d['change_pct']:.1f}%  "
            f"Vol {va['max_ratio']:.1f}x  "
            f"{va['strength'].split()[0]}"
        )
    return "\n".join(lines)


# ── Main Scanner ──────────────────────────────────────────────────────
class VolumeSurgeScanner:
    def __init__(self):
        self.alerted_today: set[str] = set()   # ไม่ส่งซ้ำในวันเดียวกัน
        self.last_alert_date: date | None = None

    def reset_if_new_day(self):
        today = datetime.now(BANGKOK_TZ).date()
        if self.last_alert_date != today:
            self.alerted_today.clear()
            self.last_alert_date = today

    async def run(self):
        print(f"[{now_bkk()}] 🚀 Volume Surge Scanner started")
        async with httpx.AsyncClient() as client:
            await send_telegram(
                f"📈 <b>Volume Surge Scanner เริ่มทำงาน</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔍 สแกนหุ้น SET ทั้งหมด {len(SET_TICKERS)} ตัว\n"
                f"⚡ เกณฑ์: ราคาขึ้น ≥{MIN_PRICE_CHANGE}% + Volume ≥{VOLUME_THRESHOLD:.0f}x\n"
                f"📊 เปรียบเทียบ: 5 / 10 / 20 วัน\n"
                f"🕐 สแกนทุก {SCAN_INTERVAL_MIN} นาที (ช่วงตลาดเปิด)\n"
                f"💰 ค่าใช้จ่าย: <b>ฟรี 100%</b>",
                client,
            )
            while True:
                try:
                    if is_market_open():
                        await self.scan(client)
                    else:
                        now = datetime.now(BANGKOK_TZ)
                        print(f"[{now_bkk()}] 💤 ตลาดปิด — รอ...")
                except Exception as ex:
                    print(f"[Error] {ex}")
                await asyncio.sleep(SCAN_INTERVAL_MIN * 60)

    async def scan(self, client: httpx.AsyncClient):
        self.reset_if_new_day()
        print(f"[{now_bkk()}] 🔍 Scanning {len(SET_TICKERS)} stocks...")

        # ดึงข้อมูลทีละ batch 20 ตัว เพื่อไม่ให้ rate limit
        alerts = []
        for i in range(0, len(SET_TICKERS), 20):
            batch = SET_TICKERS[i:i+20]
            tasks = [fetch_stock_data(t, client) for t in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for data in results:
                if not isinstance(data, dict):
                    continue
                vol_analysis = analyze_volume(data)
                if not vol_analysis:
                    continue

                ticker = data["ticker"]
                # ไม่ส่งซ้ำในวันเดียวกัน (แต่ถ้า ratio สูงขึ้นอีกให้ส่งได้)
                alert_key = f"{ticker}_{int(vol_analysis['max_ratio'])}"
                if alert_key in self.alerted_today:
                    continue

                self.alerted_today.add(alert_key)
                alerts.append({"data": data, "vol": vol_analysis})
                print(
                    f"  🔥 {ticker} "
                    f"+{data['change_pct']:.1f}% "
                    f"Vol {vol_analysis['max_ratio']:.1f}x"
                )

            # หน่วงเล็กน้อยระหว่าง batch ป้องกัน rate limit
            await asyncio.sleep(1)

        print(f"[{now_bkk()}] เจอสัญญาณ {len(alerts)} ตัว")

        if not alerts:
            return

        # ถ้าเจอ 1-3 ตัว → ส่งแยกทีละตัว (รายละเอียดเต็ม)
        if len(alerts) <= 3:
            for item in alerts:
                msg = fmt_volume_alert(item["data"], item["vol"])
                ok  = await send_telegram(msg, client)
                print(f"  → {item['data']['ticker']} Telegram {'✅' if ok else '❌'}")
                await asyncio.sleep(1)
        # ถ้าเจอ 4+ ตัว → ส่ง summary + รายละเอียดเฉพาะตัว extreme
        else:
            summary = fmt_scan_summary(alerts)
            await send_telegram(summary, client)
            await asyncio.sleep(1)
            # ส่งรายละเอียดเฉพาะตัวที่ extreme (ratio >= 5x)
            extreme = [a for a in alerts if a["vol"]["max_ratio"] >= 5]
            for item in extreme[:3]:
                msg = fmt_volume_alert(item["data"], item["vol"])
                ok  = await send_telegram(msg, client)
                await asyncio.sleep(1)


if __name__ == "__main__":
    scanner = VolumeSurgeScanner()
    asyncio.run(scanner.run())
