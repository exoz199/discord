# 📊 FinanceBot — Discord Bot Finansowy
> Notowania giełdowe + oficjalne sprawozdania SEC EDGAR + analiza AI — co 15 minut, automatycznie.

---

## 🗂️ Pliki w tym folderze

| Plik | Opis |
|------|------|
| `bot.py` | Główny kod bota (uruchamiasz ten plik) |
| `requirements.txt` | Biblioteki Python do zainstalowania |
| `README.md` | Ten plik — instrukcja krok po kroku |

---

## ⚙️ Jak to działa

Co 15 minut bot wysyła **3 wiadomości** dla kolejnej spółki w rotacji:

```
📈 Wiadomość 1 — Notowanie (Yahoo Finance)
   Cena, zmiana %, P/E, EPS, marże, ROE, wolumen, rekomendacje

🏛️ Wiadomość 2 — Dane SEC EDGAR (rząd USA, bez API key)
   Bilans, rachunek zysków i strat, przepływy pieniężne, EPS z 10-K/10-Q

🤖 Wiadomość 3 — Analiza AI (Claude)
   Profesjonalne sprawozdanie wygenerowane na podstawie powyższych danych
```

Śledzone spółki (rotacja bez powtórzeń):
- **S&P 500 (SPY)** — największy ETF świata
- **NVIDIA (NVDA)** — lider AI / GPU
- **Uber (UBER)** — mobility / delivery
- **CD Projekt (CDR.WA)** — polskie AAA studio

---

## 🚀 Instrukcja instalacji (krok po kroku)

### Krok 1 — Zainstaluj Python

Pobierz Python 3.11+ ze strony https://python.org/downloads
- ✅ Zaznacz opcję **"Add Python to PATH"** podczas instalacji
- Sprawdź instalację: otwórz terminal i wpisz `python --version`

---

### Krok 2 — Stwórz bota Discord

1. Wejdź na **https://discord.com/developers/applications**
2. Kliknij **New Application** → nadaj dowolną nazwę (np. "FinanceBot")
3. W lewym menu kliknij **Bot**
4. Kliknij **Add Bot** → potwierdź
5. W sekcji **TOKEN** kliknij **Reset Token** → **skopiuj token** (zapisz go!)
6. Niżej włącz opcję **Message Content Intent** (przełącznik na zielony)
7. Kliknij **Save Changes**

**Dodaj bota do serwera:**
1. W lewym menu kliknij **OAuth2** → **URL Generator**
2. Zaznacz w SCOPES: ✅ `bot`
3. Zaznacz w BOT PERMISSIONS: ✅ `Send Messages`, ✅ `Embed Links`, ✅ `Read Messages/View Channels`, ✅ `Manage Messages`
4. Skopiuj wygenerowany URL na dole → otwórz go w przeglądarce → dodaj bota do swojego serwera

---

### Krok 3 — Pobierz ID kanału Discord

1. W Discord otwórz **Ustawienia użytkownika** (ikonka ⚙️)
2. Idź do **Zaawansowane** → włącz **Tryb dewelopera**
3. Wróć na serwer, **prawy klik na kanał** (np. #notowania) → **Kopiuj ID kanału**

---

### Krok 4 — Pobierz klucz API Anthropic (do analizy AI)

1. Wejdź na **https://console.anthropic.com**
2. Zarejestruj się (darmowe konto, pierwsze $5 gratis)
3. W menu kliknij **API Keys** → **Create Key**
4. Skopiuj klucz (zaczyna się od `sk-ant-...`)

---

### Krok 5 — Skonfiguruj bota

Otwórz plik `bot.py` w dowolnym edytorze (np. Notatnik, VS Code) i zmień te 3 linie na górze:

```python
BOT_TOKEN     = "TWOJ_TOKEN_BOTA"       # ← wklej token z Kroku 2
CHANNEL_ID    = 123456789012345678       # ← wklej ID kanału z Kroku 3 (sama liczba, bez cudzysłowów)
ANTHROPIC_KEY = "TWOJ_KLUCZ_ANTHROPIC"  # ← wklej klucz z Kroku 4
```

**Przykład po wypełnieniu:**
```python
BOT_TOKEN     = "MTA4NjY4OTQ2MDQ3NzQ4NTY4.GfxKpQ.abc123xyz"
CHANNEL_ID    = 1186689460477485234
ANTHROPIC_KEY = "sk-ant-api03-abc123..."
```

---

### Krok 6 — Zainstaluj biblioteki

Otwórz terminal (cmd / PowerShell na Windows, Terminal na Mac/Linux) w folderze z plikami i wpisz:

```bash
pip install -r requirements.txt
```

Poczekaj aż się zainstaluje (ok. 1-2 minuty).

---

### Krok 7 — Uruchom bota

```bash
python bot.py
```

Powinieneś zobaczyć w terminalu:
```
✅ FinanceBot#1234 zalogowany
📡 Interwał: co 15 min
🏢 Spółki: S&P 500 (SPY), NVIDIA, Uber, CD Projekt
🏛️ Źródła: Yahoo Finance + SEC EDGAR (bez API key) + Claude AI
```

Bot wyśle pierwszą wiadomość za 15 minut. Żeby przetestować od razu, wpisz na Discordzie:
```
!raport NVDA
```

---

## 🤖 Komendy Discord

Wpisuj je na dowolnym kanale gdzie jest bot:

| Komenda | Opis |
|---------|------|
| `!lista` | Lista śledzonych spółek i źródeł danych |
| `!spółka TICKER` | Notowanie dla dowolnej spółki, np. `!spółka AAPL` |
| `!edgar CIK` | Dane SEC EDGAR (bilans/R&Z/CF), np. `!edgar 0001045810` |
| `!raport TICKER` | Pełna analiza: notowanie + EDGAR + AI, np. `!raport NVDA` |
| `!historia` | Kiedy ostatnio wysłano dane dla każdej spółki |
| `!pomoc` | Lista wszystkich komend |

---

## 📋 Jak zmienić śledzone spółki

W pliku `bot.py` znajdź sekcję `COMPANIES` i edytuj:

```python
COMPANIES = {
    "S&P 500 (SPY)": {
        "ticker":   "SPY",
        "cik":      None,        # None = brak danych SEC EDGAR (ETF/zagraniczne)
        "currency": "USD",
    },
    "NVIDIA": {
        "ticker":   "NVDA",
        "cik":      "0001045810",  # CIK z SEC EDGAR (tylko spółki USA)
        "currency": "USD",
    },
    # Dodaj własną spółkę:
    "Apple": {
        "ticker":   "AAPL",
        "cik":      "0000320193",  # znajdź na sec.gov
        "currency": "USD",
    },
}
```

**Jak znaleźć CIK dla spółki USA:**
Wejdź na: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=NAZWA&CIK=&type=10-K`

**Popularne tickery GPW (Polska):**
| Spółka | Ticker | CIK |
|--------|--------|-----|
| PKN Orlen | `PKN.WA` | `None` (polska) |
| KGHM | `KGH.WA` | `None` |
| CD Projekt | `CDR.WA` | `None` |
| Allegro | `ALE.WA` | `None` |
| PKO BP | `PKO.WA` | `None` |

**Popularne spółki USA z CIK:**
| Spółka | Ticker | CIK |
|--------|--------|-----|
| Apple | `AAPL` | `0000320193` |
| Microsoft | `MSFT` | `0000789019` |
| Tesla | `TSLA` | `0001318605` |
| Meta | `META` | `0001326801` |
| Alphabet | `GOOGL` | `0001652044` |
| Amazon | `AMZN` | `0001018724` |

---

## ☁️ Hosting — żeby bot działał 24/7

Terminal musisz mieć cały czas otwarty — bot wyłącza się jak zamkniesz okno. Żeby działał non-stop:

### Opcja A — Railway.app (zalecana, darmowa)
1. Wejdź na **https://railway.app** → zaloguj się przez GitHub
2. Kliknij **New Project** → **Deploy from GitHub repo**
3. Wgraj pliki `bot.py` i `requirements.txt` na GitHub (lub użyj "Empty Project" i wgraj ręcznie)
4. W Railway ustaw zmienne środowiskowe (Settings → Variables):
   - `BOT_TOKEN` = twój token
   - `CHANNEL_ID` = ID kanału
   - `ANTHROPIC_KEY` = klucz Anthropic
5. Zmień w `bot.py` te linie żeby czytało z env:
```python
import os
BOT_TOKEN     = os.environ["BOT_TOKEN"]
CHANNEL_ID    = int(os.environ["CHANNEL_ID"])
ANTHROPIC_KEY = os.environ["ANTHROPIC_KEY"]
```

### Opcja B — VPS (Hetzner, DigitalOcean)
Hetzner CX11 = ~4€/mies. Wgraj pliki przez SSH i uruchom:
```bash
pip install -r requirements.txt
nohup python bot.py &    # działa w tle nawet po zamknięciu SSH
```

### Opcja C — Raspberry Pi (dom)
Jeśli masz RPi podłączone do prądu i internetu:
```bash
pip3 install -r requirements.txt
python3 bot.py
```

---

## 🔧 Rozwiązywanie problemów

| Problem | Rozwiązanie |
|---------|-------------|
| `Token inwalid` | Sprawdź czy token jest skopiowany poprawnie (bez spacji) |
| `Kanał nie znaleziony` | Sprawdź czy CHANNEL_ID to sama liczba (nie string) i czy bot ma dostęp do kanału |
| `anthropic.AuthenticationError` | Sprawdź klucz Anthropic, czy masz środki na koncie |
| `No module named 'discord'` | Uruchom `pip install -r requirements.txt` |
| Bot nie wysyła po 15 min | Sprawdź czy terminal jest otwarty i czy nie ma błędu w konsoli |
| Brak danych EDGAR | Normalne dla ETF (SPY) i spółek zagranicznych (CDR.WA) — bot użyje samego yfinance |

---

## 📡 Źródła danych — szczegóły techniczne

### Yahoo Finance (yfinance)
- Biblioteka Python, **bez rejestracji, bez API key**
- Dane: cena, zmiana, kapitalizacja, P/E, EPS, marże, ROE, wolumen, rekomendacje, cel cenowy
- Aktualizacja: real-time z 15-minutowym opóźnieniem (bezpłatna wersja)

### SEC EDGAR (data.sec.gov)
- Oficjalne API Komisji Papierów Wartościowych USA, **bez API key, bez rejestracji**
- Endpoint: `https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json`
- Dane: pełne sprawozdania 10-K (roczne) i 10-Q (kwartalne) w formacie XBRL
- Zawiera: bilans, rachunek zysków i strat, przepływy pieniężne, EPS
- Dostępne tylko dla spółek notowanych w USA (NYSE, NASDAQ)

### Claude AI (Anthropic)
- Model: `claude-sonnet-4-6`
- Generuje analizę na podstawie danych z Yahoo Finance + SEC EDGAR
- Koszt: ~$0.01–0.03 per raport (przy standardowym użyciu: ~$2–5/miesiąc)

---

*FinanceBot · Nie stanowi porady inwestycyjnej*
