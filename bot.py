"""
Discord FinanceBot – dane z SEC EDGAR (bez API key) + yfinance
=============================================================
Źródła:
  • Ceny/wyceny/metryki rynkowe  → yfinance (darmowe, bez klucza)
  • Sprawozdania finansowe        → SEC EDGAR data.sec.gov API (oficjalne, bez klucza, rząd USA)
  • Generowanie analizy          → Claude API (anthropic)

Spółki: S&P 500 (SPY), NVIDIA (NVDA), Uber (UBER), CD Projekt (CDR.WA)
Uwaga: SPY i CDR.WA nie składają 10-K/10-Q do SEC → dla nich dane z yfinance + EDGAR fundamentals API
"""

import discord
from discord.ext import commands, tasks
import yfinance as yf
import anthropic
import requests
import json
import os
import random
import time
from datetime import datetime, timedelta
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# KONFIGURACJA
# ══════════════════════════════════════════════════════════════════
# Opcja A — wpisz wartości bezpośrednio (lokalne uruchomienie):
BOT_TOKEN     = "TWOJ_TOKEN_BOTA"          # discord.com/developers → Bot → Token
CHANNEL_ID    = 123456789012345678          # ID kanału: prawy klik → Kopiuj ID (liczba)
ANTHROPIC_KEY = "TWOJ_KLUCZ_ANTHROPIC"     # console.anthropic.com → API Keys

# Opcja B — zmienne środowiskowe (Railway / VPS / .env):
# Zakomentuj Opcję A i odkomentuj poniższe 3 linie:
# BOT_TOKEN     = os.environ["BOT_TOKEN"]
# CHANNEL_ID    = int(os.environ["CHANNEL_ID"])
# ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]

# ── Anti-rate-limit: cache danych yfinance ────────────────────────
# Dane są cache'owane przez CACHE_TTL minut — bot nie będzie pytał
# Yahoo Finance częściej niż raz na tyle minut dla tej samej spółki.
CACHE_TTL     = 30          # minuty ważności cache (zalecane min. 20)
YFINANCE_WAIT = 3.0         # sekundy czekania między requestami do Yahoo
MAX_RETRIES   = 3           # ile razy ponawiać przy błędzie 429
_cache: dict  = {}          # słownik {ticker: (timestamp, data)}

# CIK-i dla SEC EDGAR (data.sec.gov)
# Sprawdź: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=&CIK=TICKER
COMPANIES = {
    "S&P 500 (SPY)": {
        "ticker":   "SPY",
        "cik":      None,           # ETF – nie składa 10-K do SEC, dane z yfinance
        "currency": "USD",
    },
    "NVIDIA": {
        "ticker":   "NVDA",
        "cik":      "0001045810",   # oficjalny CIK NVIDIA w SEC EDGAR
        "currency": "USD",
    },
    "Uber": {
        "ticker":   "UBER",
        "cik":      "0001543151",   # oficjalny CIK Uber Technologies
        "currency": "USD",
    },
    "CD Projekt": {
        "ticker":   "CDR.WA",
        "cik":      None,           # polska spółka – nie w SEC, dane z yfinance
        "currency": "PLN",
    },
}

HISTORY_FILE     = "sent_messages.json"
INTERVAL_MINUTES = 15

# Nagłówek User-Agent wymagany przez SEC (imię, email wystarczy)
SEC_HEADERS = {"User-Agent": "FinanceBot contact@financebot.pl"}
# ══════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)
ai      = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ─────────────────────────────────────────────────────────────────
# HISTORIA (anty-repeat)
# ─────────────────────────────────────────────────────────────────
def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_history(h: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)

def pick_company(history: dict) -> tuple[str, dict]:
    now       = datetime.now()
    cooldown  = len(COMPANIES) * INTERVAL_MINUTES * 60
    available = [
        (name, cfg) for name, cfg in COMPANIES.items()
        if (now - datetime.fromisoformat(history.get(cfg["ticker"], "2000-01-01T00:00:00"))).total_seconds() > cooldown
    ]
    if not available:
        oldest = min(COMPANIES.items(),
                     key=lambda x: datetime.fromisoformat(history.get(x[1]["ticker"], "2000-01-01T00:00:00")))
        return oldest
    return random.choice(available)


# ─────────────────────────────────────────────────────────────────
# FORMATOWANIE
# ─────────────────────────────────────────────────────────────────
def fmt(val, cur="", pct=False, dec=2) -> str:
    if val is None:
        return "brak"
    if pct:
        return f"{val*100:.1f}%"
    v = abs(val)
    if v >= 1e12: return f"{val/1e12:.2f}T {cur}".strip()
    if v >= 1e9:  return f"{val/1e9:.2f}B {cur}".strip()
    if v >= 1e6:  return f"{val/1e6:.2f}M {cur}".strip()
    if v >= 1e3:  return f"{val/1e3:.1f}K {cur}".strip()
    return f"{val:.{dec}f} {cur}".strip()

def fmt_num(val) -> str:
    """Formatuje surową liczbę z EDGAR (w USD) → czytelny string"""
    if val is None: return "brak"
    v = abs(val)
    if v >= 1e12: return f"{val/1e12:.2f}B USD"   # EDGAR podaje w pełnych USD
    if v >= 1e9:  return f"{val/1e9:.2f}B USD"
    if v >= 1e6:  return f"{val/1e6:.2f}M USD"
    return f"{val:,.0f} USD"


# ─────────────────────────────────────────────────────────────────
# ŹRÓDŁO 1: yfinance – ceny i metryki rynkowe
# ─────────────────────────────────────────────────────────────────
def _fetch_yfinance_raw(ticker: str) -> Optional[dict]:
    """Surowe pobranie z yfinance — bez cache, z retry przy 429."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            t    = yf.Ticker(ticker)
            info = t.info
            if not info or ("regularMarketPrice" not in info and "currentPrice" not in info):
                print(f"[yfinance] Pusta odpowiedź dla {ticker} (próba {attempt})")
                time.sleep(YFINANCE_WAIT * attempt)
                continue
            return {
                "name":          info.get("shortName", ticker),
                "price":         info.get("currentPrice") or info.get("regularMarketPrice"),
                "change_pct":    info.get("regularMarketChangePercent"),
                "market_cap":    info.get("marketCap"),
                "pe":            info.get("trailingPE"),
                "fwd_pe":        info.get("forwardPE"),
                "eps":           info.get("trailingEps"),
                "revenue":       info.get("totalRevenue"),
                "margin":        info.get("profitMargins"),
                "gross_margin":  info.get("grossMargins"),
                "op_margin":     info.get("operatingMargins"),
                "roe":           info.get("returnOnEquity"),
                "roa":           info.get("returnOnAssets"),
                "dte":           info.get("debtToEquity"),
                "current_ratio": info.get("currentRatio"),
                "div":           info.get("dividendYield"),
                "beta":          info.get("beta"),
                "high52":        info.get("fiftyTwoWeekHigh"),
                "low52":         info.get("fiftyTwoWeekLow"),
                "volume":        info.get("regularMarketVolume"),
                "avg_volume":    info.get("averageVolume"),
                "free_cf":       info.get("freeCashflow"),
                "ebitda":        info.get("ebitda"),
                "sector":        info.get("sector", ""),
                "industry":      info.get("industry", ""),
                "description":   (info.get("longBusinessSummary") or "")[:450],
                "rec":           info.get("recommendationKey", "").upper(),
                "target":        info.get("targetMeanPrice"),
                "rev_growth":    info.get("revenueGrowth"),
                "earn_growth":   info.get("earningsGrowth"),
                "employees":     info.get("fullTimeEmployees"),
                "country":       info.get("country", ""),
            }
        except Exception as e:
            err = str(e)
            if "429" in err or "Too Many Requests" in err:
                wait = YFINANCE_WAIT * (attempt * 2)   # exponential backoff
                print(f"[yfinance] 429 rate-limit dla {ticker} — czekam {wait:.0f}s (próba {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                print(f"[yfinance] Błąd {ticker}: {e}")
                return None
    print(f"[yfinance] Wszystkie próby wyczerpane dla {ticker}")
    return None


def get_market_data(ticker: str) -> Optional[dict]:
    """Pobiera dane rynkowe z Yahoo Finance z cache'm i ochroną przed rate-limit."""
    global _cache
    now = datetime.now()

    # Sprawdź cache
    if ticker in _cache:
        cached_at, cached_data = _cache[ticker]
        age_min = (now - cached_at).total_seconds() / 60
        if age_min < CACHE_TTL:
            print(f"[cache] Używam cache dla {ticker} (wiek: {age_min:.1f} min, TTL: {CACHE_TTL} min)")
            return cached_data

    # Czekaj chwilę między requestami żeby nie bombardować Yahoo
    time.sleep(YFINANCE_WAIT)

    data = _fetch_yfinance_raw(ticker)
    if data:
        _cache[ticker] = (now, data)
        print(f"[yfinance] Pobrano i zapisano cache dla {ticker}")
    return data


# ─────────────────────────────────────────────────────────────────
# ŹRÓDŁO 2: SEC EDGAR – oficjalne sprawozdania finansowe
# ─────────────────────────────────────────────────────────────────
SEC_CONCEPTS = {
    # Rachunek zysków i strat
    "revenue":         ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gross_profit":    ["GrossProfit"],
    "op_income":       ["OperatingIncomeLoss"],
    "net_income":      ["NetIncomeLoss"],
    "ebitda":          ["EarningsBeforeInterestTaxesDepreciationAndAmortization"],  # rzadko
    "eps_basic":       ["EarningsPerShareBasic"],
    "eps_diluted":     ["EarningsPerShareDiluted"],
    "shares":          ["CommonStockSharesOutstanding"],
    # Bilans
    "total_assets":    ["Assets"],
    "total_liabilities":["Liabilities"],
    "equity":          ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "cash":            ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsAndShortTermInvestments"],
    "long_term_debt":  ["LongTermDebt", "LongTermDebtNoncurrent"],
    "current_assets":  ["AssetsCurrent"],
    "current_liab":    ["LiabilitiesCurrent"],
    # Cash flow
    "cfo":             ["NetCashProvidedByUsedInOperatingActivities"],
    "capex":           ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "free_cf":         ["FreeCashFlow"],  # rzadko bezpośrednio
    "dividends":       ["PaymentsOfDividends"],
}

def get_edgar_facts(cik: str) -> Optional[dict]:
    """
    Pobiera z data.sec.gov companyfacts JSON – oficjalne dane XBRL z 10-K/10-Q.
    Zwraca dict z kluczowymi wartościami finansowymi (ostatni dostępny okres).
    Bez żadnego API key – publiczne API SEC.
    """
    url = f"https://data.sec.gov/api/xbrl/companyfacts/{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"[EDGAR] Błąd pobierania {cik}: {e}")
        return None

    us_gaap = raw.get("facts", {}).get("us-gaap", {})
    result  = {"entity": raw.get("entityName", ""), "cik": cik}

    def latest_annual(concept_list: list) -> Optional[tuple]:
        """
        Zwraca (wartość, okres_str) dla ostatniego rocznego wpisu (10-K).
        Filtruje tylko wpisy z frame pasującym do roku (CY#### lub form 10-K).
        """
        for concept in concept_list:
            if concept not in us_gaap:
                continue
            entries = us_gaap[concept].get("units", {})
            # USD, shares, lub USD/shares
            for unit_type, facts_list in entries.items():
                # Szukaj wpisów z 10-K
                annual = [
                    f for f in facts_list
                    if f.get("form") in ("10-K", "20-F")
                    and f.get("val") is not None
                ]
                if not annual:
                    continue
                annual.sort(key=lambda x: x.get("end", ""), reverse=True)
                latest = annual[0]
                return latest["val"], latest.get("end", ""), unit_type
        return None

    def latest_quarterly(concept_list: list) -> Optional[tuple]:
        """Zwraca ostatni kwartalny wpis (10-Q)."""
        for concept in concept_list:
            if concept not in us_gaap:
                continue
            for unit_type, facts_list in us_gaap[concept].get("units", {}).items():
                qtly = [
                    f for f in facts_list
                    if f.get("form") in ("10-Q", "10-K")
                    and f.get("val") is not None
                ]
                if not qtly:
                    continue
                qtly.sort(key=lambda x: x.get("end", ""), reverse=True)
                return qtly[0]["val"], qtly[0].get("end",""), unit_type
        return None

    # Pobierz każdy concept
    for key, concepts in SEC_CONCEPTS.items():
        data = latest_annual(concepts) or latest_quarterly(concepts)
        if data:
            val, period, unit = data
            result[key]            = val
            result[f"{key}_period"]= period
            result[f"{key}_unit"]  = unit
        else:
            result[key] = None

    # Wylicz free cash flow jeśli brak bezpośredniego
    if result.get("free_cf") is None and result.get("cfo") and result.get("capex"):
        cfo   = result["cfo"]   or 0
        capex = result["capex"] or 0
        result["free_cf"] = cfo - abs(capex)

    # Wylicz marże jeśli mamy dane
    if result.get("revenue") and result.get("net_income"):
        result["net_margin_calc"] = result["net_income"] / result["revenue"]
    if result.get("revenue") and result.get("gross_profit"):
        result["gross_margin_calc"] = result["gross_profit"] / result["revenue"]
    if result.get("revenue") and result.get("op_income"):
        result["op_margin_calc"] = result["op_income"] / result["revenue"]

    # Debt ratio
    if result.get("total_liabilities") and result.get("total_assets"):
        result["debt_ratio"] = result["total_liabilities"] / result["total_assets"]

    # Current ratio
    if result.get("current_assets") and result.get("current_liab") and result["current_liab"]:
        result["current_ratio_calc"] = result["current_assets"] / result["current_liab"]

    return result


def get_recent_filings(cik: str, count: int = 3) -> list[dict]:
    """
    Pobiera metadane ostatnich złożonych formularzy 10-K i 10-Q z submissions API.
    Bez API key.
    """
    url = f"https://data.sec.gov/submissions/{cik}.json"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=15)
        r.raise_for_status()
        data     = r.json()
        filings  = data.get("filings", {}).get("recent", {})
        forms    = filings.get("form", [])
        dates    = filings.get("filingDate", [])
        accs     = filings.get("accessionNumber", [])
        descs    = filings.get("primaryDocDescription", [])
        results  = []
        for form, date, acc, desc in zip(forms, dates, accs, descs):
            if form in ("10-K", "10-Q", "8-K") and len(results) < count:
                results.append({
                    "form": form,
                    "date": date,
                    "acc":  acc,
                    "desc": desc,
                    "url":  f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc.replace('-','')}/",
                })
        return results
    except Exception as e:
        print(f"[EDGAR submissions] Błąd {cik}: {e}")
        return []


# ─────────────────────────────────────────────────────────────────
# EMBED: NOTOWANIE (yfinance)
# ─────────────────────────────────────────────────────────────────
def build_quote_embed(market: dict, cfg: dict) -> discord.Embed:
    cur  = cfg["currency"]
    chg  = market.get("change_pct") or 0
    pos  = chg >= 0
    col  = discord.Color.green() if pos else discord.Color.red()
    arr  = "📈" if pos else "📉"
    sym  = "▲" if pos else "▼"

    price_str = f"{market['price']:.2f} {cur}" if market.get("price") else "brak"
    chg_str   = f"{sym} {'+' if pos else ''}{chg*100:.2f}%"

    embed = discord.Embed(
        title       = f"{arr} {market['name']} ({cfg['ticker']})",
        description = market.get("description") or "Brak opisu.",
        color       = col,
        timestamp   = datetime.now(),
    )
    embed.add_field(name="💰 Cena",              value=f"**{price_str}**\n{chg_str}",                          inline=True)
    embed.add_field(name="🏦 Kapitalizacja",      value=fmt(market.get("market_cap"), cur),                     inline=True)
    embed.add_field(name="📊 P/E · Fwd P/E",     value=f"P/E: {fmt(market.get('pe'))}\nFwd: {fmt(market.get('fwd_pe'))}",  inline=True)

    embed.add_field(name="💼 Przychody · Marże",
                    value=f"{fmt(market.get('revenue'), cur)}\nNetto: {fmt(market.get('margin'), pct=True)} | Brutto: {fmt(market.get('gross_margin'), pct=True)}",
                    inline=True)
    embed.add_field(name="📈 ROE · ROA",
                    value=f"ROE: {fmt(market.get('roe'), pct=True)}\nROA: {fmt(market.get('roa'), pct=True)}",  inline=True)
    embed.add_field(name="🏗️ Bilans",
                    value=f"D/E: {fmt(market.get('dte'))}\nCurrent: {fmt(market.get('current_ratio'))}×",      inline=True)

    embed.add_field(name="💸 FCF · EBITDA",
                    value=f"FCF: {fmt(market.get('free_cf'), cur)}\nEBITDA: {fmt(market.get('ebitda'), cur)}",  inline=True)
    embed.add_field(name="📅 52-tygodniowy",
                    value=f"Max: {fmt(market.get('high52'), cur)}\nMin: {fmt(market.get('low52'), cur)}",       inline=True)
    embed.add_field(name="🔍 Analitycy",
                    value=f"Rek: **{market.get('rec') or 'brak'}**\nCel: {fmt(market.get('target'), cur)}",    inline=True)

    embed.add_field(name="📊 Wolumen",
                    value=f"Dziś: {fmt(market.get('volume'))}\nŚredni: {fmt(market.get('avg_volume'))}",       inline=True)
    embed.add_field(name="🎯 Dyw. · Beta",
                    value=f"Dyw: {fmt(market.get('div'), pct=True)}\nBeta: {fmt(market.get('beta'))}",         inline=True)
    embed.add_field(name="🌍 Sektor",
                    value=f"{market.get('sector','—')}\n{market.get('industry','—')}",                         inline=True)

    embed.set_footer(text=f"Źródło: Yahoo Finance · {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return embed


# ─────────────────────────────────────────────────────────────────
# EMBED: SPRAWOZDANIE SEC EDGAR (surowe dane)
# ─────────────────────────────────────────────────────────────────
def build_edgar_embed(edgar: dict, filings: list[dict], ticker: str) -> discord.Embed:
    """Embed z surowymi danymi z SEC EDGAR (bilans, R/Z, CF)."""
    embed = discord.Embed(
        title       = f"🏛️ Dane SEC EDGAR — {edgar.get('entity', ticker)}",
        description = (
            f"Oficjalne dane finansowe z bazy **SEC EDGAR** (10-K/10-Q).\n"
            f"Brak pośredników, zero opóźnień danych, źródło: rząd USA 🇺🇸"
        ),
        color       = discord.Color.blue(),
        timestamp   = datetime.now(),
    )

    # Rachunek zysków i strat
    rev = edgar.get("revenue")
    gp  = edgar.get("gross_profit")
    op  = edgar.get("op_income")
    ni  = edgar.get("net_income")
    embed.add_field(
        name  = "📋 Rachunek zysków i strat",
        value = (
            f"Przychody: **{fmt_num(rev)}**\n"
            f"Zysk brutto: {fmt_num(gp)}\n"
            f"Zysk oper.: {fmt_num(op)}\n"
            f"Zysk netto: **{fmt_num(ni)}**"
        ),
        inline=True,
    )

    # Marże (wyliczone)
    nm = edgar.get("net_margin_calc")
    gm = edgar.get("gross_margin_calc")
    om = edgar.get("op_margin_calc")
    embed.add_field(
        name  = "📊 Marże (wyliczone)",
        value = (
            f"Brutto: {f'{gm*100:.1f}%' if gm else 'brak'}\n"
            f"Operacyjna: {f'{om*100:.1f}%' if om else 'brak'}\n"
            f"Netto: {f'{nm*100:.1f}%' if nm else 'brak'}"
        ),
        inline=True,
    )

    # EPS
    epsb = edgar.get("eps_basic")
    epsd = edgar.get("eps_diluted")
    shr  = edgar.get("shares")
    embed.add_field(
        name  = "💹 EPS · Akcje",
        value = (
            f"EPS basic: {f'${epsb:.2f}' if epsb else 'brak'}\n"
            f"EPS diluted: {f'${epsd:.2f}' if epsd else 'brak'}\n"
            f"Akcji: {fmt_num(shr).replace('USD','szt.')}"
        ),
        inline=True,
    )

    # Bilans – aktywa
    ta  = edgar.get("total_assets")
    tl  = edgar.get("total_liabilities")
    eq  = edgar.get("equity")
    embed.add_field(
        name  = "🏦 Bilans – Pasywa/Aktywa",
        value = (
            f"Aktywa razem: **{fmt_num(ta)}**\n"
            f"Zobowiązania: {fmt_num(tl)}\n"
            f"Kapitał własny: **{fmt_num(eq)}**"
        ),
        inline=True,
    )

    # Płynność i zadłużenie
    ca  = edgar.get("current_assets")
    cl  = edgar.get("current_liab")
    ltd = edgar.get("long_term_debt")
    cr  = edgar.get("current_ratio_calc")
    dr  = edgar.get("debt_ratio")
    embed.add_field(
        name  = "💳 Płynność · Zadłużenie",
        value = (
            f"Aktywa bieżące: {fmt_num(ca)}\n"
            f"Zob. bieżące: {fmt_num(cl)}\n"
            f"Dług dług.: {fmt_num(ltd)}\n"
            f"Current ratio: {f'{cr:.2f}×' if cr else 'brak'}\n"
            f"Debt ratio: {f'{dr*100:.1f}%' if dr else 'brak'}"
        ),
        inline=True,
    )

    # Cash flow
    cfo    = edgar.get("cfo")
    capex  = edgar.get("capex")
    fcf    = edgar.get("free_cf")
    divs   = edgar.get("dividends")
    embed.add_field(
        name  = "💸 Przepływy pieniężne",
        value = (
            f"CFO (oper.): **{fmt_num(cfo)}**\n"
            f"CAPEX: {fmt_num(capex)}\n"
            f"FCF: **{fmt_num(fcf)}**\n"
            f"Dywidendy: {fmt_num(divs)}"
        ),
        inline=True,
    )

    # Ostatnie złożone raporty
    if filings:
        filings_str = "\n".join(
            f"[{f['form']} – {f['date']}]({f['url']})" for f in filings[:3]
        )
        embed.add_field(name="📁 Ostatnie złożenia (SEC)", value=filings_str, inline=False)

    # Okres danych
    period = edgar.get("revenue_period") or edgar.get("net_income_period") or "nieznany"
    embed.set_footer(
        text=f"Źródło: SEC EDGAR (data.sec.gov) | Bez API key | Okres: {period} · {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )
    return embed


# ─────────────────────────────────────────────────────────────────
# AI: ANALIZA CLAUDE – karmiona danymi z yfinance + SEC EDGAR
# ─────────────────────────────────────────────────────────────────
def generate_ai_report(name: str, ticker: str, market: dict, edgar: Optional[dict], cur: str) -> str:
    """Generuje profesjonalną analizę przez Claude, na podstawie danych z yfinance + SEC EDGAR."""

    edgar_section = ""
    if edgar:
        edgar_section = f"""
DANE Z SEC EDGAR (10-K/10-Q – oficjalne):
- Przychody (roczne): {fmt_num(edgar.get('revenue'))} | Okres: {edgar.get('revenue_period','?')}
- Zysk brutto: {fmt_num(edgar.get('gross_profit'))}
- Zysk operacyjny: {fmt_num(edgar.get('op_income'))}
- Zysk netto: {fmt_num(edgar.get('net_income'))}
- Marża netto: {f"{edgar.get('net_margin_calc',0)*100:.1f}%" if edgar.get('net_margin_calc') else 'brak'}
- EPS (diluted): {edgar.get('eps_diluted','brak')}
- Aktywa razem: {fmt_num(edgar.get('total_assets'))}
- Zobowiązania: {fmt_num(edgar.get('total_liabilities'))}
- Kapitał własny: {fmt_num(edgar.get('equity'))}
- Dług długoterminowy: {fmt_num(edgar.get('long_term_debt'))}
- Current ratio: {f"{edgar.get('current_ratio_calc',0):.2f}" if edgar.get('current_ratio_calc') else 'brak'}
- CFO (operacyjny CF): {fmt_num(edgar.get('cfo'))}
- CAPEX: {fmt_num(edgar.get('capex'))}
- Free Cash Flow: {fmt_num(edgar.get('free_cf'))}
"""
    else:
        edgar_section = "DANE SEC EDGAR: niedostępne (ETF lub spółka zagraniczna – użyto danych yfinance)."

    prompt = f"""Jesteś doświadczonym analitykiem finansowym CFA. Napisz profesjonalne sprawozdanie finansowe w języku POLSKIM dla poniższej spółki, korzystając wyłącznie z podanych danych. Nie wymyślaj danych których nie ma.

SPÓŁKA: {name} ({ticker})
SEKTOR: {market.get('sector','')}/{market.get('industry','')}
KRAJ: {market.get('country','')}
OPIS: {market.get('description','')}

DANE RYNKOWE (Yahoo Finance):
- Cena: {fmt(market.get('price'), cur)} | Zmiana: {f"{(market.get('change_pct') or 0)*100:.2f}%"}
- Kapitalizacja: {fmt(market.get('market_cap'), cur)}
- P/E trailing: {fmt(market.get('pe'))} | P/E forward: {fmt(market.get('fwd_pe'))}
- EPS: {fmt(market.get('eps'), cur)}
- Przychody: {fmt(market.get('revenue'), cur)} | Wzrost r/r: {fmt(market.get('rev_growth'), pct=True)}
- Marża brutto: {fmt(market.get('gross_margin'), pct=True)}
- Marża oper.: {fmt(market.get('op_margin'), pct=True)}
- Marża netto: {fmt(market.get('margin'), pct=True)}
- EBITDA: {fmt(market.get('ebitda'), cur)}
- FCF: {fmt(market.get('free_cf'), cur)}
- Wzrost zysków r/r: {fmt(market.get('earn_growth'), pct=True)}
- ROE: {fmt(market.get('roe'), pct=True)} | ROA: {fmt(market.get('roa'), pct=True)}
- D/E ratio: {fmt(market.get('dte'))}
- Dywidenda: {fmt(market.get('div'), pct=True)} | Beta: {fmt(market.get('beta'))}
- 52-tyg. Max/Min: {fmt(market.get('high52'), cur)}/{fmt(market.get('low52'), cur)}
- Rekomendacja analityków: {market.get('rec','')} | Cel: {fmt(market.get('target'), cur)}
- Pracowników: {f"{market.get('employees',0):,}" if market.get('employees') else 'brak'}
{edgar_section}

Napisz sprawozdanie z DOKŁADNIE tymi sekcjami (użyj tych nagłówków):

📋 **PODSUMOWANIE WYKONANIA**
[2-3 zdania – najważniejsze wyniki, czy spółka rośnie czy spada, kluczowe liczby]

💰 **PRZYCHODY I RENTOWNOŚĆ**
[Analiza przychodów, wzrostu, marż brutto/operacyjnej/netto, porównanie z branżą]

🏦 **SYTUACJA BILANSOWA**
[Aktywa, zobowiązania, kapitał własny, płynność, poziom zadłużenia]

💸 **PRZEPŁYWY PIENIĘŻNE**
[CFO, CAPEX, FCF – czy spółka generuje gotówkę? jakość zysku]

⚠️ **KLUCZOWE RYZYKA**
[3-4 konkretne ryzyka dla inwestora – użyj • jako bullet]

🔭 **PERSPEKTYWY**
[Prognoza krótkookresowa, katalizatory wzrostu, zagrożenia]

⚖️ **VERDICT: [POZYTYWNY / NEUTRALNY / NEGATYWNY / SPEKULACYJNY]**
[Podsumowanie dla inwestora. Dla kogo ta spółka jest odpowiednia?]

---
*Dane: Yahoo Finance + SEC EDGAR (data.sec.gov) · {datetime.now().strftime('%d.%m.%Y %H:%M')} · Nie stanowi porady inwestycyjnej*

Pisz zwięźle i po polsku. Max 550 słów łącznie. Nie wymyślaj liczb których nie ma w danych."""

    try:
        msg = ai.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 1200,
            messages   = [{"role": "user", "content": prompt}],
        )
        return msg.content[0].text
    except Exception as e:
        print(f"[Claude API] Błąd: {e}")
        return f"⚠️ Błąd generowania analizy: {e}"


def build_ai_embed(name: str, ticker: str, report_text: str) -> discord.Embed:
    embed = discord.Embed(
        title       = f"🤖 Analiza AI — {name} ({ticker})",
        description = report_text,
        color       = discord.Color.gold(),
        timestamp   = datetime.now(),
    )
    embed.set_footer(text="Analiza: Claude AI | Dane: Yahoo Finance + SEC EDGAR | Nie stanowi porady inwestycyjnej")
    return embed


# ─────────────────────────────────────────────────────────────────
# GŁÓWNA PĘTLA
# ─────────────────────────────────────────────────────────────────
@tasks.loop(minutes=INTERVAL_MINUTES)
async def send_update():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"[Bot] Kanał {CHANNEL_ID} nie znaleziony")
        return

    history          = load_history()
    name, cfg        = pick_company(history)
    ticker, cik, cur = cfg["ticker"], cfg["cik"], cfg["currency"]
    print(f"[{datetime.now():%H:%M}] Wysyłam: {name} ({ticker})")

    # 1) Dane rynkowe (yfinance) — z cache + retry
    market = get_market_data(ticker)
    if not market:
        # Powiadom na kanale i zapisz w historii żeby nie utknąć w pętli
        err_embed = discord.Embed(
            title       = f"⚠️ Tymczasowy problem z danymi — {name} ({ticker})",
            description = (
                "Yahoo Finance zwróciło błąd rate-limit (429). "
                "Bot automatycznie spróbuje ponownie przy następnej rundzie.

"
                "Jest to normalne przy intensywnym użyciu — dane będą dostępne za chwilę."
            ),
            color       = discord.Color.orange(),
            timestamp   = datetime.now(),
        )
        err_embed.set_footer(text="Bot wznowi działanie automatycznie przy kolejnym interwale")
        await channel.send(embed=err_embed)
        # Zapisz w historii żeby przejść do kolejnej spółki zamiast blokować tę samą
        history[ticker] = datetime.now().isoformat()
        save_history(history)
        print(f"[Bot] ⚠️ Pominięto {ticker} z powodu błędu danych — przejście do następnej spółki")
        return

    # 2) Dane SEC EDGAR (jeśli dostępny CIK)
    edgar   = get_edgar_facts(cik) if cik else None
    filings = get_recent_filings(cik) if cik else []

    # 3) Wyślij: notowanie
    await channel.send(embed=build_quote_embed(market, cfg))

    # 4) Wyślij: dane SEC EDGAR (jeśli dostępne)
    if edgar and cik:
        await channel.send(embed=build_edgar_embed(edgar, filings, ticker))

    # 5) Wyślij: analiza AI
    tmp = await channel.send("⏳ Generuję analizę AI na podstawie danych SEC EDGAR + yfinance...")
    report = generate_ai_report(name, ticker, market, edgar, cur)
    await tmp.delete()
    await channel.send(embed=build_ai_embed(name, ticker, report))

    history[ticker] = datetime.now().isoformat()
    save_history(history)
    print(f"[Bot] ✅ Wysłano komplet dla {name}")


# ─────────────────────────────────────────────────────────────────
# KOMENDY
# ─────────────────────────────────────────────────────────────────
@bot.command(name="spółka", aliases=["spolka", "stock", "notowanie"])
async def cmd_stock(ctx, ticker: str = None):
    """!spółka TICKER – pobierz notowanie"""
    if not ticker:
        return await ctx.send("Użycie: `!spółka TICKER` np. `!spółka NVDA`")
    market = get_market_data(ticker.upper())
    if market:
        cfg = {"ticker": ticker.upper(), "currency": "USD"}
        await ctx.send(embed=build_quote_embed(market, cfg))
    else:
        await ctx.send(f"❌ Brak danych dla `{ticker.upper()}`")

@bot.command(name="edgar")
async def cmd_edgar(ctx, cik: str = None):
    """!edgar CIK – pobierz dane SEC EDGAR dla spółki (np. !edgar 0001045810 = NVIDIA)"""
    if not cik:
        return await ctx.send(
            "Użycie: `!edgar CIK`\nCIK-i:\n"
            "• NVIDIA: `0001045810`\n• Uber: `0001543151`\n"
            "Sprawdź CIK: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        )
    await ctx.send(f"⏳ Pobieram dane SEC EDGAR dla CIK `{cik}`...")
    edgar   = get_edgar_facts(cik)
    filings = get_recent_filings(cik)
    if edgar:
        await ctx.send(embed=build_edgar_embed(edgar, filings, cik))
    else:
        await ctx.send(f"❌ Brak danych EDGAR dla CIK `{cik}`")

@bot.command(name="raport")
async def cmd_report(ctx, ticker: str = None):
    """!raport TICKER – pełna analiza AI (notowanie + EDGAR + AI)"""
    if not ticker:
        return await ctx.send("Użycie: `!raport TICKER` np. `!raport NVDA`")
    ticker = ticker.upper()
    cfg = next(((n, c) for n, c in COMPANIES.items() if c["ticker"] == ticker), None)
    cur = cfg[1]["currency"] if cfg else "USD"
    name = cfg[0] if cfg else ticker
    cik  = cfg[1]["cik"] if cfg else None

    tmp = await ctx.send(f"⏳ Pobieram dane i generuję raport dla `{ticker}`...")
    market = get_market_data(ticker)
    if not market:
        return await tmp.edit(content=f"❌ Brak danych rynkowych dla `{ticker}`")

    edgar   = get_edgar_facts(cik) if cik else None
    filings = get_recent_filings(cik) if cik else []

    await ctx.send(embed=build_quote_embed(market, {"ticker": ticker, "currency": cur}))
    if edgar:
        await ctx.send(embed=build_edgar_embed(edgar, filings, ticker))
    report = generate_ai_report(name, ticker, market, edgar, cur)
    await ctx.send(embed=build_ai_embed(name, ticker, report))
    await tmp.delete()

@bot.command(name="lista")
async def cmd_lista(ctx):
    """!lista – lista śledzonych spółek"""
    lines = [
        f"• **{n}** (`{c['ticker']}`){' | SEC EDGAR ✓' if c['cik'] else ' | yfinance only'}"
        for n, c in COMPANIES.items()
    ]
    embed = discord.Embed(
        title       = "📋 Śledzone spółki",
        description = "\n".join(lines),
        color       = discord.Color.blue(),
    )
    embed.add_field(
        name  = "Źródła danych",
        value = "📊 Yahoo Finance – ceny & metryki rynkowe\n🏛️ SEC EDGAR – bilanse, R/Z, CF (bez API key)\n🤖 Claude AI – analiza i komentarz",
        inline=False,
    )
    embed.set_footer(text=f"Aktualizacje co {INTERVAL_MINUTES} min")
    await ctx.send(embed=embed)

@bot.command(name="historia")
async def cmd_historia(ctx):
    history = load_history()
    lines   = []
    for name, cfg in COMPANIES.items():
        last = history.get(cfg["ticker"])
        ago  = int((datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60) if last else None
        lines.append(f"• **{name}** — {f'{ago} min temu' if ago is not None else 'jeszcze nie wysłano'}")
    await ctx.send(embed=discord.Embed(
        title       = "🕒 Historia wysyłania",
        description = "\n".join(lines),
        color       = discord.Color.gold(),
    ))

@bot.command(name="pomoc", aliases=["help"])
async def cmd_help(ctx):
    embed = discord.Embed(title="📖 Komendy FinanceBot", color=discord.Color.blurple())
    embed.add_field(name="!lista",              value="Lista śledzonych spółek i źródeł danych", inline=False)
    embed.add_field(name="!spółka TICKER",      value="Notowanie dla dowolnej spółki (np. `!spółka AAPL`)", inline=False)
    embed.add_field(name="!edgar CIK",          value="Dane SEC EDGAR (bilans, R/Z, CF) dla spółki USA", inline=False)
    embed.add_field(name="!raport TICKER",      value="Pełna analiza: notowanie + EDGAR + AI (np. `!raport NVDA`)", inline=False)
    embed.add_field(name="!historia",           value="Kiedy ostatnio wysłano dane dla każdej spółki", inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f"✅ {bot.user} zalogowany")
    print(f"📡 Interwał: co {INTERVAL_MINUTES} min")
    print(f"🏢 Spółki: {', '.join(COMPANIES.keys())}")
    print(f"🏛️ Źródła: Yahoo Finance + SEC EDGAR (bez API key) + Claude AI")
    if not send_update.is_running():
        send_update.start()

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
