"""
ShelChoPu - bot futures otomatis (Bitget / Binance USDT-M).
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
        self.symbol = env("SHELCHOPU_SYMBOL", "BTC/USDT:USDT")
        self.side_mode = env("SHELCHOPU_SIDE", "auto").lower()   # auto | long | short
        self.margin = float(env("SHELCHOPU_MARGIN_USDT", "20"))
        self.leverage = int(env("SHELCHOPU_LEVERAGE", "10"))
        self.fee_open = float(env("SHELCHOPU_FEE_OPEN", "0.0006"))
        self.fee_close = float(env("SHELCHOPU_FEE_CLOSE", "0.0006"))
        self.timeframe = env("SHELCHOPU_TIMEFRAME", "15m")
        self.lookback = int(env("SHELCHOPU_LOOKBACK", "20"))
        self.vol_mult = float(env("SHELCHOPU_VOL_MULT", "1.5"))
        self.ema_period = int(env("SHELCHOPU_EMA_PERIOD", "50"))

        if self.exchange_id not in ("bitget", "binance"):
            sys.exit("SHELCHOPU_EXCHANGE harus 'bitget' atau 'binance'.")
        if self.side_mode not in ("auto", "long", "short"):
            sys.exit("SHELCHOPU_SIDE harus 'auto', 'long', atau 'short'.")
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


def get_signal(ex, cfg):
    """
    Mengembalikan (side, timestamp_candle) jika ada sinyal, atau (None, timestamp).
    Candle terakhir yang masih berjalan dibuang, hanya candle yang sudah tutup yang dipakai.
    """
    candles = ex.fetch_ohlcv(cfg.symbol, cfg.timeframe, limit=200)
    closed = candles[:-1]
    ts, o, h, l, c, v = closed[-1]

    window = closed[-1 - cfg.lookback:-1]
    range_high = max(x[2] for x in window)
    range_low = min(x[3] for x in window)
    vol_avg = mean(x[5] for x in window)
    trend = ema([x[4] for x in closed], cfg.ema_period)
    vol_spike = v > cfg.vol_mult * vol_avg

    log.info("Candle %s | close=%.4f | high-range=%.4f | low-range=%.4f | EMA%d=%.4f | vol=%.2f (rata2 %.2f)",
             ts, c, range_high, range_low, cfg.ema_period, trend, v, vol_avg)

    if cfg.side_mode in ("auto", "long") and c > range_high and vol_spike and c > trend:
        return "long", ts
    if cfg.side_mode in ("auto", "short") and c < range_low and vol_spike and c < trend:
        return "short", ts
    return None, ts


def get_position(ex, cfg):
    for p in ex.fetch_positions([cfg.symbol]):
        if p["symbol"] == cfg.symbol and float(p.get("contracts") or 0) > 0:
            return p
    return None


def cancel_all_open_orders(ex, cfg):
    try:
        for o in ex.fetch_open_orders(cfg.symbol):
            ex.cancel_order(o["id"], cfg.symbol)
            log.info("Batalkan order sisa: %s", o["id"])
    except Exception as e:
        log.warning("Gagal membatalkan order sisa: %s", e)


def place_protection(ex, cfg, side, entry, qty):
    lv = price_levels(entry, side, cfg.leverage, cfg.fee_open, cfg.fee_close)
    tp = float(ex.price_to_precision(cfg.symbol, lv["tp"]))
    sl = float(ex.price_to_precision(cfg.symbol, lv["sl"]))
    be = float(ex.price_to_precision(cfg.symbol, lv["breakeven"]))
    close_side = "sell" if side == "long" else "buy"

    log.info("Harga impas : %.4f", be)
    log.info("Take profit : %.4f (bersih +%d%% margin)", tp, TP_PCT_OF_MARGIN * 100)
    log.info("Stop loss   : %.4f (bersih -%d%% margin)", sl, SL_PCT_OF_MARGIN * 100)

    ex.create_order(cfg.symbol, "market", close_side, qty, None,
                    {"triggerPrice": tp, "reduceOnly": True})
    ex.create_order(cfg.symbol, "market", close_side, qty, None,
                    {"triggerPrice": sl, "reduceOnly": True})
    log.info("TP dan SL terpasang di exchange.")


def open_position(ex, cfg, side):
    ex.set_margin_mode("isolated", cfg.symbol)
    ex.set_leverage(cfg.leverage, cfg.symbol)

    ref_price = ex.fetch_ticker(cfg.symbol)["last"]
    amount = ex.amount_to_precision(cfg.symbol, cfg.margin * cfg.leverage / ref_price)
    order_side = "buy" if side == "long" else "sell"

    log.info("Buka %s %s qty=%s (ref %.4f)", side, cfg.symbol, amount, ref_price)
    order = ex.create_order(cfg.symbol, "market", order_side, amount)
    order = ex.fetch_order(order["id"], cfg.symbol)
    entry = float(order["average"] or order["price"] or ref_price)
    filled = float(order["filled"])
    log.info("Entry fill: %.4f", entry)

    place_protection(ex, cfg, side, entry, filled)


def show_recent_trades(ex, cfg):
    try:
        trades = ex.fetch_my_trades(cfg.symbol, limit=5)
        for t in trades[-5:]:
            info = t.get("info", {}) or {}
            pnl = float(info.get("realizedPnl") or info.get("profit") or 0)
            log.info("Trade terakhir %s %s harga=%s realized PnL=%.4f",
                     t["side"], t["amount"], t["price"], pnl)
    except Exception as e:
        log.warning("Gagal mengambil riwayat trade: %s", e)


def run():
    log.info("=== %s run dimulai ===", APP_NAME)
    cfg = Config()
    ex = build_exchange(cfg)
    log.info("Terhubung ke %s (sandbox=%s)", cfg.exchange_id, cfg.sandbox)

    pos = get_position(ex, cfg)
    if pos:
        side = pos["side"]
        entry = float(pos["entryPrice"])
        qty = float(pos["contracts"])
        log.info("Posisi %s masih terbuka, entry %.4f. TP/SL dijalankan exchange.", side, entry)
        open_orders = ex.fetch_open_orders(cfg.symbol)
        reduce_orders = [o for o in open_orders if o.get("reduceOnly")]
        if len(reduce_orders) < 2:
            log.warning("TP/SL tidak lengkap (%d order), memasang ulang.", len(reduce_orders))
            cancel_all_open_orders(ex, cfg)
            place_protection(ex, cfg, side, entry, qty)
        return

    # Tidak ada posisi: bersihkan sisa order trigger agar tidak salah eksekusi
    cancel_all_open_orders(ex, cfg)

    signal, candle_ts = get_signal(ex, cfg)
    if signal is None:
        log.info("Tidak ada sinyal.")
        show_recent_trades(ex, cfg)
        return

    # Cegah entry ganda pada candle yang sama (misalnya setelah TP/SL terpicu)
    recent = ex.fetch_my_trades(cfg.symbol, since=candle_ts)
    if recent:
        log.info("Sudah ada trade pada candle ini, dilewati.")
        return

    log.info("SINYAL %s terdeteksi.", signal.upper())
    open_position(ex, cfg, signal)


if __name__ == "__main__":
    run()
