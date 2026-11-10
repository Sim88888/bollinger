#!/usr/bin/env python3
"""Récupère les cours Yahoo Finance des actifs de assets_v3.json -> data.json + data.js (lu par index.html).
Lancé par GitHub Actions toutes les 30 minutes en séance : mise à jour LÉGÈRE (1 mois de cours, fusionné à l'historique) des cours
vieux de plus de REFRESH_MIN minutes ; historique COMPLET (2 ans) une fois par jour. Le dernier point d'un cours est le cours en direct de Yahoo.
Yahoo limite les requêtes par adresse IP (erreur 429) : requêtes espacées, abandon rapide si l'IP est bloquée.
Le script enregistre ce qu'il a obtenu au fil de l'eau et s'arrête de lui-même après un budget de temps (BUDGET_S, 480 s par défaut) :
un passage incomplet laisse donc des cours utilisables, et le passage suivant reprend par les actifs manquants."""
import json, os, time, random, datetime, urllib.request, urllib.parse, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
FX = "EURUSD=X"
REFRESH_MIN = int(os.environ.get("REFRESH_MIN", "20"))   # un cours plus récent que cela n'est pas redemandé
MAX_AGE = REFRESH_MIN * 60
FULL_EVERY = 7 * 86400                                    # historique complet (2 ans) au plus une fois par semaine
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "12"))    # au plus 12 actifs par passage (les plus anciens d'abord) : moins de requêtes d'un coup = moins de blocages Yahoo
STOP_AFTER = 3                                            # arrêt après 3 échecs consécutifs : on ne s'acharne pas sur une adresse limitée
BUDGET_S = int(os.environ.get("BUDGET_S", "480"))
SAVE_EVERY = 4
UAS = [
 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
 "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
 "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]
BACKOFF = (8, 15, 25)        # attentes (s) après un 429 : au plus 3 essais par actif

class Blocked(Exception): pass

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": random.choice(UAS), "Accept": "application/json,text/plain,*/*",
                                               "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8", "Referer": "https://finance.yahoo.com/"})
    return urllib.request.urlopen(req, timeout=timeout)

def fetch(ticker, rng="2y"):
    last = None
    for attempt in range(len(BACKOFF)):
        host = ("query1", "query2")[attempt % 2]
        url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d&events=history&includeAdjustedClose=true"
               % (host, urllib.parse.quote(ticker), rng))
        try:
            q = json.loads(_get(url).read())["chart"]["result"][0]
            ind = q["indicators"]
            c = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
            series = [[t, round(p, 6)] for t, p in zip(q.get("timestamp") or [], c) if p is not None]
            if not series: raise ValueError("aucune donnée")
            return series
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429: time.sleep(BACKOFF[attempt])
            elif e.code == 404:
                try: body = e.read().decode("utf-8", "ignore").replace("\n", " ")[:160]
                except Exception: body = ""
                raise ValueError("ticker inconnu (404) %s" % body)
            else: time.sleep(3)
        except Exception as e:
            last = e; time.sleep(3)
    if isinstance(last, urllib.error.HTTPError) and last.code == 429:
        raise Blocked(str(last))
    raise last

def merge(old, new):
    """Raccorde les cours récents (new) à l'historique (old) : les nouveaux cours sont remis à l'échelle de l'historique
    à partir d'un jour commun déjà terminé. Renvoie None si le raccord est impossible (-> historique complet)."""
    if not old or not new: return None
    olddict = {t: p for t, p in old}
    ref = None
    for t, p in reversed(new):
        if t < old[-1][0] and t in olddict and p:
            ref = olddict[t] / p; break
    if ref is None or not (0.5 < ref < 2): return None
    keep = [b for b in old if b[0] < new[0][0]]
    return keep + [[t, round(p * ref, 6)] for t, p in new]

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
    stale = sorted([t for t in tickers if t not in missing and now - data[t].get("ts", 0) > MAX_AGE], key=lambda t: data[t].get("ts", 0))   # les plus vieux d'abord
    todo = (missing + stale)[:MAX_PER_RUN]                   # les cours manquants passent avant les cours à rafraîchir
    print("%d ticker(s) à traiter (%d manquants, %d à rafraîchir) sur %d — budget %d s" % (len(todo), len(missing), len(stale), len(tickers), BUDGET_S))
    got, blocked_streak, since_save, stopped, fails = 0, 0, 0, "", []
    for t in todo:
        if time.time() - start > BUDGET_S:
            stopped = "budget de temps atteint"; break
        try:
            old = data.get(t, {})
            full_ts = (old.get("full_ts") or time.time()) if "series" in old else 0      # historique existant : considéré comme complet
            series, mode = None, "complet"
            if "series" in old and time.time() - full_ts < FULL_EVERY:      # mise à jour légère : quelques semaines de cours fusionnées à l'historique
                age_j = (time.time() - old["series"][-1][0]) / 86400
                rng = "1mo" if age_j < 24 else "3mo" if age_j < 80 else "6mo" if age_j < 170 else "2y"
                try:
                    series = merge(old["series"], fetch(t, rng))
                except ValueError:                           # un actif déjà connu qui répond « inconnu » (404) : Yahoo limite l'adresse, on ne redemande pas
                    raise Blocked("réponse 404 sur un actif connu (limitation Yahoo)")
                mode = "léger"
            if series is None:
                try:
                    series = fetch(t)
                except ValueError:                           # ticker inconnu : on essaie les tickers de secours
                    for alt in alts.get(t, []):
                        try:
                            series = fetch(alt); print("      (secours %s utilisé pour %s)" % (alt, t)); break
                        except ValueError: continue
                    else: raise
                mode = "complet"
            data[t] = {"series": series, "ts": int(time.time()), "full_ts": int(time.time()) if mode == "complet" else int(full_ts)}
            got += 1; since_save += 1; blocked_streak = 0
            print("OK    ", t, "(%s)" % mode)
        except Blocked as e:
            blocked_streak += 1
            print("BLOQUÉ", t, e); fails.append("%s: %s" % (t, e))
            if blocked_streak >= STOP_AFTER:
                stopped = "Yahoo refuse cette adresse IP"; break
        except Exception as e:
            print("ECHEC ", t, e); fails.append("%s: %s" % (t, e))
            data.setdefault(t, {"error": str(e)})
            if "series" in data[t]:                          # actif déjà connu qui échoue : compte comme un blocage
                blocked_streak += 1
                if blocked_streak >= STOP_AFTER:
                    stopped = "échecs consécutifs"; break
        if since_save >= SAVE_EVERY:                         # enregistrement intermédiaire
            save(cfg, data); since_save = 0
        time.sleep(random.uniform(1.5, 3.0))
    have = sum(1 for v in data.values() if "series" in v)
    print("%d/%d tickers disponibles (%d mis à jour)%s" % (have, len(tickers), got, " — arrêt : " + stopped + " (reprise au prochain passage)" if stopped else ""))
    absent = [t for t in tickers if "series" not in data.get(t, {})]
    if absent: print("Sans cours pour l'instant :", ", ".join(absent))
    write_summary(have, len(tickers), got, absent, stopped, max((v.get("ts", 0) for v in data.values()), default=0))
    try:    # fichier de diagnostic enregistré à chaque passage (lisible dans le dépôt, sans ouvrir les journaux)
        json.dump({"passage": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "mis_a_jour": got, "disponibles": have,
                   "total": len(tickers), "arret": stopped or None, "erreurs": fails[:8], "sans_cours": absent},
                  open(os.path.join(HERE, "statut.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception: pass
    if got or (config_changed and have):
        save(cfg, data)
    elif have == 0:
        raise SystemExit("Aucun cours disponible pour l'instant.")

if __name__ == "__main__": main()
