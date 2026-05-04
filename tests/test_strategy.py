import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from almanak.framework.teardown import TeardownMode
from strategy import ArbTASwapRSIStrategy, RegimeState


def _balance(amount: str, amount_usd: str):
    return SimpleNamespace(balance=Decimal(amount), balance_usd=Decimal(amount_usd))


def _quote(amount_out: str = "1", impact_bps: str = "10", slippage_bps: str = "10"):
    return SimpleNamespace(
        amount_out=Decimal(amount_out),
        price_impact_bps=Decimal(impact_bps),
        slippage_estimate_bps=Decimal(slippage_bps),
    )


def _market(now: datetime):
    market = SimpleNamespace()
    market.chain = "arbitrum"
    market.timestamp = now
    market.wallet_address = "0x" + "1" * 40

    market._weth = _balance("1", "2300")
    market._usdc = _balance("1000", "1000")
    market._rsi = Decimal("50")
    market._quote = _quote()
    market._gas_ok = True
    market._pool_ok = True
    market._slippage = SimpleNamespace(price_impact_bps=12, effective_slippage_bps=14)

    def balance(token):
        if token == "WETH":
            return market._weth
        return market._usdc

    def rsi(*args, **kwargs):
        return SimpleNamespace(value=market._rsi)

    def ohlcv(*args, **kwargs):
        latest = market.timestamp.replace(second=0, microsecond=0)
        previous = latest - timedelta(minutes=5)
        return pd.DataFrame(
            [
                {"timestamp": previous, "close": 2300.0},
                {"timestamp": latest, "close": 2310.0},
            ]
        )

    def pool_price_by_pair(*args, **kwargs):
        if not market._pool_ok:
            raise ValueError("pool unavailable")
        return SimpleNamespace(value=Decimal("2300"))

    def price_across_dexs(*args, **kwargs):
        return SimpleNamespace(best_quote=market._quote, quotes={"uniswap_v3": market._quote})

    market.balance = balance
    market.rsi = rsi
    market.ohlcv = ohlcv
    market.pool_price_by_pair = pool_price_by_pair
    market.pool_price = lambda *args, **kwargs: SimpleNamespace(value=Decimal("2300"))
    market.pool_reserves = lambda *args, **kwargs: SimpleNamespace(liquidity=Decimal("100"))
    market.price_across_dexs = price_across_dexs
    market.estimate_slippage = lambda *args, **kwargs: SimpleNamespace(value=market._slippage)
    market.is_trade_worthwhile = lambda **kwargs: market._gas_ok
    market.estimate_swap_gas_cost_usd = lambda *args, **kwargs: Decimal("0.20")
    return market


@pytest.fixture
def strategy_config() -> dict:
    return {
        "chain": "arbitrum",
        "base_token": "WETH",
        "quote_token": "USDC",
        "pool_fee_tier_bps": 500,
        "rsi_period": 14,
        "rsi_timeframe": "5m",
        "rsi_lower_band": 45,
        "rsi_upper_band": 55,
        "allocation_pct": "0.95",
        "max_slippage": "0.003",
        "max_price_impact": "0.003",
        "max_quote_slippage_bps": 30,
        "max_quote_impact_bps": 30,
        "min_expected_out": "0",
        "min_trade_value_usd": "25",
        "max_gas_ratio": "0.05",
        "quote_timeout_seconds": 0.05,
        "cooldown_candles": 1,
        "force_action": "",
        "halt_on_repeated_failures": True,
        "max_consecutive_failed_swaps": 3,
    }


@pytest.fixture
def strategy(strategy_config: dict) -> ArbTASwapRSIStrategy:
    return ArbTASwapRSIStrategy(
        config=strategy_config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )


def _next_candle(market, minutes: int = 5):
    market.timestamp = market.timestamp + timedelta(minutes=minutes)


def test_initial_cycle_holds_until_prev_rsi_exists(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    market._rsi = Decimal("50")
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"
    assert strategy.prev_rsi == Decimal("50")


def test_cross_above_upper_flips_to_long_weth(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    market._rsi = Decimal("54")
    strategy.decide(market)

    _next_candle(market)
    market._rsi = Decimal("56")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "WETH"


def test_cross_below_lower_flips_to_long_usdc(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    market._rsi = Decimal("46")
    strategy.decide(market)

    _next_candle(market)
    market._rsi = Decimal("44")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "WETH"
    assert intent.to_token == "USDC"


def test_neutral_band_sets_neutral_state_and_holds(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.regime_state = RegimeState.LONG_WETH
    market._rsi = Decimal("50")

    strategy.decide(market)
    _next_candle(market)
    market._rsi = Decimal("50")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert strategy.regime_state == RegimeState.NEUTRAL


def test_no_repeat_swap_when_already_in_target_state(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.regime_state = RegimeState.LONG_WETH
    market._rsi = Decimal("54")
    strategy.decide(market)

    _next_candle(market)
    market._rsi = Decimal("56")
    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"


def test_cooldown_blocks_new_flip(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.prev_rsi = Decimal("54")
    strategy.last_processed_candle_ts = (market.timestamp - timedelta(minutes=5)).isoformat()
    strategy.cooldown_until_ts = (market.timestamp + timedelta(minutes=5)).isoformat()
    market._rsi = Decimal("56")

    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_hold_when_balance_insufficient(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.prev_rsi = Decimal("54")
    strategy.last_processed_candle_ts = (market.timestamp - timedelta(minutes=5)).isoformat()
    market._rsi = Decimal("56")
    market._usdc = _balance("1", "1")

    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_hold_when_pool_unavailable(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.prev_rsi = Decimal("54")
    strategy.last_processed_candle_ts = (market.timestamp - timedelta(minutes=5)).isoformat()
    market._rsi = Decimal("56")
    market._pool_ok = False

    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_pool_address_falls_back_to_pair_lookup_when_pool_price_missing(strategy_config):
    strategy_config["pool_address"] = "0xc6962004f452be9203591991d15f6b388e09e8d0"
    strategy = ArbTASwapRSIStrategy(
        config=strategy_config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    delattr(market, "pool_price")

    strategy.prev_rsi = Decimal("54")
    strategy.last_processed_candle_ts = (market.timestamp - timedelta(minutes=5)).isoformat()
    market._rsi = Decimal("56")

    intent = strategy.decide(market)
    assert intent.intent_type.value == "SWAP"


def test_hold_when_quote_slippage_above_limit(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.prev_rsi = Decimal("54")
    strategy.last_processed_candle_ts = (market.timestamp - timedelta(minutes=5)).isoformat()
    market._rsi = Decimal("56")
    market._quote = _quote(amount_out="0.1", impact_bps="10", slippage_bps="60")

    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_hold_when_gas_not_worthwhile(strategy):
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    strategy.prev_rsi = Decimal("54")
    strategy.last_processed_candle_ts = (market.timestamp - timedelta(minutes=5)).isoformat()
    market._rsi = Decimal("56")
    market._gas_ok = False

    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_force_action_buy_bypasses_signal_gates(strategy, strategy_config):
    strategy_config["force_action"] = "buy"
    forced = ArbTASwapRSIStrategy(
        config=strategy_config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    market._rsi = Decimal("20")

    intent = forced.decide(market)
    assert intent.intent_type.value == "SWAP"
    assert intent.from_token == "USDC"
    assert intent.to_token == "WETH"


def test_force_action_skips_quote_and_pool_calls(strategy_config):
    strategy_config["force_action"] = "buy"
    strategy_config["quote_timeout_seconds"] = 0.02
    forced = ArbTASwapRSIStrategy(
        config=strategy_config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))

    calls = {"quote": 0, "pool": 0}

    def very_slow_quote(*args, **kwargs):
        calls["quote"] += 1
        time.sleep(0.5)
        return SimpleNamespace(best_quote=_quote(), quotes={"uniswap_v3": _quote()})

    def should_not_call_pool(*args, **kwargs):
        calls["pool"] += 1
        raise AssertionError("force_action should not call pool checks")

    market.price_across_dexs = very_slow_quote
    market.pool_price_by_pair = should_not_call_pool

    started = time.perf_counter()
    intent = forced.decide(market)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert calls["quote"] == 0
    assert calls["pool"] == 0
    assert intent.intent_type.value == "SWAP"


def test_force_action_still_enforces_min_trade_value(strategy_config):
    strategy_config["force_action"] = "buy"
    forced = ArbTASwapRSIStrategy(
        config=strategy_config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )
    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    market._usdc = _balance("1", "1")

    intent = forced.decide(market)
    assert intent.intent_type.value == "HOLD"


def test_failure_halt_gate_and_success_state_update(strategy):
    swap_intent = SimpleNamespace(intent_type=SimpleNamespace(value="SWAP"))

    strategy.on_intent_executed(swap_intent, success=False, result=SimpleNamespace())
    strategy.on_intent_executed(swap_intent, success=False, result=SimpleNamespace())
    strategy.on_intent_executed(swap_intent, success=False, result=SimpleNamespace())
    assert strategy.halted_due_to_failures is True

    market = _market(datetime(2026, 1, 1, tzinfo=UTC))
    intent = strategy.decide(market)
    assert intent.intent_type.value == "HOLD"

    strategy.halted_due_to_failures = False
    strategy.pending_target_state = RegimeState.LONG_WETH
    strategy.on_intent_executed(swap_intent, success=True, result=SimpleNamespace(tx_hash="0xabc"))
    assert strategy.regime_state == RegimeState.LONG_WETH
    assert strategy.cooldown_until_ts is not None


def test_persistence_roundtrip(strategy, strategy_config):
    strategy.regime_state = RegimeState.LONG_USDC
    strategy.prev_rsi = Decimal("44")
    strategy.last_processed_candle_ts = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    saved = strategy.get_persistent_state()

    restored = ArbTASwapRSIStrategy(
        config=strategy_config,
        chain="arbitrum",
        wallet_address="0x" + "1" * 40,
    )
    restored.load_persistent_state(saved)

    assert restored.regime_state == RegimeState.LONG_USDC
    assert restored.prev_rsi == Decimal("44")
    assert restored.last_processed_candle_ts == strategy.last_processed_candle_ts


def test_teardown_is_empty_for_swap_only(strategy):
    summary = strategy.get_open_positions()
    intents = strategy.generate_teardown_intents(TeardownMode.SOFT)

    assert summary.positions == []
    assert intents == []
