import os
import requests
import numpy as np
import yfinance as yf

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def wma(series, period):
    weights = np.arange(1, period + 1)
    return series.rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def detect_order_blocks(df, lookback=3, move_threshold=0.0015):
    df = df.copy()
    df["Bullish"] = df["Close"] > df["Open"]
    df["OB_Type"] = None
    for i in range(len(df) - lookback):
        bougie = df.iloc[i]
        move = df["Close"].iloc[i+1:i+1+lookback].max() - bougie["Close"]
        move_down = bougie["Close"] - df["Close"].iloc[i+1:i+1+lookback].min()
        if not bougie["Bullish"] and move > move_threshold:
            df.iloc[i, df.columns.get_loc("OB_Type")] = "OB_HAUSSIER"
        elif bougie["Bullish"] and move_down > move_threshold:
            df.iloc[i, df.columns.get_loc("OB_Type")] = "OB_BAISSIER"
    return df


def detect_liquidity_sweep(df, window=10):
    df = df.copy()
    df["Liquidity_Sweep"] = None
    for i in range(window, len(df) - 1):
        recent_high = df["High"].iloc[i-window:i].max()
        recent_low = df["Low"].iloc[i-window:i].min()
        if df["High"].iloc[i] > recent_high and df["Close"].iloc[i] < recent_high:
            df.iloc[i, df.columns.get_loc("Liquidity_Sweep")] = "SWEEP_HAUT"
        elif df["Low"].iloc[i] < recent_low and df["Close"].iloc[i] > recent_low:
            df.iloc[i, df.columns.get_loc("Liquidity_Sweep")] = "SWEEP_BAS"
    return df


def detect_rejection_engulfing(df, min_range=0.0002):
    df = df.copy()
    df["Rejet_Haussier"] = False
    df["Rejet_Baissier"] = False
    df["Englobante_Haussiere"] = False
    df["Englobante_Baissiere"] = False
    for i in range(1, len(df)):
        prev = df.iloc[i-1]
        curr = df.iloc[i]
        range_total = curr["High"] - curr["Low"]
        if range_total < min_range:
            continue
        corps_bas = min(curr["Open"], curr["Close"])
        corps_haut = max(curr["Open"], curr["Close"])
        meche_basse = corps_bas - curr["Low"]
        meche_haute = curr["High"] - corps_haut
        if meche_basse / range_total > 0.55:
            df.iloc[i, df.columns.get_loc("Rejet_Haussier")] = True
        if meche_haute / range_total > 0.55:
            df.iloc[i, df.columns.get_loc("Rejet_Baissier")] = True
        if curr["Close"] > curr["Open"] and curr["Close"] > prev["Open"] and curr["Open"] < prev["Close"]:
            df.iloc[i, df.columns.get_loc("Englobante_Haussiere")] = True
        if curr["Close"] < curr["Open"] and curr["Open"] > prev["Close"] and curr["Close"] < prev["Open"]:
            df.iloc[i, df.columns.get_loc("Englobante_Baissiere")] = True
    return df


def detect_bos(df, lookback=20):
    df = df.copy()
    df["BOS_Haussier"] = False
    df["BOS_Baissier"] = False
    for i in range(lookback, len(df)):
        sommet_recent = df["High"].iloc[i-lookback:i].max()
        creux_recent = df["Low"].iloc[i-lookback:i].min()
        if df["Close"].iloc[i] > sommet_recent:
            df.iloc[i, df.columns.get_loc("BOS_Haussier")] = True
        elif df["Close"].iloc[i] < creux_recent:
            df.iloc[i, df.columns.get_loc("BOS_Baissier")] = True
    return df


def verifier_extension(df, prix_zone_origine, direction="LONG", max_extension_pct=0.5):
    prix_actuel = df["Close"].iloc[-1]
    if direction == "LONG":
        plus_bas_recent = df["Low"].iloc[-20:].min()
        amplitude_totale = prix_actuel - plus_bas_recent
        distance_depuis_zone = prix_actuel - prix_zone_origine
    else:
        plus_haut_recent = df["High"].iloc[-20:].max()
        amplitude_totale = plus_haut_recent - prix_actuel
        distance_depuis_zone = prix_zone_origine - prix_actuel
    if amplitude_totale == 0:
        return {"trop_etendu": False, "raison": "Amplitude nulle"}
    ratio_extension = distance_depuis_zone / amplitude_totale
    trop_etendu = ratio_extension > (1 - max_extension_pct) if amplitude_totale > 0 else False
    return {"trop_etendu": trop_etendu, "raison": "Marché trop étendu - NO TRADE (anti-FOMO)" if trop_etendu else "OK"}


def analyser_dernier_setup_complet(df, direction="LONG", max_extension_pct=0.5, fenetre_liquidite=5):
    derniere = df.iloc[-1]
    raisons_no_trade = []
    position_ok = (derniere["Position"] == "AU-DESSUS" and direction == "LONG") or \
                  (derniere["Position"] == "EN-DESSOUS" and direction == "SHORT")
    if not position_ok:
        raisons_no_trade.append("Contexte WMA50 non cohérent")
    sweep_recherche = "SWEEP_BAS" if direction == "LONG" else "SWEEP_HAUT"
    liquidite_ok = (df["Liquidity_Sweep"].iloc[-fenetre_liquidite:] == sweep_recherche).any()
    if not liquidite_ok:
        raisons_no_trade.append("Pas de prise de liquidité cohérente récente")
    ob_type_recherche = "OB_HAUSSIER" if direction == "LONG" else "OB_BAISSIER"
    obs_pertinents = df[df["OB_Type"] == ob_type_recherche]
    zone_origine = obs_pertinents["Close"].iloc[-1] if len(obs_pertinents) > 0 else None
    if zone_origine is None:
        raisons_no_trade.append("Aucun Order Block pertinent trouvé")
    rejet_ok = (derniere["Rejet_Haussier"] and direction == "LONG") or \
               (derniere["Rejet_Baissier"] and direction == "SHORT")
    englobante_ok = (derniere["Englobante_Haussiere"] and direction == "LONG") or \
                     (derniere["Englobante_Baissiere"] and direction == "SHORT")
    bos_ok = False
    trop_etendu = False
    if rejet_ok and not englobante_ok:
        bos_ok = (derniere["BOS_Haussier"] and direction == "LONG") or \
                 (derniere["BOS_Baissier"] and direction == "SHORT")
        if not bos_ok:
            raisons_no_trade.append("Pas d'englobante et pas de BOS propre")
        elif zone_origine is not None:
            ext = verifier_extension(df, prix_zone_origine=zone_origine, direction=direction, max_extension_pct=max_extension_pct)
            trop_etendu = ext["trop_etendu"]
            if trop_etendu:
                raisons_no_trade.append(ext["raison"])
    elif not rejet_ok:
        raisons_no_trade.append("Pas de rejet valide")
    setup_valide = rejet_ok and (englobante_ok or (bos_ok and not trop_etendu))
    setup = {
        "direction": direction,
        "date": str(df.index[-1]),
        "prix_actuel": derniere["Close"],
        "position_wma_ok": position_ok,
        "liquidite_ok": liquidite_ok,
        "zone_origine": zone_origine,
        "rejet_ok": rejet_ok,
        "englobante_ok": englobante_ok,
        "bos_ok": bos_ok,
        "trop_etendu": trop_etendu,
        "raisons_no_trade": raisons_no_trade
    }
    if not position_ok or not liquidite_ok or not setup_valide or len(raisons_no_trade) > 0:
        setup["classification"] = "NO_TRADE"
    else:
        setup["classification"] = "A+" if englobante_ok else "A"
    return setup


def envoyer_alerte(setup, actif="EURUSD"):
    if setup["classification"] in ["A+", "A"]:
        action = "BUY" if setup["direction"] == "LONG" else "SELL"
        message = f"""🔔 ALERTE — EG FINANCE

📊 Actif : {actif}
📈 {action}
🟡 Entrée : {setup['prix_actuel']:.5f}

⭐ Qualité : {setup['classification']}

❌ SL : à définir selon l'OB

⚠️ Gestion du risque obligatoire
📌 Suivre le plan, pas les émotions
"""
    else:
        message = f"❌ NO TRADE ({actif} / {setup['direction']}) — {', '.join(setup['raisons_no_trade'])}"
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    return requests.post(url, data=data).json()


def preparer_donnees(symbole="EURUSD=X"):
    data = yf.download(symbole, period="30d", interval="1h")
    data.columns = data.columns.get_level_values(0)
    data["WMA50"] = wma(data["Close"], 50)
    data["Position"] = np.where(data["Close"] > data["WMA50"], "AU-DESSUS", "EN-DESSOUS")
    data = detect_order_blocks(data)
    data = detect_liquidity_sweep(data)
    data = detect_rejection_engulfing(data)
    data = detect_bos(data)
    return data


def main():
    data = preparer_donnees("EURUSD=X")
    for direction in ["LONG", "SHORT"]:
        setup = analyser_dernier_setup_complet(data, direction=direction)
        if setup["classification"] in ["A+", "A"]:
            print(f"Signal trouvé : {setup}")
            envoyer_alerte(setup, actif="EURUSD")
        else:
            print(f"NO TRADE ({direction}) : {setup['raisons_no_trade']}")


if __name__ == "__main__":
    main()