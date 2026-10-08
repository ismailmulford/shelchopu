"""
ShelChoPu - bot futures otomatis (Bitget / Binance USDT-M).
Multi-simbol, maksimal N posisi terbuka sekaligus (default 3).
Dirancang stateless: setiap run membaca kondisi langsung dari exchange,
cocok untuk dijalankan terjadwal (GitHub Actions) tanpa VPS.
TP/SL dipasang sebagai order trigger reduce-only di exchange.
"""
import os
import sys
import logging
from statistics import mean

import ccxt

APP_NAME = "ShelChoPu"
TP_PCT_OF_MARGIN = 0.50   # target bersih 50% dari margin
SL_PCT_OF_MARGIN = 0.30   # batas loss 30% dari margin

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(APP_NAME)


def env(name, default=None):
    v = os.getenv(name, default)
    if v is None or v == "":
        return default
    return v


class Config:
    def __init__(self):
        self.exchange_id = env("SHELCHOPU_EXCHANGE", "bitget").lower()
        self.api_key = env("SHELCHOPU_API_KEY", "")
        self.api_secret = env("SHELCHOPU_API_SECRET", "")
        self.passphrase = env("SHELCHOPU_API_PASSPHRASE", "")
        self.sandbox = env("SHELCHOPU_SANDBOX", "true").lower() == "true"
        self.symbols = [s.strip() for s in env(
            "SHELCHOPU_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT").split(",") if s.strip()]
        self.max_positions = int(env("SHELCHOPU_MAX_POSITIONS", "3"))
        self.side_mode = env("SHELCHOPU_SIDE", "auto").lower()   # auto | long | short
        self.margin = float(env("SHELCHOPU_MARGIN_USDT", "20"))
        self.leverage = int(env("SHELCHOPU_LEVERAGE", "10"))
        self.fee_open = float(env("SHELCHOPU_FEE_OPEN", "0.0006"))
        self.fee_close = float(env("SHELCHOPU_FEE_CLOSE", "0.0006"))
        self.timeframe = env("SHELCHOPU_TIMEFRAME", "5m")
        self.lookback = int(env("SHELCHOPU_LOOKBACK", "10"))
        self.vol_mult = float(env("SHELCHOPU_VOL_MULT", "1.2"))
        self.ema_period = int(env("SHELCHOPU_EMA_PERIOD", "50"))

        if self.exchange_id not in ("bitget", "binance"):
            sys.exit("SHELCHOPU_EXCHANGE harus 'bitget' atau 'binance'.")
        if self.side_mode not in ("auto", "long", "short"):
            sys.exit("SHELCHOPU_SIDE harus 'auto', 'long', atau 'short'.")
        if not self.symbols:
            sys.exit("SHELCHOPU_SYMBOLS belum diisi.")
        if self.max_positions < 1:
            sys.exit("SHELCHOPU_MAX_POSITIONS minimal 1.")
        if not self.api_key or not self.api_secret:
            sys.exit("API key/secret belum diisi.")
        if self.exchange_id == "bitget" and not self.passphrase:
            sys.exit("Bitget membutuhkan passphrase.")


def build_exchange(cfg):
    params = {
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    }
    if cfg.exchange_id == "bitget":
        params["password"] = cfg.passphrase
        ex = ccxt.bitget(params)
    else:
        ex = ccxt.binanceusdm(params)
    if cfg.sandbox:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    return ex


def price_levels(entry, side, leverage, fee_open, fee_close):
    """Harga impas, TP, dan SL (sudah memperhitungkan fee) berdasarkan harga fill."""
    k_tp = TP_PCT_OF_MARGIN / leverage
    k_sl = SL_PCT_OF_MARGIN / leverage
    if side == "long":
        be = entry * (1 + fee_open) / (1 - fee_close)
        tp = entry * (1 + fee_open + k_tp) / (1 - fee_close)
        sl = entry * (1 + fee_open - k_sl) / (1 - fee_close)
    else:
        be = entry * (1 - fee_open) / (1 + fee_close)
        tp = entry * (1 - fee_open - k_tp) / (1 + fee_close)
        sl = entry * (1 - fee_open + k_sl) / (1 + fee_close)
    return {"breakeven": be, "tp": tp, "sl": sl}


def ema(values, period):
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def get_signal(ex, cfg, symbol):
    """
    Mengembalikan (side, timestamp_candle) jika ada sinyal, atau (None, timestamp).
    Candle terakhir yang masih berjalan dibuang, hanya candle yang sudah tutup yang dipakai.
    """
    candles = ex.fetch_ohlcv(symbol, cfg.timeframe, limit=200)
    closed = candles[:-1]
    ts, o, h, l, c, v = closed[-1]

    window = closed[-1 - cfg.lookback:-1]
    range_high = max(x[2] for x in window)
    range_low = min(x[3] for x in window)
    vol_avg = mean(x[5] for x in window)
    trend = ema([x[4] for x in closed], cfg.ema_period)
    vol_spike = v > cfg.vol_mult * vol_avg

    log.info("[%s] close=%.4f | high-range=%.4f | low-range=%.4f | EMA%d=%.4f | vol=%.2f (rata2 %.2f)",
             symbol, c, range_high, range_low, cfg.ema_period, trend, v, vol_avg)

    if cfg.side_mode in ("auto", "long") and c > range_high and vol_spike and c > trend:
        return "long", ts
    if cfg.side_mode in ("auto", "short") and c < range_low and vol_spike and c < trend:
        return "short", ts
    return None, ts


def get_open_positions(ex, cfg):
    """Dictionary simbol -> posisi, hanya untuk simbol yang ada di cfg.symbols."""
    result = {}
    for p in ex.fetch_positions(cfg.symbols):
        if p["symbol"] in cfg.symbols and float(p.get("contracts") or 0) > 0:
            result[p["symbol"]] = p
    return result


def cancel_all_open_orders(ex, symbol):
    try:
        for o in ex.fetch_open_orders(symbol):
            ex.cancel_order(o["id"], symbol)
            log.info("[%s] Batalkan order sisa: %s", symbol, o["id"])
    except Exception as e:
        log.warning("[%s] Gagal membatalkan order sisa: %s", symbol, e)


def place_protection(ex, cfg, symbol, side, entry, qty):
    lv = price_levels(entry, side, cfg.leverage, cfg.fee_open, cfg.fee_close)
    tp = float(ex.price_to_precision(symbol, lv["tp"]))
    sl = float(ex.price_to_precision(symbol, lv["sl"]))
    be = float(ex.price_to_precision(symbol, lv["breakeven"]))
    close_side = "sell" if side == "long" else "buy"

    log.info("[%s] Harga impas : %.4f", symbol, be)
    log.info("[%s] Take profit : %.4f (bersih +%d%% margin)", symbol, tp, TP_PCT_OF_MARGIN * 100)
    log.info("[%s] Stop loss   : %.4f (bersih -%d%% margin)", symbol, sl, SL_PCT_OF_MARGIN * 100)

    ex.create_order(symbol, "market", close_side, qty, None,
                    {"triggerPrice": tp, "reduceOnly": True})
    ex.create_order(symbol, "market", close_side, qty, None,
                    {"triggerPrice": sl, "reduceOnly": True})
    log.info("[%s] TP dan SL terpasang di exchange.", symbol)


def open_position(ex, cfg, symbol, side):
    ex.set_margin_mode("isolated", symbol)
    ex.set_leverage(cfg.leverage, symbol)

    ref_price = ex.fetch_ticker(symbol)["last"]
    amount = ex.amount_to_precision(symbol, cfg.margin * cfg.leverage / ref_price)
    order_side = "buy" if side == "long" else "sell"

    log.info("[%s] Buka %s qty=%s (ref %.4f)", symbol, side, amount, ref_price)
    order = ex.create_order(symbol, "market", order_side, amount)
    order = ex.fetch_order(order["id"], symbol)
    entry = float(order["average"] or order["price"] or ref_price)
    filled = float(order["filled"])
    log.info("[%s] Entry fill: %.4f", symbol, entry)

    place_protection(ex, cfg, symbol, side, entry, filled)


def show_recent_trades(ex, symbol):
    try:
        trades = ex.fetch_my_trades(symbol, limit=3)
        for t in trades[-3:]:
            info = t.get("info", {}) or {}
            pnl = float(info.get("realizedPnl") or info.get("profit") or 0)
            log.info("[%s] Trade terakhir %s %s harga=%s realized PnL=%.4f",
                     symbol, t["side"], t["amount"], t["price"], pnl)
    except Exception as e:
        log.warning("[%s] Gagal mengambil riwayat trade: %s", symbol, e)


def run():
    log.info("=== %s run dimulai ===", APP_NAME)
    cfg = Config()
    ex = build_exchange(cfg)
    log.info("Terhubung ke %s (sandbox=%s)", cfg.exchange_id, cfg.sandbox)

    positions = get_open_positions(ex, cfg)
    open_count = len(positions)
    log.info("Posisi terbuka: %d dari maksimal %d", open_count, cfg.max_positions)

    for symbol in cfg.symbols:
        # Simbol yang sudah punya posisi: pastikan TP dan SL masih lengkap
        if symbol in positions:
            pos = positions[symbol]
            side = pos["side"]
            entry = float(pos["entryPrice"])
            qty = float(pos["contracts"])
            log.info("[%s] Posisi %s terbuka, entry %.4f. TP/SL dijalankan exchange.", symbol, side, entry)
            reduce_orders = [o for o in ex.fetch_open_orders(symbol) if o.get("reduceOnly")]
            if len(reduce_orders) < 2:
                log.warning("[%s] TP/SL tidak lengkap (%d order), memasang ulang.", symbol, len(reduce_orders))
                cancel_all_open_orders(ex, symbol)
                place_protection(ex, cfg, symbol, side, entry, qty)
            continue

        # Tidak ada posisi di simbol ini: bersihkan sisa order trigger
        cancel_all_open_orders(ex, symbol)

        if open_count >= cfg.max_positions:
            log.info("[%s] Batas %d posisi sudah tercapai, dilewati.", symbol, cfg.max_positions)
            continue

        signal, candle_ts = get_signal(ex, cfg, symbol)
        if signal is None:
            log.info("[%s] Tidak ada sinyal.", symbol)
            show_recent_trades(ex, symbol)
            continue

        # Cegah entry ganda pada candle yang sama (misalnya setelah TP/SL terpicu)
        if ex.fetch_my_trades(symbol, since=candle_ts):
            log.info("[%s] Sudah ada trade pada candle ini, dilewati.", symbol)
            continue

        log.info("[%s] SINYAL %s terdeteksi.", symbol, signal.upper())
        open_position(ex, cfg, symbol, signal)
        open_count += 1
        log.info("Posisi terbuka sekarang: %d dari maksimal %d", open_count, cfg.max_positions)


if __name__ == "__main__":
    run()
