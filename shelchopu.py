"""
ShelChoPu - bot futures otomatis untuk Bitget dan Binance (USDT-M).
TP/SL dipasang sebagai order trigger reduce-only di exchange,
sehingga eksekusi dan PnL final sepenuhnya dihitung oleh exchange.
"""
import os
import sys
import time
import logging
from dataclasses import dataclass

import ccxt
from dotenv import load_dotenv

APP_NAME = "ShelChoPu"
TP_PCT_OF_MARGIN = 0.50   # target bersih 50% dari margin
SL_PCT_OF_MARGIN = 0.30   # batas loss 30% dari margin
POLL_SECONDS = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(APP_NAME)


@dataclass
class Config:
    exchange_id: str
    api_key: str
    api_secret: str
    passphrase: str
    sandbox: bool
    symbol: str
    side: str            # "long" atau "short"
    margin: float        # USDT
    leverage: int
    fee_open: float      # fraksi, mis. 0.0006 = 0,06%
    fee_close: float


def load_config() -> Config:
    load_dotenv()
    cfg = Config(
        exchange_id=os.getenv("SHELCHOPU_EXCHANGE", "bitget").lower(),
        api_key=os.getenv("SHELCHOPU_API_KEY", ""),
        api_secret=os.getenv("SHELCHOPU_API_SECRET", ""),
        passphrase=os.getenv("SHELCHOPU_API_PASSPHRASE", ""),
        sandbox=os.getenv("SHELCHOPU_SANDBOX", "true").lower() == "true",
        symbol=os.getenv("SHELCHOPU_SYMBOL", "BTC/USDT:USDT"),
        side=os.getenv("SHELCHOPU_SIDE", "long").lower(),
        margin=float(os.getenv("SHELCHOPU_MARGIN_USDT", "20")),
        leverage=int(os.getenv("SHELCHOPU_LEVERAGE", "10")),
        fee_open=float(os.getenv("SHELCHOPU_FEE_OPEN", "0.0006")),
        fee_close=float(os.getenv("SHELCHOPU_FEE_CLOSE", "0.0006")),
    )
    if cfg.exchange_id not in ("bitget", "binance"):
        sys.exit("SHELCHOPU_EXCHANGE harus 'bitget' atau 'binance'.")
    if cfg.side not in ("long", "short"):
        sys.exit("SHELCHOPU_SIDE harus 'long' atau 'short'.")
    if not cfg.api_key or not cfg.api_secret:
        sys.exit("API key/secret belum diisi.")
    if cfg.exchange_id == "bitget" and not cfg.passphrase:
        sys.exit("Bitget membutuhkan passphrase.")
    return cfg


def build_exchange(cfg: Config):
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


def price_levels(entry: float, side: str, leverage: int,
                 fee_open: float, fee_close: float) -> dict:
    """Harga impas, TP, dan SL dalam harga, berdasarkan fill entry."""
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


def open_position(ex, cfg: Config):
    ex.set_margin_mode("isolated", cfg.symbol)
    ex.set_leverage(cfg.leverage, cfg.symbol)

    ticker = ex.fetch_ticker(cfg.symbol)
    ref_price = ticker["last"]
    notional = cfg.margin * cfg.leverage
    amount = ex.amount_to_precision(cfg.symbol, notional / ref_price)

    order_side = "buy" if cfg.side == "long" else "sell"
    close_side = "sell" if cfg.side == "long" else "buy"
    log.info("Buka %s %s qty=%s (ref %.4f)", cfg.side, cfg.symbol, amount, ref_price)

    order = ex.create_order(cfg.symbol, "market", order_side, amount)
    time.sleep(1)
    order = ex.fetch_order(order["id"], cfg.symbol)
    entry = float(order["average"] or order["price"] or ref_price)
    filled = float(order["filled"])

    lv = price_levels(entry, cfg.side, cfg.leverage, cfg.fee_open, cfg.fee_close)
    tp_price = float(ex.price_to_precision(cfg.symbol, lv["tp"]))
    sl_price = float(ex.price_to_precision(cfg.symbol, lv["sl"]))
    be_price = float(ex.price_to_precision(cfg.symbol, lv["breakeven"]))

    log.info("Entry fill      : %.4f", entry)
    log.info("Harga impas     : %.4f", be_price)
    log.info("Take profit     : %.4f  (bersih +%.0f%% margin)", tp_price, TP_PCT_OF_MARGIN * 100)
    log.info("Stop loss       : %.4f  (bersih -%.0f%% margin)", sl_price, SL_PCT_OF_MARGIN * 100)

    # TP dan SL dipasang sebagai order trigger reduce-only di exchange
    ex.create_order(cfg.symbol, "market", close_side, filled, None, {
        "triggerPrice": tp_price, "reduceOnly": True,
    })
    ex.create_order(cfg.symbol, "market", close_side, filled, None, {
        "triggerPrice": sl_price, "reduceOnly": True,
    })
    log.info("TP dan SL sudah terpasang di exchange.")
    return entry, filled, time.time() * 1000


def get_open_size(ex, cfg: Config) -> float:
    positions = ex.fetch_positions([cfg.symbol])
    for p in positions:
        if p["symbol"] == cfg.symbol and float(p.get("contracts") or 0) > 0:
            return float(p["contracts"])
    return 0.0


def realized_result(ex, cfg: Config, since_ms: float) -> dict:
    """PnL dan fee diambil langsung dari riwayat trade exchange."""
    trades = ex.fetch_my_trades(cfg.symbol, since=int(since_ms))
    pnl = 0.0
    fee = 0.0
    for t in trades:
        info = t.get("info", {}) or {}
        pnl += float(info.get("realizedPnl") or info.get("profit") or 0)
        if t.get("fee") and t["fee"].get("cost"):
            fee += float(t["fee"]["cost"])
    return {"pnl": pnl, "fee": fee, "net": pnl - fee}


def monitor(ex, cfg: Config, since_ms: float):
    log.info("Memantau posisi. TP/SL dijalankan oleh exchange.")
    while True:
        size = get_open_size(ex, cfg)
        if size == 0:
            break
        time.sleep(POLL_SECONDS)

    # Batalkan sisa order trigger yang belum tereksekusi
    try:
        for o in ex.fetch_open_orders(cfg.symbol):
            if o.get("reduceOnly"):
                ex.cancel_order(o["id"], cfg.symbol)
    except Exception as e:
        log.warning("Gagal membatalkan sisa order: %s", e)

    res = realized_result(ex, cfg, since_ms)
    log.info("Posisi ditutup oleh exchange.")
    log.info("PnL realisasi (exchange): %.4f USDT", res["pnl"])
    log.info("Fee (exchange)          : %.4f USDT", res["fee"])
    log.info("Hasil bersih            : %.4f USDT (%.2f%% dari margin)",
             res["net"], res["net"] / load_margin_ref(cfg) * 100)


def load_margin_ref(cfg: Config) -> float:
    return cfg.margin


def main():
    log.info("=== %s dimulai ===", APP_NAME)
    cfg = load_config()
    ex = build_exchange(cfg)
    log.info("Terhubung ke %s (sandbox=%s)", cfg.exchange_id, cfg.sandbox)

    if get_open_size(ex, cfg) > 0:
        sys.exit("Sudah ada posisi terbuka di simbol ini. Tutup dulu agar tidak dobel.")

    entry, filled, since_ms = open_position(ex, cfg)
    monitor(ex, cfg, since_ms)


if __name__ == "__main__":
    main()
