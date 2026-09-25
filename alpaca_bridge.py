"""Turn TradingAgents ratings into orders on an Alpaca paper account.

Paper only: the trading client is always created with ``paper=True``, and the
paper endpoint refuses live keys.

Sizing (long-only; never shorts, never borrows):

    slot = account equity / number of tickers in the watchlist

    Buy          raise the position to one slot (never trims one already above it)
    Overweight   move halfway from the position to one slot
    Hold         no trade
    Underweight  sell half the position
    Sell         close the position
    anything else (REVIEW, ERROR): no trade

Buys together spend at most the cash free when the plan is made, and at most
MAX_DAILY_BUY of equity in one run, so a new book is built over several days.
Proceeds of sells in the same run are not counted, so margin is never used.
Market orders placed after the 16:00 ET close queue for the next session.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, PositionSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest

from tradingagents.portfolio import PortfolioContext, Position

# Target market value for a rating, from what is held now and one slot.
TARGETS = {
    "Buy": lambda held, slot: max(held, slot),
    "Overweight": lambda held, slot: held + max(slot - held, 0.0) / 2,
    "Hold": lambda held, slot: held,
    "Underweight": lambda held, slot: held / 2,
    "Sell": lambda held, slot: 0.0,
}

# A change smaller than this share of a slot is not worth an order.
REBALANCE_BAND = 0.05
MIN_ORDER = 1.0  # dollars; Alpaca's smallest notional order
# Share of equity that one run may spend on buys.
MAX_DAILY_BUY = 0.25

MARKET_TZ = ZoneInfo("America/New_York")  # Alpaca's calendar times are New York times
# Yahoo's daily bar is final a little after the close; before that its Close is partial.
BAR_FINAL_AFTER = timedelta(minutes=30)


class BrokerError(RuntimeError):
    """The account cannot be reached or used."""


@dataclass
class Plan:
    ticker: str
    rating: str
    held: float = 0.0            # market value held now
    target: float = 0.0          # market value the rating asks for
    side: str = ""               # "buy", "sell", or "" for no order
    notional: float | None = None
    qty: float | None = None
    note: str = ""
    status: str = ""

    def order(self) -> str:
        if not self.side:
            return "-"
        amount = f"${self.notional:,.2f}" if self.notional is not None else f"{self.qty:g} sh"
        return f"{self.side} {amount}"


class Broker:
    """An Alpaca paper account: its book, and the orders the ratings call for."""

    def __init__(self, trading=None, data=None):
        if trading is None:
            key = os.environ.get("ALPACA_API_KEY")
            secret = os.environ.get("ALPACA_SECRET_KEY")
            if not key or not secret:
                raise BrokerError("set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env (paper keys)")
            trading = TradingClient(key, secret, paper=True)
            data = StockHistoricalDataClient(key, secret)
        self.trading, self.data = trading, data
        self._assets: dict = {}
        self.refresh()

    def refresh(self):
        """Reload the account and positions; a watchlist run can take hours."""
        try:
            self.account = self.trading.get_account()
            self.positions = {p.symbol: p for p in self.trading.get_all_positions()}
        except APIError as exc:
            raise BrokerError(
                f"Alpaca refused the request ({exc.message}); check that the keys are paper keys"
            ) from None
        if self.account.trading_blocked or self.account.account_blocked:
            raise BrokerError("the Alpaca account is blocked from trading")

    def summary(self) -> str:
        a = self.account
        change = float(a.equity) - float(a.last_equity)
        return (f"Alpaca paper {a.account_number}: equity ${float(a.equity):,.2f} "
                f"({change:+,.2f} today), cash ${float(a.cash):,.2f}, {len(self.positions)} positions")

    def last_completed_session(self, now: datetime | None = None) -> str:
        """The latest session whose daily bar is final, from Alpaca's market calendar.

        Holidays are skipped and early closes honoured, so a run on a holiday or a
        weekend lands on a session already analyzed and places nothing new.
        """
        now = (now or datetime.now(MARKET_TZ)).astimezone(MARKET_TZ).replace(tzinfo=None)
        sessions = self.trading.get_calendar(
            GetCalendarRequest(start=now.date() - timedelta(days=14), end=now.date())
        )
        done = [s for s in sessions if s.close + BAR_FINAL_AFTER <= now]
        if not done:
            raise BrokerError("no completed session in the last two weeks of Alpaca's calendar")
        return done[-1].date.strftime("%Y-%m-%d")

    def portfolio_context(self) -> PortfolioContext:
        """The book as the trader, risk and portfolio agents see it."""
        return PortfolioContext(
            cash=float(self.account.cash),
            currency=self.account.currency or "USD",
            positions=[
                Position(ticker=symbol, quantity=_signed_qty(p), average_price=float(p.avg_entry_price))
                for symbol, p in self.positions.items()
            ],
        )

    def plan(self, ratings: list[tuple[str, str]], slots: int) -> list[Plan]:
        """One plan per (ticker, rating); buys are capped to the free cash and the daily cap."""
        equity = float(self.account.equity)
        slot = equity / max(slots, 1)
        band = max(MIN_ORDER, REBALANCE_BAND * slot)
        queued = {o.symbol for o in self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))}
        plans = [self._plan_one(ticker, rating, slot, band, queued) for ticker, rating in ratings]

        # Open orders already hold buying power, and cash alone never borrows.
        budget = max(0.0, min(float(self.account.cash), float(self.account.non_marginable_buying_power),
                              MAX_DAILY_BUY * equity))
        for plan in sorted((p for p in plans if p.side == "buy"), key=lambda p: p.rating != "Buy"):
            budget -= self._size_buy(plan, budget, band)
        return plans

    def submit(self, plans: list[Plan], trade_date: str):
        """Place every planned order as a DAY market order."""
        for plan in plans:
            if not plan.side:
                continue
            request = MarketOrderRequest(
                symbol=plan.ticker,
                side=OrderSide.BUY if plan.side == "buy" else OrderSide.SELL,
                notional=plan.notional,
                qty=plan.qty,
                time_in_force=TimeInForce.DAY,
                # Alpaca refuses a reused client_order_id, so re-running the same
                # date cannot place a ticker's order twice.
                client_order_id=f"ta-{trade_date}-{plan.ticker}",
            )
            try:
                order = self.trading.submit_order(request)
                plan.status = f"submitted ({getattr(order.status, 'value', order.status)})"
            except APIError as exc:
                plan.status = f"rejected: {exc.message}"

    def _plan_one(self, ticker, rating, slot, band, queued) -> Plan:
        plan = Plan(ticker, rating)
        if rating not in TARGETS:
            plan.note = "no trade on this rating"
            return plan
        asset = self._asset(ticker)
        if asset is None or not asset.tradable:
            plan.note = "not tradable on Alpaca"
            return plan
        position = self.positions.get(ticker)
        if position is not None and _signed_qty(position) < 0:
            plan.note = "short position, left alone"
            return plan

        plan.held = float(position.market_value) if position is not None else 0.0
        plan.target = TARGETS[rating](plan.held, slot)
        delta = plan.target - plan.held
        if ticker in queued:
            plan.note = "an order is already open"
        elif rating == "Hold":
            plan.note = "hold"
        elif plan.target == 0 and plan.held > 0:
            plan.side, plan.qty = "sell", float(position.qty)
        elif abs(delta) < band:
            plan.note = "nothing held" if plan.held == 0 and delta <= 0 else "within band"
        elif delta < 0:
            shares = float(position.qty) * -delta / plan.held
            shares = math.floor(shares * 1e6) / 1e6 if asset.fractionable else math.floor(shares)
            if shares:
                plan.side, plan.qty = "sell", shares
            else:
                plan.note = "less than one share to sell"
        else:
            plan.side, plan.notional = "buy", delta
        return plan

    def _size_buy(self, plan: Plan, budget: float, band: float) -> float:
        """Fit a buy to the budget; return the dollars it will spend."""
        wanted = plan.notional
        amount = min(wanted, budget)
        if amount < band:
            plan.side, plan.notional, plan.note = "", None, "today's buy budget is used up"
            return 0.0
        if budget < wanted - band:
            plan.note = "capped by today's buy budget"
        if self._asset(plan.ticker).fractionable:
            plan.notional = math.floor(amount * 100) / 100
        else:
            price = self._price(plan.ticker)
            shares = math.floor(amount / price)
            if not shares:
                plan.side, plan.notional, plan.note = "", None, f"one share (${price:,.2f}) exceeds the buy"
                return 0.0
            plan.notional, plan.qty, amount = None, shares, shares * price
            plan.note = plan.note or f"whole shares at ${price:,.2f}"
        return amount

    def _asset(self, ticker):
        if ticker not in self._assets:
            try:
                self._assets[ticker] = self.trading.get_asset(ticker)
            except APIError:
                self._assets[ticker] = None  # e.g. 2454.TW: not listed in the US
        return self._assets[ticker]

    def _price(self, ticker) -> float:
        # IEX is the feed free accounts may query without a data subscription.
        trades = self.data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX)
        )
        return float(trades[ticker].price)


def _signed_qty(position) -> float:
    qty = abs(float(position.qty))
    return -qty if position.side == PositionSide.SHORT else qty


def render(plans: list[Plan]) -> str:
    lines = [f"  {'ticker':8} {'rating':11} {'held':>11} {'target':>11}  {'order':18} note"]
    for p in plans:
        detail = "; ".join(x for x in (p.note, p.status) if x)
        lines.append(f"  {p.ticker:8} {p.rating:11} {p.held:>11,.2f} {p.target:>11,.2f}  {p.order():18} {detail}")
    return "\n".join(lines)
