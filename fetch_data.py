#!/usr/bin/env python3
"""Récupère les cours Yahoo Finance des actifs de assets_v3.json -> data.json + data.js (lu par index.html).
Lancé par GitHub Actions plusieurs fois par jour : ne retélécharge que les cours manquants ou vieux de plus de 20 h.
Yahoo limite les requêtes par adresse IP (erreur 429) : requêtes espacées, abandon rapide si l'IP est bloquée.
Le script enregistre ce qu'il a obtenu au fil de l'eau et s'arrête de lui-même après un budget de temps (BUDGET_S, 480 s par défaut) :
un passage incomplet laisse donc des cours utilisables, et le passage suivant reprend par les actifs manquants."""
import json, os, time, random, datetime, urllib.request, urllib.parse, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
FX = "EURUSD=X"
MAX_AGE = 20 * 3600
BUDGET_S = int(os.environ.get("BUDGET_S", "480"))
SAVE_EVERY = 4
UAS = [
 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
 "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]
BACKOFF = (8, 15, 25)        # attentes (s) après un 429 : au plus 3 essais par actif

class Blocked(Exception): pass

def fetch(ticker):
    last = None
    for attempt in range(len(BACKOFF)):
        host = ("query1", "query2")[attempt % 2]
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=2y&interval=1d&events=history&includeAdjustedClose=true"
               % (host, urllib.parse.quote(ticker)))
        req = urllib.request.Request(url, headers={
            "User-Agent": random.choice(UAS), "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8", "Referer": "https://finance.yahoo.com/"})
        try:
            q = json.loads(urllib.request.urlopen(req, timeout=20).read())["chart"]["result"][0]
            ind = q["indicators"]
            c = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
            series = [[t, round(p, 6)] for t, p in zip(q.get("timestamp") or [], c) if p is not None]
            if not series: raise ValueError("aucune donnée")
            return series
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429: time.sleep(BACKOFF[attempt])
            elif e.code == 404: raise ValueError("ticker inconnu (404)")
            else: time.sleep(3)
        except Exception as e:
            last = e; time.sleep(3)
    if isinstance(last, urllib.error.HTTPError) and last.code == 429:
        raise Blocked(str(last))
    raise last

def save(cfg, data):
    last = max((v.get("ts", 0) for v in data.values()), default=0) or time.time()
    out = {"updated": datetime.datetime.fromtimestamp(last, datetime.timezone.utc).isoformat(timespec="seconds"),
           "config": cfg, "data": data}
    json.dump(out, open(os.path.join(HERE, "data.json"), "w", encoding="utf-8"), separators=(",", ":"))
    open(os.path.join(HERE, "data.js"), "w", encoding="utf-8").write("window.QUOTES=" + json.dumps(out, separators=(",", ":")) + ";")

def write_summary(have, total, got, absent, stopped, last_ts):
    """Résumé affiché en haut de la page du passage (onglet Actions › le passage › Summary)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path: return
    try:
        lines = ["### Cours : %d/%d actifs disponibles (%d mis à jour pendant ce passage)" % (have, total, got)]
        if last_ts: lines.append("Dernier téléchargement enregistré : %s UTC" % datetime.datetime.fromtimestamp(last_ts, datetime.timezone.utc).strftime("%d/%m/%Y %H:%M"))
        if stopped: lines.append("Arrêt anticipé : **%s** (reprise au prochain passage)." % stopped)
        if absent: lines.append("Sans cours pour l'instant : `%s`" % "`, `".join(absent))
        open(path, "a", encoding="utf-8").write("\n\n".join(lines) + "\n")
    except Exception: pass

def main():
    start = time.time()
    cfg = json.load(open(os.path.join(HERE, "assets_v3.json"), encoding="utf-8"))
    alts = {a["ticker"]: a.get("alt", []) for a in cfg["canaries"] + cfg["offensive"]}
    # ordre de priorité : taux de change et canaris d'abord, puis les actifs de la liste dans l'ordre
    tickers = [FX] + [a["ticker"] for a in cfg["canaries"]] + [a["ticker"] for a in cfg["offensive"]]
    try: prev = json.load(open(os.path.join(HERE, "data.json"), encoding="utf-8"))
    except Exception: prev = {}
    data = {t: v for t, v in prev.get("data", {}).items() if t in tickers}
    config_changed = prev.get("config") != cfg
    now = time.time()
    missing = [t for t in tickers if "series" not in data.get(t, {})]
    stale = sorted([t for t in tickers if t not in missing and now - data[t].get("ts", 0) > MAX_AGE], key=lambda t: data[t].get("ts", 0))
    todo = missing + stale                                   # les cours manquants passent avant les cours à rafraîchir
    print("%d ticker(s) à traiter (%d manquants, %d à rafraîchir) sur %d — budget %d s" % (len(todo), len(missing), len(stale), len(tickers), BUDGET_S))
    got, blocked_streak, since_save, stopped = 0, 0, 0, ""
    for t in todo:
        if time.time() - start > BUDGET_S:
            stopped = "budget de temps atteint"; break
        try:
            try:
                series = fetch(t)
            except ValueError:                               # ticker inconnu : on essaie les tickers de secours
                for alt in alts.get(t, []):
                    try:
                        series = fetch(alt); print("      (secours %s utilisé pour %s)" % (alt, t)); break
                    except ValueError: continue
                else: raise
            data[t] = {"series": series, "ts": int(time.time())}
            got += 1; since_save += 1; blocked_streak = 0
            print("OK    ", t)
        except Blocked as e:
            blocked_streak += 1
            print("BLOQUÉ", t, e)
            if blocked_streak >= 2:
                stopped = "Yahoo refuse cette adresse IP"; break
        except Exception as e:
            print("ECHEC ", t, e)
            data.setdefault(t, {"error": str(e)})
        if since_save >= SAVE_EVERY:                         # enregistrement intermédiaire
            save(cfg, data); since_save = 0
        time.sleep(random.uniform(1.5, 3.0))
    have = sum(1 for v in data.values() if "series" in v)
    print("%d/%d tickers disponibles (%d mis à jour)%s" % (have, len(tickers), got, " — arrêt : " + stopped + " (reprise au prochain passage)" if stopped else ""))
    absent = [t for t in tickers if "series" not in data.get(t, {})]
    if absent: print("Sans cours pour l'instant :", ", ".join(absent))
    write_summary(have, len(tickers), got, absent, stopped, max((v.get("ts", 0) for v in data.values()), default=0))
    if got or (config_changed and have):
        save(cfg, data)
    elif have == 0:
        raise SystemExit("Aucun cours disponible pour l'instant.")

if __name__ == "__main__": main()
