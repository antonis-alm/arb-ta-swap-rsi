from unittest.mock import patch

from dashboard.ui import _build_dashboard_config, render_custom_dashboard


def test_dashboard_module_imports():
    assert callable(render_custom_dashboard)


def test_build_dashboard_config_uses_rsi_params():
    strategy_config = {
        "rsi_period": 21,
        "rsi_lower_band": 42,
        "rsi_upper_band": 58,
    }

    config = _build_dashboard_config(strategy_config)

    assert config.indicator_name == "RSI"
    assert config.indicator_period == 21
    assert config.lower_threshold == 42
    assert config.upper_threshold == 58


def test_render_custom_dashboard_calls_ta_template():
    strategy_config = {
        "base_token": "WETH",
        "quote_token": "USDC",
        "chain": "arbitrum",
        "protocol": "uniswap_v3",
        "rsi_period": 14,
        "rsi_lower_band": 45,
        "rsi_upper_band": 55,
    }
    session_state = {"rsi_value": 50}

    with patch("dashboard.ui.render_ta_dashboard") as mock_render:
        render_custom_dashboard("arb-ta-rsi", strategy_config, None, session_state)

    mock_render.assert_called_once()
    args = mock_render.call_args.args
    assert args[0] == "arb-ta-rsi"
    assert args[1] == strategy_config
    assert args[2] == session_state
    assert args[3].indicator_name == "RSI"
    assert args[3].indicator_period == 14
    assert args[3].lower_threshold == 45
    assert args[3].upper_threshold == 55
