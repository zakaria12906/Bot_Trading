# Fixed-Lot Grid Recovery Bot

A **regime-filtered, event-aware, adaptive-step, fixed-lot basket strategy** with hard shutdown rules. Built for MetaTrader 5 with optional TradingView webhook integration.

## Philosophy

This bot does **not** try to win every trade or trade every day. It trades **rarely and selectively** — only when the market regime fits the grid's mean-reversion assumptions. When those assumptions break down, the bot exits aggressively and stands down.

Key principles:
- **Fixed lot** — equal position size at every grid level; no martingale
- **Adaptive step** — grid spacing is ATR-normalized, not fixed pips
- **Regime-gated** — every new cycle and grid add must earn permission through volatility, trend, spread, and structure checks
- **Hard kill-switches** — equity stop, daily loss cap, spread explosion, time stop, and session controls
- **Three operating modes** — Normal → Defensive → Shutdown

## Architecture

```
main.py                  ← entry point
core/
  bot.py                 ← orchestrator: one SymbolEngine per symbol
  grid_manager.py        ← adaptive grid construction
  basket_manager.py      ← basket lifecycle and exit hierarchy
  risk_manager.py        ← Normal / Defensive / Shutdown mode controller
filters/
  regime_filter.py       ← CALM / CAUTION / HOSTILE classification
  volatility_filter.py   ← ATR ratio gating
  spread_filter.py       ← bid-ask spread monitoring
  session_filter.py      ← trading session window enforcement
  news_filter.py         ← economic calendar lockout
  breakout_detector.py   ← trend/impulse detection (multi-signal scoring)
indicators/
  atr.py                 ← Average True Range (Wilder-smoothed)
  adx.py                 ← Average Directional Index
  moving_average.py      ← SMA, EMA, distance-from-MA
  candle_analysis.py     ← impulse candles, overlap detection
broker/
  base_broker.py         ← abstract broker interface
  mt5_connector.py       ← MetaTrader 5 implementation
webhook/
  server.py              ← Flask webhook for TradingView alerts
utils/
  logger.py              ← rotating file + console logger
  helpers.py             ← timeframe maps, session checks, conversions
```

## Decision Flow (per tick)

```
1. Daily reset check
2. Cooldown active? → skip
3. Basket open?
   YES → evaluate risk mode
         SHUTDOWN  → close all, record loss, cooldown
         check time stop → close if expired
         check basket TP → close if target hit
         check scratch exit → close near breakeven if regime deteriorated
         NORMAL → check if price reached next grid level → add position
   NO  → can open new cycle?
         all filters pass + directional bias found → open initial position + grid plan
```

## Exit Priority Hierarchy

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | Equity stop breached | Close all, shutdown |
| 2 | Regime failure (HOSTILE) | Close all, shutdown |
| 3 | Time stop expired | Close basket |
| 4 | Basket TP reached | Close basket (profit) |
| 5 | Scratch exit available | Close near breakeven |

## Three Risk Modes

| Mode | Trigger | Allowed |
|------|---------|---------|
| **Normal** | All filters pass | Open cycles, add grid levels, take TP |
| **Defensive** | Regime CAUTION, spread widened, news approaching, breakout score 2 | No new cycles, no adds, prioritize exit |
| **Shutdown** | Equity stop, HOSTILE regime, spread explosion, breakout score 3+, session closed | Close everything immediately |

## Setup

### Prerequisites

- Windows VPS with MetaTrader 5 installed
- Python 3.10+
- MT5 terminal running and logged in

### Installation

```bash
git clone <repo-url> && cd Trading_view_Bot
pip install -r requirements.txt
```

### Configuration

1. Copy `.env.example` to `.env` and fill in your MT5 credentials:

```bash
cp .env.example .env
```

2. Edit `config.yaml` to configure symbols, risk parameters, and session windows. Every parameter from the blueprint is exposed.

### Running

```bash
python main.py
# or with a custom config
python main.py --config my_config.yaml
```

## TradingView Webhook

When `webhook.enabled: true` in config, the bot starts a Flask server that accepts POST requests.

### Endpoint

```
POST /webhook
```

### Payload

```json
{
  "secret": "your_webhook_secret",
  "symbol": "EURUSD",
  "direction": "BUY"
}
```

### Monitoring

```
GET /health     → {"status": "ok"}
GET /status     → account balance, equity, per-symbol basket state and risk mode
```

### TradingView Alert Setup

In TradingView, create an alert with webhook URL `http://your-vps-ip:5000/webhook` and message body:

```json
{"secret": "{{strategy.order.comment}}", "symbol": "EURUSD", "direction": "BUY"}
```

## Key Configuration Parameters

| Parameter | Location | Purpose |
|-----------|----------|---------|
| `lot_size` | symbols.SYMBOL | Fixed lot for every grid entry |
| `max_trades` | symbols.SYMBOL | Maximum grid levels (caps exposure) |
| `basket_tp_currency` | symbols.SYMBOL | Net P/L target in account currency |
| `grid_step_multiplier` | symbols.SYMBOL | Step = ATR × this multiplier |
| `min_step_points` | symbols.SYMBOL | Floor for grid step |
| `max_atr_ratio` | symbols.SYMBOL | Block entries above this ATR ratio |
| `adx_ceiling` | symbols.SYMBOL | Block entries when ADX exceeds this |
| `equity_stop_pct` | risk | Hard kill at N% of balance |
| `max_daily_loss_pct` | risk | Stop for the day at N% |
| `max_daily_losing_baskets` | risk | Stop after N losing baskets |
| `news_lockout_minutes` | symbols.SYMBOL | No new cycles within N min of high-impact news |
| `time_stop_minutes` | symbols.SYMBOL | Max basket lifespan |

## Disclaimer

This bot is a tool for systematic trading, not a profit guarantee. No automated system can eliminate market risk. The bot deliberately skips many market conditions and accepts bounded losses in exchange for avoiding catastrophic ones. Always test on a demo account first.
