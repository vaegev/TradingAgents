"""Run TradingAgents over a watchlist for the latest completed US trading session.

    python run_watchlist.py                          # every ticker in watchlist.txt
    python run_watchlist.py --tickers NVDA,AAPL      # a subset
    python run_watchlist.py --date 2026-09-24        # a specific session
    python run_watchlist.py --analysts market,news   # fewer analysts, fewer LLM calls
    python run_watchlist.py --alpaca                 # also plan orders on the Alpaca paper account
    python run_watchlist.py --alpaca --execute       # and submit them

Each ticker is a full multi-agent run. A ticker already in the decision log for
the date is skipped, so an interrupted sweep continues where it stopped when run
again. Stocks only: crypto needs a different analyst set.

With --alpaca the agents see the paper account's cash and positions, the date
comes from Alpaca's market calendar, and the ratings become orders by the sizing
rule in alpaca_bridge.py. Without --execute the orders are only listed.

Guards for unattended runs:
  - only one run at a time (a lock file);
  - no orders while ~/.tradingagents/STOP exists (touch it to stop trading);
  - no orders when more than MAX_ERROR_SHARE of the tickers failed, since a
    provider outage leaves the rest resting on broken data.
"""

import argparse
import csv
import dataclasses
import fcntl
import logging
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import RATINGS_5_TIER
from tradingagents.backtest import summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

WATCHLIST = Path(__file__).with_name("watchlist.txt")
ANALYSTS = "market,social,news,fundamentals"

HOME = Path.home() / ".tradingagents"
KILL_SWITCH = HOME / "STOP"
LOCK_FILE = HOME / "run_watchlist.lock"
MAX_ERROR_SHARE = 0.2

MARKET_TZ = ZoneInfo("America/New_York")
# Yahoo publishes the final daily bar shortly after the 16:00 close. Before then
# today's candle is partial, and its Close is not a closing price.
SESSION_FINAL = time(16, 30)


def last_completed_session(now: datetime | None = None) -> str:
    """Date of the most recent US session whose daily bar is final, in New York time.

    Exchange holidays are not skipped. A holiday date still runs: the data tools
    serve the last bar before it.
    """
    now = now or datetime.now(MARKET_TZ)
    day = now.date() if now.time() >= SESSION_FINAL else now.date() - timedelta(days=1)
    while day.weekday() >= 5:  # Saturday, Sunday
        day -= timedelta(days=1)
    return day.strftime("%Y-%m-%d")


def parse_date(value: str) -> str:
    """A YYYY-MM-DD date no later than today, as the graph requires.

    Checked once here, so a bad --date is one error instead of one per ticker.
    """
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        day = None
    if day is None or day.strftime("%Y-%m-%d") != value:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")
    if day > date.today():
        raise argparse.ArgumentTypeError(f"{value} is in the future")
    return value


def read_watchlist(path: Path) -> list[str]:
    """Tickers from a file of one per line, ignoring blanks and # comments."""
    tickers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        ticker = line.split("#", 1)[0].strip().upper()
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def acquire_lock():
    """Hold an exclusive lock for the life of the process, or exit if a run holds it."""
    HOME.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")  # noqa: SIM115 -- closing the file would release the lock
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another run_watchlist.py is running; not starting a second one")
    return handle


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--date", type=parse_date,
        help="analysis date, YYYY-MM-DD (default: last completed US session)",
    )
    parser.add_argument("--tickers", help="comma-separated tickers to run instead of watchlist.txt")
    parser.add_argument("--analysts", default=ANALYSTS, help=f"comma-separated (default: {ANALYSTS})")
    parser.add_argument("--alpaca", action="store_true", help="plan orders on the Alpaca paper account")
    parser.add_argument("--execute", action="store_true", help="submit the planned orders (needs --alpaca)")
    args = parser.parse_args()
    if args.execute and not args.alpaca:
        parser.error("--execute needs --alpaca")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    lock = acquire_lock()  # noqa: F841 -- held until the process exits
    print(f"=== run started {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)

    tickers = (
        [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        if args.tickers
        else read_watchlist(WATCHLIST)
    )
    analysts = [a.strip() for a in args.analysts.split(",") if a.strip()]

    # Connect before the long run, so bad keys fail in seconds, not hours.
    broker = portfolio = None
    if args.alpaca:
        import alpaca_bridge

        try:
            broker = alpaca_bridge.Broker()
        except alpaca_bridge.BrokerError as exc:
            sys.exit(f"Alpaca: {exc}")
        portfolio = broker.portfolio_context()
        print(broker.summary(), flush=True)
    trade_date = args.date or (broker.last_completed_session() if broker else last_completed_session())

    config = DEFAULT_CONFIG.copy()
    stats = StatsCallbackHandler()
    ta = TradingAgentsGraph(analysts, config=config, callbacks=[stats])
    logged = {(e["ticker"], e["date"]): e["rating"] for e in ta.memory_log.load_entries()}

    print(f"Analyzing {len(tickers)} tickers for {trade_date} with {', '.join(analysts)}", flush=True)
    results = []
    for i, ticker in enumerate(tickers, 1):
        if (ticker, trade_date) in logged:
            print(f"[{i}/{len(tickers)}] {ticker}: already decided, skipping", flush=True)
            results.append((ticker, logged[ticker, trade_date], "from decision log"))
            continue
        print(f"[{i}/{len(tickers)}] {ticker} ...", flush=True)
        before = stats.get_stats()
        try:
            final_state, rating = ta.propagate(ticker, trade_date, portfolio=portfolio)
            report_dir = ta.save_reports(final_state, ticker)
            results.append((ticker, rating, str(report_dir)))
            outcome = rating
        except Exception as exc:  # one failing ticker must not end the sweep
            results.append((ticker, "ERROR", str(exc)))
            outcome = f"failed: {exc}"
        used = {k: v - before[k] for k, v in stats.get_stats().items()}
        print(f"[{i}/{len(tickers)}] {ticker}: {outcome}  ({used['llm_calls']} LLM calls, "
              f"{used['tokens_in']:,} in / {used['tokens_out']:,} out tokens)", flush=True)

    # Most bullish first; REVIEW and ERROR last.
    order = {r: n for n, r in enumerate(RATINGS_5_TIER)}
    results.sort(key=lambda r: order.get(r[1], len(order)))

    out_dir = Path(config["results_dir"]) / "watchlist"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = out_dir / f"{trade_date}.csv"
    with open(summary, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ticker", "rating", "report"])
        writer.writerows(results)

    print(f"\nRatings for {trade_date}:")
    for ticker, rating, _ in results:
        print(f"  {ticker:10} {rating}")
    total = stats.get_stats()
    print(f"\nLLM usage: {total['llm_calls']} calls, {total['tokens_in']:,} input / "
          f"{total['tokens_out']:,} output tokens")
    print(f"Summary: {summary}")

    if broker is not None:
        broker.refresh()
        # Slots come from the whole watchlist, so a --tickers subset is not sized larger.
        slots = max(len(read_watchlist(WATCHLIST)), len(tickers))
        plans = broker.plan([(ticker, rating) for ticker, rating, _ in results], slots)

        errors = sum(rating == "ERROR" for _, rating, _ in results)
        blocked = None
        if KILL_SWITCH.exists():
            blocked = f"kill switch {KILL_SWITCH} is present"
        elif errors > MAX_ERROR_SHARE * len(results):
            blocked = f"{errors} of {len(results)} tickers failed, so the ratings may rest on broken data"
        if args.execute and blocked is None:
            broker.submit(plans, trade_date)
            mode = "submitted"
        elif args.execute:
            mode = f"NOT submitted: {blocked}"
        else:
            mode = "dry run: add --execute to submit"

        orders = out_dir / f"{trade_date}-orders.csv"
        with open(orders, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[fl.name for fl in dataclasses.fields(alpaca_bridge.Plan)])
            writer.writeheader()
            writer.writerows(dataclasses.asdict(p) for p in plans)
        print(f"\nOrders ({mode}):\n{alpaca_bridge.render(plans)}")
        print(f"Orders: {orders}")
        print(f"\n{broker.summary()}")

    log_path = Path(config["memory_log_path"])
    if log_path.is_file():
        print(f"\nScorecard, all settled decisions:\n{summarize(log_path).render()}")
    print(f"=== run finished {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)


if __name__ == "__main__":
    main()
