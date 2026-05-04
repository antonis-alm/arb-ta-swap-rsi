import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

from almanak.framework.data import (
    BalanceUnavailableError,
    DexQuoteUnavailableError,
    PoolPriceUnavailableError,
    PriceUnavailableError,
    RSIUnavailableError,
    SlippageEstimateUnavailableError,
)
from almanak.framework.intents import Intent
from almanak.framework.strategies import IntentStrategy, MarketSnapshot, almanak_strategy
from almanak.framework.teardown import TeardownMode, TeardownPositionSummary

logger = logging.getLogger(__name__)


class RegimeState(StrEnum):
    LONG_WETH = "LONG_WETH"
    LONG_USDC = "LONG_USDC"
    NEUTRAL = "NEUTRAL"


@dataclass
class SwapCheckResult:
    from_token: str
    to_token: str
    amount: Decimal
    amount_usd: Decimal
    expected_out: Decimal
    impact_bps: Decimal
    slippage_bps: Decimal


@almanak_strategy(
    name="Arb-TA-Swap-RSI",
    description="Arbitrum Uniswap V3 WETH/USDC RSI regime flipper (swap-only)",
    version="1.0.0",
    author="Almanak",
    tags=["rsi", "swap", "uniswap_v3", "arbitrum", "regime"],
    supported_chains=["arbitrum"],
    supported_protocols=["uniswap_v3"],
    intent_types=["SWAP", "HOLD"],
    default_chain="arbitrum",
)
class ArbTASwapRSIStrategy(IntentStrategy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.execution_chain = str(self.get_config("chain", self.chain or "arbitrum"))
        self.protocol = "uniswap_v3"

        self.base_token = str(self.get_config("base_token", "WETH"))
        self.quote_token = str(self.get_config("quote_token", "USDC"))
        self.pool_fee_tier_bps = int(self.get_config("pool_fee_tier_bps", 500))
        self.pool_address = str(self.get_config("pool_address", "")).lower()

        self.rsi_period = int(self.get_config("rsi_period", 14))
        self.rsi_timeframe = str(self.get_config("rsi_timeframe", "5m"))
        self.rsi_lower_band = Decimal(str(self.get_config("rsi_lower_band", "45")))
        self.rsi_upper_band = Decimal(str(self.get_config("rsi_upper_band", "55")))

        self.allocation_pct = Decimal(str(self.get_config("allocation_pct", "0.95")))
        self.max_slippage = Decimal(str(self.get_config("max_slippage", "0.003")))
        self.max_price_impact = Decimal(str(self.get_config("max_price_impact", "0.003")))
        self.max_quote_slippage_bps = Decimal(str(self.get_config("max_quote_slippage_bps", "30")))
        self.max_quote_impact_bps = Decimal(str(self.get_config("max_quote_impact_bps", "30")))
        self.min_expected_out = Decimal(str(self.get_config("min_expected_out", "0")))
        self.min_trade_value_usd = Decimal(str(self.get_config("min_trade_value_usd", "25")))
        self.max_gas_ratio = Decimal(str(self.get_config("max_gas_ratio", "0.05")))
        self.min_pool_liquidity = Decimal(str(self.get_config("min_pool_liquidity", "1")))
        self.quote_timeout_seconds = float(self.get_config("quote_timeout_seconds", 1.5))

        self.cooldown_candles = int(self.get_config("cooldown_candles", 1))
        self.force_action = str(self.get_config("force_action", "")).strip().lower()

        self.halt_on_repeated_failures = bool(self.get_config("halt_on_repeated_failures", True))
        self.max_consecutive_failed_swaps = int(self.get_config("max_consecutive_failed_swaps", 3))

        self.regime_state = RegimeState.NEUTRAL
        self.pending_target_state: RegimeState | None = None
        self.prev_rsi: Decimal | None = None
        self.last_processed_candle_ts: str | None = None
        self.cooldown_until_ts: str | None = None
        self.consecutive_failed_swaps = 0
        self.halted_due_to_failures = False

        self.last_decision_reason = ""
        self.last_expected_out = Decimal("0")
        self.last_slippage_bps = Decimal("0")
        self.last_impact_bps = Decimal("0")
        self.last_trade_amount = Decimal("0")
        self.last_trade_amount_usd = Decimal("0")

    def decide(self, market: MarketSnapshot) -> Intent | None:
        if self.halted_due_to_failures:
            return self._hold("halted after repeated failed swaps")

        if self.force_action:
            return self._forced_intent(market)

        candle_ts = self._latest_closed_candle_ts(market)
        if candle_ts is None:
            return self._hold("no confirmed candle close")

        if candle_ts == self.last_processed_candle_ts:
            return self._hold("waiting for next confirmed candle close")

        rsi_value = self._read_rsi(market)
        if rsi_value is None:
            self._mark_candle_processed(candle_ts, None)
            return self._hold("rsi data unavailable")

        if self._is_cooldown_active(market.timestamp):
            self._mark_candle_processed(candle_ts, rsi_value)
            return self._hold("cooldown active")

        if self.prev_rsi is None:
            self._mark_candle_processed(candle_ts, rsi_value)
            return self._hold("initialized rsi history")

        if self.rsi_lower_band <= rsi_value <= self.rsi_upper_band:
            self.regime_state = RegimeState.NEUTRAL
            self._mark_candle_processed(candle_ts, rsi_value)
            return self._hold("rsi in neutral band")

        cross_up = self.prev_rsi <= self.rsi_upper_band and rsi_value > self.rsi_upper_band
        cross_down = self.prev_rsi >= self.rsi_lower_band and rsi_value < self.rsi_lower_band

        if not cross_up and not cross_down:
            self._mark_candle_processed(candle_ts, rsi_value)
            return self._hold("no cross event")

        target_state = RegimeState.LONG_WETH if cross_up else RegimeState.LONG_USDC
        if self.regime_state == target_state:
            self._mark_candle_processed(candle_ts, rsi_value)
            return self._hold(f"already in {target_state.value}")

        check_result = self._run_swap_sanity_checks(market, target_state)
        if check_result is None:
            self._mark_candle_processed(candle_ts, rsi_value)
            return Intent.hold(reason=self.last_decision_reason)

        self.pending_target_state = target_state
        self.last_expected_out = check_result.expected_out
        self.last_slippage_bps = check_result.slippage_bps
        self.last_impact_bps = check_result.impact_bps
        self.last_trade_amount = check_result.amount
        self.last_trade_amount_usd = check_result.amount_usd

        logger.info(
            "RSI=%s prev=%s state=%s target=%s amount=%s %s expected_out=%s impact_bps=%s slippage_bps=%s",
            rsi_value,
            self.prev_rsi,
            self.regime_state.value,
            target_state.value,
            check_result.amount,
            check_result.from_token,
            check_result.expected_out,
            check_result.impact_bps,
            check_result.slippage_bps,
        )

        self._mark_candle_processed(candle_ts, rsi_value)
        return Intent.swap(
            from_token=check_result.from_token,
            to_token=check_result.to_token,
            amount=check_result.amount,
            max_slippage=self.max_slippage,
            max_price_impact=self.max_price_impact,
            protocol=self.protocol,
            chain=self.execution_chain,
        )

    def _forced_intent(self, market: MarketSnapshot) -> Intent:
        if self.force_action == "buy":
            target_state = RegimeState.LONG_WETH
        elif self.force_action == "sell":
            target_state = RegimeState.LONG_USDC
        else:
            return self._hold(f"unknown force_action={self.force_action}")

        from_token = self.quote_token if target_state == RegimeState.LONG_WETH else self.base_token
        to_token = self.base_token if target_state == RegimeState.LONG_WETH else self.quote_token

        try:
            source_balance = market.balance(from_token)
        except (BalanceUnavailableError, ValueError) as exc:
            return self._hold(f"force_action balance unavailable: {exc}")

        amount = Decimal(str(source_balance.balance)) * self.allocation_pct
        amount_usd = Decimal(str(source_balance.balance_usd)) * self.allocation_pct

        if amount <= 0:
            return self._hold(f"insufficient {from_token} balance")
        if amount_usd < self.min_trade_value_usd:
            return self._hold(
                f"trade value ${amount_usd:.2f} below min ${self.min_trade_value_usd}"
            )

        self.pending_target_state = target_state
        self.last_expected_out = Decimal("0")
        self.last_slippage_bps = Decimal("0")
        self.last_impact_bps = Decimal("0")
        self.last_trade_amount = amount
        self.last_trade_amount_usd = amount_usd

        logger.info(
            "force_action=%s amount=%s %s amount_usd=%s",
            self.force_action,
            amount,
            from_token,
            amount_usd,
        )
        return Intent.swap(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            max_slippage=self.max_slippage,
            max_price_impact=self.max_price_impact,
            protocol=self.protocol,
            chain=self.execution_chain,
        )

    def _run_swap_sanity_checks(self, market: MarketSnapshot, target_state: RegimeState) -> SwapCheckResult | None:
        from_token = self.quote_token if target_state == RegimeState.LONG_WETH else self.base_token
        to_token = self.base_token if target_state == RegimeState.LONG_WETH else self.quote_token

        try:
            source_balance = market.balance(from_token)
            destination_balance = market.balance(to_token)
        except (BalanceUnavailableError, ValueError) as exc:
            return self._set_hold_reason(f"balance unavailable: {exc}")

        amount = Decimal(str(source_balance.balance)) * self.allocation_pct
        amount_usd = Decimal(str(source_balance.balance_usd)) * self.allocation_pct

        if amount <= 0:
            return self._set_hold_reason(f"insufficient {from_token} balance")
        if amount_usd < self.min_trade_value_usd:
            return self._set_hold_reason(
                f"trade value ${amount_usd:.2f} below min ${self.min_trade_value_usd}"
            )

        logger.info(
            "balance %s=%s (%s usd), %s=%s (%s usd)",
            from_token,
            source_balance.balance,
            source_balance.balance_usd,
            to_token,
            destination_balance.balance,
            destination_balance.balance_usd,
        )

        if not self._pool_has_liquidity(market):
            return None

        quote = self._quote_swap(market, from_token, to_token, amount)
        if quote is None:
            return None

        raw_amount_out = getattr(quote, "amount_out", None)
        expected_out = Decimal(str(raw_amount_out)) if raw_amount_out is not None else Decimal("0")
        impact_bps = Decimal(str(getattr(quote, "price_impact_bps", "0")))
        slippage_bps = Decimal(str(getattr(quote, "slippage_estimate_bps", "0")))

        if self.min_expected_out > 0 and raw_amount_out is None:
            return self._set_hold_reason("expected output unavailable while min_expected_out is enforced")
        if expected_out < self.min_expected_out:
            return self._set_hold_reason(
                f"expected output {expected_out} below minimum {self.min_expected_out}"
            )
        if impact_bps > self.max_quote_impact_bps:
            return self._set_hold_reason(
                f"price impact {impact_bps} bps above limit {self.max_quote_impact_bps}"
            )
        if slippage_bps > self.max_quote_slippage_bps:
            return self._set_hold_reason(
                f"slippage {slippage_bps} bps above limit {self.max_quote_slippage_bps}"
            )

        gas_ok = market.is_trade_worthwhile(
            amount_usd=amount_usd,
            chain=self.execution_chain,
            max_gas_ratio=self.max_gas_ratio,
        )
        if not gas_ok:
            gas_cost = market.estimate_swap_gas_cost_usd(self.execution_chain)
            return self._set_hold_reason(
                f"gas cost ${gas_cost} too high for ${amount_usd:.2f} trade"
            )

        return SwapCheckResult(
            from_token=from_token,
            to_token=to_token,
            amount=amount,
            amount_usd=amount_usd,
            expected_out=expected_out,
            impact_bps=impact_bps,
            slippage_bps=slippage_bps,
        )

    def _pool_has_liquidity(self, market: MarketSnapshot) -> bool:
        try:
            if self.pool_address:
                market.pool_price(self.pool_address, chain=self.execution_chain)
                reserves = market.pool_reserves(self.pool_address, chain=self.execution_chain)
                liquidity = Decimal(str(getattr(reserves, "liquidity", "0") or "0"))
                if liquidity < self.min_pool_liquidity:
                    self.last_decision_reason = (
                        f"pool liquidity {liquidity} below {self.min_pool_liquidity}"
                    )
                    return False
            else:
                market.pool_price_by_pair(
                    token_a=self.base_token,
                    token_b=self.quote_token,
                    chain=self.execution_chain,
                    protocol=self.protocol,
                    fee_tier=self.pool_fee_tier_bps,
                )
        except (PoolPriceUnavailableError, ValueError) as exc:
            self.last_decision_reason = f"pool unavailable: {exc}"
            return False
        return True

    def _quote_swap(
        self, market: MarketSnapshot, from_token: str, to_token: str, amount: Decimal
    ) -> Any | None:
        def _fetch_quote():
            return market.price_across_dexs(
                token_in=from_token,
                token_out=to_token,
                amount=amount,
                dexs=[self.protocol],
            )

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            quote_result = executor.submit(_fetch_quote).result(timeout=self.quote_timeout_seconds)

            best_quote = getattr(quote_result, "best_quote", None)
            if best_quote is not None:
                return best_quote

            quotes = getattr(quote_result, "quotes", None) or {}
            if self.protocol in quotes:
                return quotes[self.protocol]
            if quotes:
                return next(iter(quotes.values()))

            self.last_decision_reason = "no quote returned"
            return None
        except FuturesTimeoutError:
            logger.warning(
                "quote fetch timed out after %.2fs; using slippage fallback",
                self.quote_timeout_seconds,
            )
            return self._fallback_quote_from_slippage(market, from_token, to_token, amount)
        except (DexQuoteUnavailableError, ValueError) as exc:
            logger.warning("quote fetch unavailable (%s); using slippage fallback", exc)
            return self._fallback_quote_from_slippage(market, from_token, to_token, amount)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _fallback_quote_from_slippage(
        self, market: MarketSnapshot, from_token: str, to_token: str, amount: Decimal
    ) -> Any | None:
        def _fetch_slippage():
            return market.estimate_slippage(
                token_in=from_token,
                token_out=to_token,
                amount=amount,
                chain=self.execution_chain,
                protocol=self.protocol,
            )

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            estimate_envelope = executor.submit(_fetch_slippage).result(timeout=self.quote_timeout_seconds)
            estimate = getattr(estimate_envelope, "value", estimate_envelope)
            return SimpleNamespace(
                amount_out=None,
                price_impact_bps=Decimal(str(getattr(estimate, "price_impact_bps", 0))),
                slippage_estimate_bps=Decimal(str(getattr(estimate, "effective_slippage_bps", 0))),
            )
        except (FuturesTimeoutError, SlippageEstimateUnavailableError, ValueError) as exc:
            self.last_decision_reason = f"quote/slippage unavailable: {exc}"
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _latest_closed_candle_ts(self, market: MarketSnapshot) -> str | None:
        fallback_ts = self._timeframe_bucket_ts(market.timestamp)
        if not hasattr(market, "ohlcv"):
            return fallback_ts

        try:
            candles = market.ohlcv(
                f"{self.base_token}/{self.quote_token}",
                timeframe=self.rsi_timeframe,
                limit=2,
            )
        except (AttributeError, ValueError) as exc:
            logger.warning("OHLCV unavailable: %s", exc)
            return fallback_ts

        if candles is None or getattr(candles, "empty", True):
            return fallback_ts

        latest = candles.iloc[-1]
        ts = latest.get("timestamp")
        if ts is None:
            return fallback_ts
        if hasattr(ts, "to_pydatetime"):
            ts = ts.to_pydatetime()
        if not isinstance(ts, datetime):
            return fallback_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC).isoformat()

    def _read_rsi(self, market: MarketSnapshot) -> Decimal | None:
        try:
            rsi_data = market.rsi(
                self.base_token,
                period=self.rsi_period,
                timeframe=self.rsi_timeframe,
            )
            return Decimal(str(rsi_data.value))
        except (RSIUnavailableError, ValueError) as exc:
            logger.warning("RSI unavailable: %s", exc)
            return None

    def _is_cooldown_active(self, now: datetime) -> bool:
        if self.cooldown_until_ts is None:
            return False
        cooldown_until = datetime.fromisoformat(self.cooldown_until_ts)
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=UTC)
        return now.astimezone(UTC) < cooldown_until

    def _mark_candle_processed(self, candle_ts: str, rsi_value: Decimal | None) -> None:
        self.last_processed_candle_ts = candle_ts
        if rsi_value is not None:
            self.prev_rsi = rsi_value

    def _set_hold_reason(self, reason: str) -> None:
        self.last_decision_reason = reason
        logger.info(reason)
        return None

    def _hold(self, reason: str) -> Intent:
        self.last_decision_reason = reason
        logger.info(reason)
        return Intent.hold(reason=reason)

    def on_intent_executed(self, intent, success: bool, result):
        intent_type = getattr(getattr(intent, "intent_type", None), "value", "")
        if intent_type != "SWAP":
            return

        if success:
            if self.pending_target_state is not None:
                self.regime_state = self.pending_target_state
            self.pending_target_state = None
            self.consecutive_failed_swaps = 0
            cooldown_seconds = self._timeframe_to_seconds(self.rsi_timeframe) * self.cooldown_candles
            cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
            self.cooldown_until_ts = cooldown_until.isoformat()
            tx_hash = getattr(result, "tx_hash", None)
            logger.info(
                "swap success regime=%s cooldown_until=%s tx_hash=%s",
                self.regime_state.value,
                self.cooldown_until_ts,
                tx_hash,
            )
            return

        self.pending_target_state = None
        self.consecutive_failed_swaps += 1
        if (
            self.halt_on_repeated_failures
            and self.consecutive_failed_swaps >= self.max_consecutive_failed_swaps
        ):
            self.halted_due_to_failures = True
        logger.warning(
            "swap failed consecutive_failures=%s halted=%s",
            self.consecutive_failed_swaps,
            self.halted_due_to_failures,
        )

    def get_persistent_state(self) -> dict[str, Any]:
        return {
            "regime_state": self.regime_state.value,
            "pending_target_state": self.pending_target_state.value if self.pending_target_state else None,
            "prev_rsi": str(self.prev_rsi) if self.prev_rsi is not None else None,
            "last_processed_candle_ts": self.last_processed_candle_ts,
            "cooldown_until_ts": self.cooldown_until_ts,
            "consecutive_failed_swaps": self.consecutive_failed_swaps,
            "halted_due_to_failures": self.halted_due_to_failures,
            "last_decision_reason": self.last_decision_reason,
        }

    def load_persistent_state(self, state: dict[str, Any]) -> None:
        if not state:
            return

        regime = state.get("regime_state", RegimeState.NEUTRAL.value)
        self.regime_state = RegimeState(regime)

        pending = state.get("pending_target_state")
        self.pending_target_state = RegimeState(pending) if pending else None

        prev_rsi = state.get("prev_rsi")
        self.prev_rsi = Decimal(str(prev_rsi)) if prev_rsi is not None else None

        self.last_processed_candle_ts = state.get("last_processed_candle_ts")
        self.cooldown_until_ts = state.get("cooldown_until_ts")
        self.consecutive_failed_swaps = int(state.get("consecutive_failed_swaps", 0))
        self.halted_due_to_failures = bool(state.get("halted_due_to_failures", False))
        self.last_decision_reason = str(state.get("last_decision_reason", ""))

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "Arb-TA-Swap-RSI",
            "chain": self.execution_chain,
            "pair": f"{self.base_token}/{self.quote_token}",
            "pool_fee_tier_bps": self.pool_fee_tier_bps,
            "regime_state": self.regime_state.value,
            "pending_target_state": self.pending_target_state.value if self.pending_target_state else None,
            "prev_rsi": str(self.prev_rsi) if self.prev_rsi is not None else None,
            "last_processed_candle_ts": self.last_processed_candle_ts,
            "cooldown_until_ts": self.cooldown_until_ts,
            "consecutive_failed_swaps": self.consecutive_failed_swaps,
            "halted_due_to_failures": self.halted_due_to_failures,
            "last_decision_reason": self.last_decision_reason,
            "last_trade_amount": str(self.last_trade_amount),
            "last_trade_amount_usd": str(self.last_trade_amount_usd),
            "last_expected_out": str(self.last_expected_out),
            "last_impact_bps": str(self.last_impact_bps),
            "last_slippage_bps": str(self.last_slippage_bps),
        }

    def supports_teardown(self) -> bool:
        return True

    def get_open_positions(self) -> TeardownPositionSummary:
        return TeardownPositionSummary.empty(getattr(self, "strategy_id", self.STRATEGY_NAME))

    def generate_teardown_intents(self, mode: TeardownMode, market=None) -> list[Intent]:
        return []

    def _timeframe_bucket_ts(self, timestamp: datetime) -> str:
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        seconds = int(timestamp.astimezone(UTC).timestamp())
        bucket_size = self._timeframe_to_seconds(self.rsi_timeframe)
        bucket_seconds = seconds - (seconds % bucket_size)
        return datetime.fromtimestamp(bucket_seconds, tz=UTC).isoformat()

    @staticmethod
    def _timeframe_to_seconds(timeframe: str) -> int:
        unit = timeframe[-1]
        quantity = int(timeframe[:-1])
        if unit == "m":
            return quantity * 60
        if unit == "h":
            return quantity * 3600
        if unit == "d":
            return quantity * 86400
        raise ValueError(f"unsupported timeframe: {timeframe}")
