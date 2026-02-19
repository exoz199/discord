"""
Discord FinanceBot v3 — Finnhub + SEC EDGAR + Claude AI
========================================================
Źródła danych:
  • Ceny i metryki rynkowe   → Finnhub API (oficjalne, darmowe, 60 req/min)
  • Sprawozdania finansowe   → SEC EDGAR data.sec.gov (rząd USA, bez klucza)
  • Analiza tekstowa         → Claude AI (Anthropic)

Dlaczego Finnhub zamiast yfinance:
  yfinance = scraper stron Yahoo → blokady 429, "Unauthorized", niestabilne
  Finnhub  = oficjalne REST API  → dedykowane, stabilne, 60 wywołań/min za darmo

Spółki: S&P 500 (SPY), NVIDIA (NVDA), Uber (UBER), CD Projekt (CDR.WA)
"""

import discord
from discord.ext import commands, tasks
import requests
import anthropic
import json
import os
import time
import random
from datetime import datetime, timedelta
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# KONFIGURACJA — uzupełnij te 4 wartości
# ══════════════════════════════════════════════════════════════════
# Opcja A — wpisz bezpośrednio (lokalne uruchomienie):
BOT_TOKEN      = "TWOJ_TOKEN_BOTA"       # discord.com/developers → Bot → Token
CHANNEL_ID     = 123456789012345678       # prawy klik na kanał → Kopiuj ID (liczba!)
ANTHROPIC_KEY  = "TWOJ_KLUCZ_ANTHROPIC"  # console.anthropic.com → API Keys
FINNHUB_KEY    = "TWOJ_KLUCZ_FINNHUB"    # finnhub.io → Dashboard → API Key (darmowe!)

# Opcja B — zmienne środowiskowe (Railway / VPS):
# import os
# BOT_TOKEN     = os.environ["BOT_TOKEN"]
# CHANNEL_ID    = int(os.environ["CHANNEL_ID"])
# ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
# FINNHUB_KEY   = os.environ["FINNHUB_KEY"]
# ══════════════════════════════════════════════════════════════════

FINNHUB_BASE  = "https://finnhub.io/api/v1"
SEC_HEADERS   = {"User-Agent": "FinanceBot contact@financebot.pl"}
INTERVAL_MIN  = 15        # co ile minut wysyłać wiadomości
CACHE_TTL     = 25        # minuty ważności cache Finnhub
CALL_DELAY    = 1.2       # sekund między kolejnymi requestami Finnhub (max 60/min)

_cache: dict = {}         # {ticker: (datetime, data)}

# Spółki: ticker Finnhub + CIK dla SEC EDGAR (None = brak danych EDGAR)
COMPANIES = {
    "S&P 500 (SPY)": {
        "ticker":    "SPY",
        "cik":       None,          # ETF — nie składa 10-K do SEC
        "currency":  "USD",
        "market":    "US",
    },
    "NVIDIA": {
        "ticker":    "NVDA",
        "cik":       "0001045810",  # oficjalny CIK w SEC EDGAR
        "currency":  "USD",
        "market":    "US",
    },
    "Uber": {
        "ticker":    "UBER",
        "cik":       "0001543151",
        "currency":  "USD",
        "market":    "US",
    },
    "CD Projekt": {
        "ticker":    "CDR.WA",      # Finnhub: ticker.giełda
        "cik":       None,          # polska spółka — nie w SEC
        "currency":  "PLN",
        "market":    "WSE",         # Warsaw Stock Exchange
    },
}

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)
ai      = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ─────────────────────────────────────────────────────────────────
# HISTORIA (anty-repeat)
# ─────────────────────────────────────────────────────────────────
HISTORY_FILE = "sent_messages.json"

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(h: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)

def pick_company(history: dict) -> tuple[str, dict]:
    now      = datetime.now()
    cooldown = len(COMPANIES) * INTERVAL_MIN * 60
    available = [
        (name, cfg) for name, cfg in COMPANIES.items()
        if (now - datetime.fromisoformat(
            history.get(cfg["ticker"], "2000-01-01T00:00:00")
        )).total_seconds() > cooldown
    ]
    if not available:
        oldest = min(
            COMPANIES.items(),
            key=lambda x: datetime.fromisoformat(
                history.get(x[1]["ticker"], "2000-01-01T00:00:00")
            )
        )
        return oldest
    return random.choice(available)


# ─────────────────────────────────────────────────────────────────
# FORMATOWANIE
# ─────────────────────────────────────────────────────────────────
def fmt(val, cur="", pct=False, dec=2) -> str:
    if val is None or val == 0:
        return "brak"
    if pct:
        return f"{val*100:.1f}%"
    v = abs(val)
    if v >= 1e12: return f"{val/1e12:.2f}T {cur}".strip()
    if v >= 1e9:  return f"{val/1e9:.2f}B {cur}".strip()
    if v >= 1e6:  return f"{val/1e6:.2f}M {cur}".strip()
    if v >= 1e3:  return f"{val/1e3:.1f}K {cur}".strip()
    return f"{val:.{dec}f} {cur}".strip()

def fmt_edgar(val) -> str:
    if val is None: return "brak"
    v = abs(val)
    if v >= 1e9:  return f"{val/1e9:.2f}B USD"
    if v >= 1e6:  return f"{val/1e6:.2f}M USD"
    return f"{val:,.0f} USD"


# ─────────────────────────────────────────────────────────────────
# ŹRÓDŁO 1: FINNHUB API
# ─────────────────────────────────────────────────────────────────
def _finnhub_get(endpoint: str, params: dict) -> Optional[dict]:
    """Wywołanie Finnhub z obsługą błędów."""
    params["token"] = FINNHUB_KEY
    try:
        r = requests.get(f"{FINNHUB_BASE}/{endpoint}", params=params, timeout=10)
        if r.status_code == 429:
            print(f"[Finnhub] Rate-limit 429 na /{endpoint} — czekam 15s")
            time.sleep(15)
            r = requests.get(f"{FINNHUB_BASE}/{endpoint}", params=params, timeout=10)
        if r.status_code == 403:
            print(f"[Finnhub] 403 Forbidden — sprawdź FINNHUB_KEY w konfiguracji")
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[Finnhub] Błąd /{endpoint}: {e}")
        return None

def get_market_data(ticker: str, currency: str) -> Optional[dict]:
    """
    Pobiera dane rynkowe z Finnhub (quote + profile + basic financials).
    Wyniki są cache'owane przez CACHE_TTL minut.
    """
    global _cache
    now = datetime.now()

    # Cache hit
    if ticker in _cache:
        age = (now - _cache[ticker][0]).total_seconds() / 60
        if age < CACHE_TTL:
            print(f"[cache] {ticker} — wiek {age:.1f} min (TTL {CACHE_TTL} min)")
            return _cache[ticker][1]

    print(f"[Finnhub] Pobieram dane dla {ticker}...")

    # 1) Kurs i zmiana dzienna
    time.sleep(CALL_DELAY)
    quote = _finnhub_get("quote", {"symbol": ticker})
    if not quote or quote.get("c", 0) == 0:
        print(f"[Finnhub] Brak danych quote dla {ticker} — sprawdź ticker")
        return None

    # 2) Profil spółki (nazwa, sektor, pracownicy, opis itd.)
    time.sleep(CALL_DELAY)
    profile = _finnhub_get("stock/profile2", {"symbol": ticker}) or {}

    # 3) Fundamenty (P/E, EPS, marże, ROE, dług itd.)
    time.sleep(CALL_DELAY)
    basics_raw = _finnhub_get("stock/metric", {"symbol": ticker, "metric": "all"}) or {}
    m = basics_raw.get("metric", {})

    price     = quote.get("c", 0)
    prev      = quote.get("pc", price)
    chg       = quote.get("d", 0)
    chg_pct   = quote.get("dp", 0) / 100 if quote.get("dp") else ((price - prev) / prev if prev else 0)

    data = {
        # Cena
        "name":         profile.get("name", ticker),
        "price":        price,
        "prev_close":   prev,
        "change":       chg,
        "change_pct":   chg_pct,
        "high_day":     quote.get("h"),
        "low_day":      quote.get("l"),
        "open":         quote.get("o"),
        # Metryki
        "market_cap":   m.get("marketCapitalization", 0) * 1e6 if m.get("marketCapitalization") else None,
        "pe":           m.get("peBasicExclExtraTTM") or m.get("peTTM"),
        "fwd_pe":       m.get("forwardPE"),
        "eps":          m.get("epsBasicExclExtraAnnual") or m.get("epsTTM"),
        "pb":           m.get("pbAnnual"),
        "ps":           m.get("psTTM"),
        "ev_ebitda":    m.get("evToEbitda"),
        # Rentowność
        "revenue":      m.get("revenuePerShareAnnual", 0) * m.get("sharesOutstanding", 0) * 1e6 if m.get("revenuePerShareAnnual") and m.get("sharesOutstanding") else None,
        "gross_margin": m.get("grossMarginAnnual") / 100 if m.get("grossMarginAnnual") else None,
        "op_margin":    m.get("operatingMarginAnnual") / 100 if m.get("operatingMarginAnnual") else None,
        "net_margin":   m.get("netMarginAnnual") / 100 if m.get("netMarginAnnual") else None,
        "roe":          m.get("roeAnnual") / 100 if m.get("roeAnnual") else None,
        "roa":          m.get("roaAnnual") / 100 if m.get("roaAnnual") else None,
        "roic":         m.get("roicAnnual") / 100 if m.get("roicAnnual") else None,
        # Zadłużenie/Płynność
        "dte":          m.get("totalDebt/totalEquityAnnual") or m.get("longTermDebt/equityAnnual"),
        "current_ratio":m.get("currentRatioAnnual"),
        "quick_ratio":  m.get("quickRatioAnnual"),
        # Cash Flow / Wzrost
        "fcf_yield":    m.get("fcfYieldTTM") / 100 if m.get("fcfYieldTTM") else None,
        "rev_growth_5y":m.get("revenueGrowth5Y") / 100 if m.get("revenueGrowth5Y") else None,
        "eps_growth_5y":m.get("epsGrowth5Y") / 100 if m.get("epsGrowth5Y") else None,
        # Dywidenda/Zmienność
        "div_yield":    m.get("dividendYieldIndicatedAnnual") / 100 if m.get("dividendYieldIndicatedAnnual") else None,
        "beta":         m.get("beta"),
        "52w_high":     m.get("52WeekHigh"),
        "52w_low":      m.get("52WeekLow"),
        "52w_return":   m.get("52WeekPriceReturnDaily") / 100 if m.get("52WeekPriceReturnDaily") else None,
        # Profil
        "sector":       profile.get("finnhubIndustry", ""),
        "country":      profile.get("country", ""),
        "exchange":     profile.get("exchange", ""),
        "ipo":          profile.get("ipo", ""),
        "employees":    profile.get("employeeTotal"),
        "description":  profile.get("description", "")[:450] if profile.get("description") else "",
        "website":      profile.get("weburl", ""),
        "currency":     currency,
    }

    # 4) Rekomendacje analityków (osobny endpoint)
    time.sleep(CALL_DELAY)
    rec_raw = _finnhub_get("stock/recommendation", {"symbol": ticker})
    if rec_raw and len(rec_raw) > 0:
        latest = rec_raw[0]
        data["rec_buy"]    = latest.get("buy", 0)
        data["rec_hold"]   = latest.get("hold", 0)
        data["rec_sell"]   = latest.get("sell", 0)
        data["rec_strong_buy"]  = latest.get("strongBuy", 0)
        data["rec_strong_sell"] = latest.get("strongSell", 0)
        total = sum([data["rec_buy"], data["rec_hold"], data["rec_sell"],
                     data["rec_strong_buy"], data["rec_strong_sell"]])
        data["rec_total"]  = total
        # Dominująca rekomendacja
        cats = {
            "STRONG BUY": data["rec_strong_buy"],
            "BUY":        data["rec_buy"],
            "HOLD":       data["rec_hold"],
            "SELL":       data["rec_sell"],
            "STRONG SELL":data["rec_strong_sell"],
        }
        data["rec"] = max(cats, key=cats.get) if total > 0 else "brak"
    else:
        data["rec"] = "brak"
        data["rec_total"] = 0

    # 5) Cel cenowy analityków
    time.sleep(CALL_DELAY)
    target_raw = _finnhub_get("stock/price-target", {"symbol": ticker}) or {}
    data["target_mean"] = target_raw.get("targetMean")
    data["target_high"] = target_raw.get("targetHigh")
    data["target_low"]  = target_raw.get("targetLow")

    _cache[ticker] = (now, data)
    print(f"[Finnhub] ✅ Pobrano {ticker} — {data['name']} @ {price} {currency}")
    return data


# ─────────────────────────────────────────────────────────────────
# ŹRÓDŁO 2: SEC EDGAR — bez API key
# ─────────────────────────────────────────────────────────────────
SEC_CONCEPTS = {
    "revenue":      ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "op_income":    ["OperatingIncomeLoss"],
    "net_income":   ["NetIncomeLoss"],
    "eps_diluted":  ["EarningsPerShareDiluted"],
    "eps_basic":    ["EarningsPerShareBasic"],
    "shares":       ["CommonStockSharesOutstanding"],
    "total_assets": ["Assets"],
    "total_liab":   ["Liabilities"],
    "equity":       ["StockholdersEquity"],
    "cash":         ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"],
    "long_term_debt":["LongTermDebt", "LongTermDebtNoncurrent"],
    "current_assets":["AssetsCurrent"],
    "current_liab": ["LiabilitiesCurrent"],
    "cfo":          ["NetCashProvidedByUsedInOperatingActivities"],
    "capex":        ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "dividends":    ["PaymentsOfDividends"],
}

def get_edgar_facts(cik: str) -> Optional[dict]:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"[EDGAR] Błąd {cik}: {e}")
        return None

    us_gaap = raw.get("facts", {}).get("us-gaap", {})
    result  = {"entity": raw.get("entityName", ""), "cik": cik}

    def latest_filing(concept_list, forms=("10-K","20-F","10-Q")):
        for concept in concept_list:
            if concept not in us_gaap:
                continue
            for unit_type, facts_list in us_gaap[concept].get("units", {}).items():
                hits = [f for f in facts_list if f.get("form") in forms and f.get("val") is not None]
                if hits:
                    hits.sort(key=lambda x: x.get("end",""), reverse=True)
                    return hits[0]["val"], hits[0].get("end",""), unit_type
        return None

    for key, concepts in SEC_CONCEPTS.items():
        hit = latest_filing(concepts, ("10-K","20-F")) or latest_filing(concepts)
        if hit:
            result[key], result[f"{key}_period"], result[f"{key}_unit"] = hit
        else:
            result[key] = None

    # Wyliczone wskaźniki
    if result.get("revenue") and result.get("net_income"):
        result["net_margin_calc"] = result["net_income"] / result["revenue"]
    if result.get("revenue") and result.get("gross_profit"):
        result["gross_margin_calc"] = result["gross_profit"] / result["revenue"]
    if result.get("revenue") and result.get("op_income"):
        result["op_margin_calc"] = result["op_income"] / result["revenue"]
    if result.get("cfo") and result.get("capex"):
        result["fcf_calc"] = result["cfo"] - abs(result["capex"])
    if result.get("total_liab") and result.get("total_assets"):
        result["debt_ratio"] = result["total_liab"] / result["total_assets"]
    if result.get("current_assets") and result.get("current_liab") and result["current_liab"]:
        result["current_ratio_calc"] = result["current_assets"] / result["current_liab"]

    return result

def get_recent_filings(cik: str, count: int = 3) -> list:
    url = f"https://data.sec.gov/submissions/{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        r.raise_for_status()
        data    = r.json()
        filings = data.get("filings", {}).get("recent", {})
        forms   = filings.get("form", [])
        dates   = filings.get("filingDate", [])
        accs    = filings.get("accessionNumber", [])
        results = []
        for form, date, acc in zip(forms, dates, accs):
            if form in ("10-K", "10-Q", "8-K") and len(results) < count:
                results.append({
                    "form": form, "date": date,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc.replace('-','')}/"
                })
        return results
    except:
        return []


# ─────────────────────────────────────────────────────────────────
# EMBED 1: NOTOWANIE (Finnhub)
# ─────────────────────────────────────────────────────────────────
def build_quote_embed(d: dict, cfg: dict) -> discord.Embed:
    cur  = cfg["currency"]
    chg  = d.get("change_pct") or 0
    pos  = chg >= 0
    col  = discord.Color.green() if pos else discord.Color.red()
    arr  = "📈" if pos else "📉"
    sym  = "▲" if pos else "▼"

    price_str = f"{d['price']:.2f} {cur}"
    chg_str   = f"{sym} {'+' if pos else ''}{chg*100:.2f}%"

    # Rekomendacje
    if d.get("rec_total", 0) > 0:
        rec_str = (
            f"**{d.get('rec','?')}**\n"
            f"SB:{d.get('rec_strong_buy',0)} B:{d.get('rec_buy',0)} "
            f"H:{d.get('rec_hold',0)} S:{d.get('rec_sell',0)} SS:{d.get('rec_strong_sell',0)}"
        )
    else:
        rec_str = "brak"

    target_str = (
        f"Śr: {fmt(d.get('target_mean'), cur)}\n"
        f"Max: {fmt(d.get('target_high'), cur)} / Min: {fmt(d.get('target_low'), cur)}"
        if d.get("target_mean") else "brak"
    )

    embed = discord.Embed(
        title       = f"{arr} {d['name']} ({cfg['ticker']})",
        description = d.get("description") or "Brak opisu.",
        color       = col,
        timestamp   = datetime.now(),
    )

    embed.add_field(name="💰 Cena",
        value=f"**{price_str}**\n{chg_str}\nO: {fmt(d.get('open'),cur)} | H: {fmt(d.get('high_day'),cur)} | L: {fmt(d.get('low_day'),cur)}",
        inline=True)
    embed.add_field(name="🏦 Kapitalizacja",
        value=f"{fmt(d.get('market_cap'), cur)}\nP/B: {fmt(d.get('pb'))}\nP/S: {fmt(d.get('ps'))}",
        inline=True)
    embed.add_field(name="📊 Wycena",
        value=f"P/E: {fmt(d.get('pe'))}\nFwd P/E: {fmt(d.get('fwd_pe'))}\nEV/EBITDA: {fmt(d.get('ev_ebitda'))}",
        inline=True)

    embed.add_field(name="📈 Marże",
        value=f"Brutto: {fmt(d.get('gross_margin'), pct=True)}\nOper.: {fmt(d.get('op_margin'), pct=True)}\nNetto: {fmt(d.get('net_margin'), pct=True)}",
        inline=True)
    embed.add_field(name="💹 Zwroty",
        value=f"ROE: {fmt(d.get('roe'), pct=True)}\nROA: {fmt(d.get('roa'), pct=True)}\nROIC: {fmt(d.get('roic'), pct=True)}",
        inline=True)
    embed.add_field(name="🏗️ Bilans",
        value=f"D/E: {fmt(d.get('dte'))}\nCurr.R: {fmt(d.get('current_ratio'))}×\nQuick: {fmt(d.get('quick_ratio'))}×",
        inline=True)

    embed.add_field(name="📅 52-tygodniowy",
        value=f"Max: {fmt(d.get('52w_high'), cur)}\nMin: {fmt(d.get('52w_low'), cur)}\nZwrot: {fmt(d.get('52w_return'), pct=True)}",
        inline=True)
    embed.add_field(name="🎯 Dywidenda · Beta",
        value=f"Dyw: {fmt(d.get('div_yield'), pct=True)}\nBeta: {fmt(d.get('beta'))}\nFCF Yield: {fmt(d.get('fcf_yield'), pct=True)}",
        inline=True)
    embed.add_field(name="📊 Wzrost (5-letni)",
        value=f"Przychody: {fmt(d.get('rev_growth_5y'), pct=True)}\nEPS: {fmt(d.get('eps_growth_5y'), pct=True)}",
        inline=True)

    embed.add_field(name="🔍 Rekomendacje analityków", value=rec_str, inline=True)
    embed.add_field(name="🎯 Cele cenowe", value=target_str, inline=True)
    if d.get("sector") or d.get("country"):
        embed.add_field(name="🌍 Sektor · Kraj",
            value=f"{d.get('sector','—')}\n{d.get('country','—')} · {d.get('exchange','—')}",
            inline=True)

    embed.set_footer(text=f"Źródło: Finnhub API (oficjalne) · {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return embed


# ─────────────────────────────────────────────────────────────────
# EMBED 2: SEC EDGAR
# ─────────────────────────────────────────────────────────────────
def build_edgar_embed(edgar: dict, filings: list, ticker: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🏛️ Dane SEC EDGAR — {edgar.get('entity', ticker)}",
        description=(
            "Oficjalne dane finansowe z **SEC EDGAR** (10-K/10-Q).\n"
            "Bez pośredników · Zero opóźnień · Źródło: rząd USA 🇺🇸\n"
            f"CIK: `{edgar.get('cik')}` | Endpoint: `data.sec.gov`"
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(),
    )
    period = edgar.get("revenue_period") or edgar.get("net_income_period") or "nieznany"
    embed.add_field(name="📋 Rachunek zysk. i strat",
        value=(f"Przychody: **{fmt_edgar(edgar.get('revenue'))}**\n"
               f"Zysk brutto: {fmt_edgar(edgar.get('gross_profit'))}\n"
               f"Zysk oper.: {fmt_edgar(edgar.get('op_income'))}\n"
               f"Zysk netto: **{fmt_edgar(edgar.get('net_income'))}**"),
        inline=True)
    gm = edgar.get("gross_margin_calc")
    om = edgar.get("op_margin_calc")
    nm = edgar.get("net_margin_calc")
    embed.add_field(name="📊 Marże",
        value=(f"Brutto: {f'{gm*100:.1f}%' if gm else 'brak'}\n"
               f"Oper.: {f'{om*100:.1f}%' if om else 'brak'}\n"
               f"Netto: {f'{nm*100:.1f}%' if nm else 'brak'}"),
        inline=True)
    epsb = edgar.get("eps_basic")
    epsd = edgar.get("eps_diluted")
    embed.add_field(name="💹 EPS",
        value=(f"Basic: {f'${epsb:.2f}' if epsb else 'brak'}\n"
               f"Diluted: {f'${epsd:.2f}' if epsd else 'brak'}"),
        inline=True)
    embed.add_field(name="🏦 Bilans",
        value=(f"Aktywa: **{fmt_edgar(edgar.get('total_assets'))}**\n"
               f"Zobowiązania: {fmt_edgar(edgar.get('total_liab'))}\n"
               f"Kapitał własny: **{fmt_edgar(edgar.get('equity'))}**"),
        inline=True)
    cr = edgar.get("current_ratio_calc")
    dr = edgar.get("debt_ratio")
    embed.add_field(name="💳 Płynność · Dług",
        value=(f"Gotówka: {fmt_edgar(edgar.get('cash'))}\n"
               f"Dług dług.: {fmt_edgar(edgar.get('long_term_debt'))}\n"
               f"Curr.ratio: {f'{cr:.2f}×' if cr else 'brak'}\n"
               f"Debt ratio: {f'{dr*100:.1f}%' if dr else 'brak'}"),
        inline=True)
    fcf = edgar.get("fcf_calc")
    embed.add_field(name="💸 Cash Flow",
        value=(f"CFO: **{fmt_edgar(edgar.get('cfo'))}**\n"
               f"CAPEX: {fmt_edgar(edgar.get('capex'))}\n"
               f"FCF: **{fmt_edgar(fcf)}**\n"
               f"Dywidendy: {fmt_edgar(edgar.get('dividends'))}"),
        inline=True)
    if filings:
        fl = "\n".join(f"[{f['form']} – {f['date']}]({f['url']})" for f in filings[:3])
        embed.add_field(name="📁 Ostatnie złożenia SEC", value=fl, inline=False)
    embed.set_footer(text=f"data.sec.gov/api/xbrl/companyfacts · Bez API key · Okres: {period} · {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return embed


# ─────────────────────────────────────────────────────────────────
# ŹRÓDŁO 3: CLAUDE AI — analiza
# ─────────────────────────────────────────────────────────────────
def generate_ai_report(name: str, ticker: str, d: dict, edgar: Optional[dict], cur: str) -> str:
    edgar_block = ""
    if edgar:
        gm = edgar.get("gross_margin_calc")
        om = edgar.get("op_margin_calc")
        nm = edgar.get("net_margin_calc")
        cr = edgar.get("current_ratio_calc")
        dr = edgar.get("debt_ratio")
        edgar_block = f"""
DANE SEC EDGAR (oficjalne 10-K/10-Q):
- Przychody: {fmt_edgar(edgar.get('revenue'))} | Okres: {edgar.get('revenue_period','?')}
- Zysk brutto: {fmt_edgar(edgar.get('gross_profit'))} | Marża: {f'{gm*100:.1f}%' if gm else 'brak'}
- Zysk operacyjny: {fmt_edgar(edgar.get('op_income'))} | Marża: {f'{om*100:.1f}%' if om else 'brak'}
- Zysk netto: {fmt_edgar(edgar.get('net_income'))} | Marża: {f'{nm*100:.1f}%' if nm else 'brak'}
- EPS (diluted): {edgar.get('eps_diluted','brak')}
- Aktywa: {fmt_edgar(edgar.get('total_assets'))} | Zobowiązania: {fmt_edgar(edgar.get('total_liab'))}
- Kapitał własny: {fmt_edgar(edgar.get('equity'))}
- Gotówka: {fmt_edgar(edgar.get('cash'))} | Dług: {fmt_edgar(edgar.get('long_term_debt'))}
- Current ratio: {f'{cr:.2f}' if cr else 'brak'} | Debt ratio: {f'{dr*100:.1f}%' if dr else 'brak'}
- CFO: {fmt_edgar(edgar.get('cfo'))} | CAPEX: {fmt_edgar(edgar.get('capex'))} | FCF: {fmt_edgar(edgar.get('fcf_calc'))}"""
    else:
        edgar_block = "EDGAR: niedostępne (ETF lub spółka zagraniczna) — dane tylko z Finnhub."

    rec_block = ""
    if d.get("rec_total", 0) > 0:
        rec_block = (f"Strong Buy: {d.get('rec_strong_buy',0)} | Buy: {d.get('rec_buy',0)} | "
                     f"Hold: {d.get('rec_hold',0)} | Sell: {d.get('rec_sell',0)} | "
                     f"Strong Sell: {d.get('rec_strong_sell',0)} | Dominuje: {d.get('rec','?')}")

    prompt = f"""Jesteś analitykiem finansowym CFA. Napisz zwięzłe profesjonalne sprawozdanie po POLSKU tylko na bazie poniższych danych. Nie dodawaj informacji spoza danych.

SPÓŁKA: {name} ({ticker}) | Sektor: {d.get('sector','')} | Kraj: {d.get('country','')}
OPIS: {d.get('description','')}

DANE FINNHUB (rynkowe):
- Cena: {d.get('price',0):.2f} {cur} | Zmiana: {(d.get('change_pct',0))*100:.2f}%
- Kap. rynkowa: {fmt(d.get('market_cap'), cur)}
- P/E: {fmt(d.get('pe'))} | Fwd P/E: {fmt(d.get('fwd_pe'))} | EV/EBITDA: {fmt(d.get('ev_ebitda'))}
- Marża brutto/oper./netto: {fmt(d.get('gross_margin'),pct=True)} / {fmt(d.get('op_margin'),pct=True)} / {fmt(d.get('net_margin'),pct=True)}
- ROE: {fmt(d.get('roe'),pct=True)} | ROA: {fmt(d.get('roa'),pct=True)} | ROIC: {fmt(d.get('roic'),pct=True)}
- D/E: {fmt(d.get('dte'))} | Curr.R: {fmt(d.get('current_ratio'))} | FCF Yield: {fmt(d.get('fcf_yield'),pct=True)}
- Beta: {fmt(d.get('beta'))} | Dyw: {fmt(d.get('div_yield'),pct=True)}
- 52-tyg.: {fmt(d.get('52w_high'),cur)} / {fmt(d.get('52w_low'),cur)} | Zwrot: {fmt(d.get('52w_return'),pct=True)}
- Cel cenowy analityków: śr. {fmt(d.get('target_mean'),cur)} (max {fmt(d.get('target_high'),cur)}, min {fmt(d.get('target_low'),cur)})
- Rekomendacje: {rec_block or 'brak'}
{edgar_block}

Napisz sprawozdanie z tymi nagłówkami:

📋 **PODSUMOWANIE**
[2-3 zdania — najważniejsze wyniki i trend]

💰 **RENTOWNOŚĆ I PRZYCHODY**
[Marże, wzrost, jakość zysku]

🏦 **SYTUACJA BILANSOWA**
[Aktywa, dług, płynność]

💸 **PRZEPŁYWY PIENIĘŻNE**
[CFO, CAPEX, FCF — czy spółka generuje gotówkę]

⚠️ **RYZYKA**
[3-4 punkty z •]

🔭 **PERSPEKTYWY**
[Krótko- i średnioterminowa prognoza]

⚖️ **VERDICT: [POZYTYWNY / NEUTRALNY / NEGATYWNY / SPEKULACYJNY]**
[Dla kogo ta spółka jest odpowiednia?]

---
*Dane: Finnhub API + SEC EDGAR · {datetime.now().strftime('%d.%m.%Y %H:%M')} · Nie stanowi porady inwestycyjnej*

Max 520 słów. Po polsku. Nie wymyślaj liczb."""

    try:
        msg = ai.messages.create(
            model="claude-sonnet-4-6", max_tokens=1200,
            messages=[{"role":"user","content":prompt}]
        )
        return msg.content[0].text
    except Exception as e:
        print(f"[Claude] Błąd: {e}")
        return f"⚠️ Błąd generowania analizy: {e}"

def build_ai_embed(name: str, ticker: str, text: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"🤖 Analiza AI — {name} ({ticker})",
        description=text,
        color=discord.Color.gold(),
        timestamp=datetime.now(),
    )
    embed.set_footer(text="Claude AI · Finnhub + SEC EDGAR · Nie stanowi porady inwestycyjnej")
    return embed


# ─────────────────────────────────────────────────────────────────
# GŁÓWNA PĘTLA
# ─────────────────────────────────────────────────────────────────
@tasks.loop(minutes=INTERVAL_MIN)
async def send_update():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"[Bot] Kanał {CHANNEL_ID} nie znaleziony")
        return

    history     = load_history()
    name, cfg   = pick_company(history)
    ticker      = cfg["ticker"]
    cik         = cfg["cik"]
    cur         = cfg["currency"]
    print(f"[{datetime.now():%H:%M}] Wysyłam: {name} ({ticker})")

    # 1) Dane rynkowe (Finnhub)
    market = get_market_data(ticker, cur)
    if not market:
        err = discord.Embed(
            title=f"⚠️ Problem z danymi — {name} ({ticker})",
            description=(
                "Nie udało się pobrać danych z Finnhub.\n\n"
                "**Możliwe przyczyny:**\n"
                "• Nieprawidłowy klucz `FINNHUB_KEY` w konfiguracji\n"
                "• Ticker może wymagać korekty (np. `CDR.WA` zamiast `CDR`)\n"
                "• Tymczasowa niedostępność API\n\n"
                "Bot automatycznie przejdzie do kolejnej spółki."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(),
        )
        await channel.send(embed=err)
        history[ticker] = datetime.now().isoformat()
        save_history(history)
        return

    # 2) SEC EDGAR
    edgar   = get_edgar_facts(cik)   if cik else None
    filings = get_recent_filings(cik) if cik else []

    # 3) Wyślij embedy
    await channel.send(embed=build_quote_embed(market, cfg))
    if edgar:
        await channel.send(embed=build_edgar_embed(edgar, filings, ticker))

    tmp = await channel.send("⏳ Generuję analizę AI (Finnhub + SEC EDGAR)...")
    report = generate_ai_report(name, ticker, market, edgar, cur)
    await tmp.delete()
    await channel.send(embed=build_ai_embed(name, ticker, report))

    history[ticker] = datetime.now().isoformat()
    save_history(history)
    print(f"[Bot] ✅ Wysłano komplet dla {name}")


# ─────────────────────────────────────────────────────────────────
# KOMENDY
# ─────────────────────────────────────────────────────────────────
@bot.command(name="spółka", aliases=["spolka","stock","notowanie"])
async def cmd_stock(ctx, ticker: str = None):
    """!spółka TICKER — pobierz notowanie z Finnhub"""
    if not ticker:
        return await ctx.send("Użycie: `!spółka TICKER` np. `!spółka NVDA`")
    t = ticker.upper()
    await ctx.send(f"⏳ Pobieram dane Finnhub dla `{t}`...")
    data = get_market_data(t, "USD")
    if data:
        await ctx.send(embed=build_quote_embed(data, {"ticker": t, "currency": "USD"}))
    else:
        await ctx.send(f"❌ Brak danych dla `{t}`. Sprawdź ticker na finnhub.io")

@bot.command(name="edgar")
async def cmd_edgar(ctx, cik: str = None):
    """!edgar CIK — dane SEC EDGAR (np. !edgar 0001045810)"""
    if not cik:
        return await ctx.send(
            "Użycie: `!edgar CIK`\nPrzykłady:\n"
            "• NVIDIA: `!edgar 0001045810`\n• Uber: `!edgar 0001543151`\n"
            "Znajdź CIK: <https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany>"
        )
    await ctx.send(f"⏳ Pobieram SEC EDGAR dla CIK `{cik}`...")
    edgar = get_edgar_facts(cik)
    filings = get_recent_filings(cik)
    if edgar:
        await ctx.send(embed=build_edgar_embed(edgar, filings, cik))
    else:
        await ctx.send(f"❌ Brak danych EDGAR dla CIK `{cik}`")

@bot.command(name="raport")
async def cmd_report(ctx, ticker: str = None):
    """!raport TICKER — pełna analiza (Finnhub + EDGAR + AI)"""
    if not ticker:
        return await ctx.send("Użycie: `!raport TICKER` np. `!raport NVDA`")
    t   = ticker.upper()
    cfg = next(((n, c) for n, c in COMPANIES.items() if c["ticker"] == t), None)
    cur = cfg[1]["currency"] if cfg else "USD"
    name = cfg[0] if cfg else t
    cik  = cfg[1]["cik"] if cfg else None

    tmp = await ctx.send(f"⏳ Pobieram dane i generuję raport dla `{t}`...")
    data = get_market_data(t, cur)
    if not data:
        return await tmp.edit(content=f"❌ Brak danych Finnhub dla `{t}`")
    edgar   = get_edgar_facts(cik)   if cik else None
    filings = get_recent_filings(cik) if cik else []
    await ctx.send(embed=build_quote_embed(data, {"ticker": t, "currency": cur}))
    if edgar:
        await ctx.send(embed=build_edgar_embed(edgar, filings, t))
    report = generate_ai_report(name, t, data, edgar, cur)
    await ctx.send(embed=build_ai_embed(name, t, report))
    await tmp.delete()

@bot.command(name="lista")
async def cmd_lista(ctx):
    lines = [
        f"• **{n}** (`{c['ticker']}`){' · EDGAR ✓' if c['cik'] else ' · Finnhub only'}"
        for n, c in COMPANIES.items()
    ]
    embed = discord.Embed(title="📋 Śledzone spółki", description="\n".join(lines), color=discord.Color.blue())
    embed.add_field(name="Źródła",
        value="📊 **Finnhub** — ceny, metryki, rekomendacje (60 req/min, darmowe)\n"
              "🏛️ **SEC EDGAR** — bilanse, R/Z, CF (rząd USA, bez klucza)\n"
              "🤖 **Claude AI** — analiza i komentarz",
        inline=False)
    embed.set_footer(text=f"Aktualizacje co {INTERVAL_MIN} min")
    await ctx.send(embed=embed)

@bot.command(name="historia")
async def cmd_historia(ctx):
    history = load_history()
    lines = []
    for name, cfg in COMPANIES.items():
        last = history.get(cfg["ticker"])
        ago  = int((datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60) if last else None
        lines.append(f"• **{name}** — {f'{ago} min temu' if ago is not None else 'jeszcze nie wysłano'}")
    await ctx.send(embed=discord.Embed(title="🕒 Historia", description="\n".join(lines), color=discord.Color.gold()))

@bot.command(name="pomoc", aliases=["help"])
async def cmd_help(ctx):
    embed = discord.Embed(title="📖 Komendy FinanceBot", color=discord.Color.blurple())
    embed.add_field(name="!lista",         value="Lista spółek i źródeł danych", inline=False)
    embed.add_field(name="!spółka TICKER", value="Notowanie Finnhub, np. `!spółka AAPL`", inline=False)
    embed.add_field(name="!edgar CIK",     value="Dane SEC EDGAR, np. `!edgar 0001045810`", inline=False)
    embed.add_field(name="!raport TICKER", value="Pełna analiza AI, np. `!raport NVDA`", inline=False)
    embed.add_field(name="!historia",      value="Kiedy ostatnio wysłano każdą spółkę", inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f"✅ {bot.user} zalogowany")
    print(f"📡 Interwał: co {INTERVAL_MIN} min | Cache: {CACHE_TTL} min")
    print(f"📊 Źródła: Finnhub API + SEC EDGAR (bez klucza) + Claude AI")
    print(f"🏢 Spółki: {', '.join(COMPANIES.keys())}")
    if not send_update.is_running():
        send_update.start()

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
