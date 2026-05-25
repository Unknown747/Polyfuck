"""Tests for the ClawBots Polymarket Bot."""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from src.config import Config
from src.wallet.wallet import validate_private_key, get_address_from_key
from src.scanner.scanner import MarketScanner, Mispricing
from src.trader.trader import Trader, Trade
from src.positions.positions import Position
from src.redemption.redemption import AutoRedeemer, RedemptionResult


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def make_mispricing(**kwargs) -> Mispricing:
    defaults = dict(
        event_slug="test-event",
        event_title="Test Event",
        market_slug="test-market",
        market_question="Will X happen?",
        yes_price=0.60,
        no_price=0.35,
        price_sum=0.95,
        edge_pct=5.0,
        yes_token_id="0xyes",
        no_token_id="0xno",
        condition_id="0x" + "a" * 64,
    )
    defaults.update(kwargs)
    return Mispricing(**defaults)


def make_position(**kwargs) -> Position:
    defaults = dict(
        condition_id="0x" + "b" * 64,
        title="Test Market",
        outcome="Yes",
        size=10.0,
        avg_price=0.55,
        current_price=1.0,
        initial_value=5.50,
        current_value=10.0,
        cash_pnl=4.50,
        percent_pnl=81.8,
        is_redeemable=True,
    )
    defaults.update(kwargs)
    return Position(**defaults)


# ──────────────────────────────────────────────────────────────
# Config Tests
# ──────────────────────────────────────────────────────────────

class TestConfig:
    def test_validate_missing_key(self):
        with patch.object(Config, "PRIVATE_KEY", ""):
            errors = Config.validate()
            assert len(errors) > 0
            assert "POLY_PRIVATE_KEY" in errors[0]

    def test_validate_bad_key_format(self):
        with patch.object(Config, "PRIVATE_KEY", "badkey"):
            errors = Config.validate()
            assert len(errors) > 0

    def test_validate_good_key(self):
        with patch.object(Config, "PRIVATE_KEY", "0x" + "a" * 64):
            errors = Config.validate()
            assert len(errors) == 0

    def test_validate_trade_size_exceeds_max_position(self):
        """DEFAULT_TRADE_SIZE_USD > MAX_POSITION_USD should error."""
        with patch.object(Config, "PRIVATE_KEY", "0x" + "a" * 64), \
             patch.object(Config, "DEFAULT_TRADE_SIZE_USD", 10.0), \
             patch.object(Config, "MAX_POSITION_USD", 3.0):
            errors = Config.validate()
            assert any("DEFAULT_TRADE_SIZE_USD" in e for e in errors)

    def test_summary_no_secrets(self):
        with patch.object(Config, "PRIVATE_KEY", "0x" + "a" * 64):
            summary = Config.summary()
            assert summary["private_key_set"] is True
            assert "0xaaa" not in str(summary)

    def test_ten_dollar_defaults(self):
        """Default capital settings should be calibrated for ~$10 accounts."""
        assert Config.DEFAULT_TRADE_SIZE_USD <= 3.0
        assert Config.MAX_POSITION_USD <= 5.0
        assert Config.MAX_DAILY_LOSS_USD <= 3.0
        assert Config.MAX_OPEN_POSITIONS <= 5


# ──────────────────────────────────────────────────────────────
# Wallet Tests
# ──────────────────────────────────────────────────────────────

class TestWallet:
    def test_validate_empty(self):
        assert validate_private_key("") is False

    def test_validate_short(self):
        assert validate_private_key("0x1234") is False

    def test_validate_valid(self):
        assert validate_private_key("0x" + "a" * 64) is True

    def test_validate_without_prefix(self):
        assert validate_private_key("a" * 64) is True

    def test_get_address_from_key(self):
        addr = get_address_from_key("0x" + "1" * 64)
        assert addr.startswith("0x")
        assert len(addr) == 42


# ──────────────────────────────────────────────────────────────
# Mispricing Tests
# ──────────────────────────────────────────────────────────────

class TestMispricing:
    def test_buy_both_direction(self):
        opp = make_mispricing(yes_price=0.62, no_price=0.33, price_sum=0.95)
        assert opp.is_mispriced is True
        assert opp.direction == "BUY_BOTH"
        assert abs(opp.guaranteed_profit_per_share - 0.05) < 0.001

    def test_sell_both_direction(self):
        opp = make_mispricing(yes_price=0.65, no_price=0.40, price_sum=1.05)
        assert opp.direction == "SELL_BOTH"

    def test_correctly_priced(self):
        opp = make_mispricing(yes_price=0.50, no_price=0.50, price_sum=1.00, edge_pct=0.0)
        assert opp.is_mispriced is False


# ──────────────────────────────────────────────────────────────
# Scanner Tests
# ──────────────────────────────────────────────────────────────

class TestScanner:
    def test_parse_outcome_prices_string(self):
        scanner = MarketScanner()
        prices = scanner._parse_outcome_prices({"outcomePrices": '["0.65", "0.35"]'})
        assert prices == [0.65, 0.35]

    def test_parse_outcome_prices_list(self):
        scanner = MarketScanner()
        prices = scanner._parse_outcome_prices({"outcomePrices": [0.80, 0.20]})
        assert prices == [0.80, 0.20]

    def test_parse_outcome_prices_missing(self):
        scanner = MarketScanner()
        assert scanner._parse_outcome_prices({}) is None

    def test_parse_token_ids_string(self):
        scanner = MarketScanner()
        tokens = scanner._parse_token_ids({"clobTokenIds": '["0xtoken1", "0xtoken2"]'})
        assert tokens == {"yes": "0xtoken1", "no": "0xtoken2"}

    def test_parse_token_ids_list(self):
        scanner = MarketScanner()
        tokens = scanner._parse_token_ids({"clobTokenIds": ["0xa", "0xb"]})
        assert tokens == {"yes": "0xa", "no": "0xb"}

    def test_parse_token_ids_empty(self):
        scanner = MarketScanner()
        assert scanner._parse_token_ids({}) == {}


# ──────────────────────────────────────────────────────────────
# Trader Tests
# ──────────────────────────────────────────────────────────────

class TestTrader:
    def test_fee_estimation_crypto(self):
        # fee = C × feeRate × p × (1-p) = 100 × 0.07 × 0.5 × 0.5 = 1.75
        trader = Trader()
        assert abs(trader.estimate_fee(0.5, 100, "crypto") - 1.75) < 0.01

    def test_fee_estimation_sports(self):
        trader = Trader()
        assert abs(trader.estimate_fee(0.5, 100, "sports") - 0.75) < 0.01

    def test_fee_estimation_geopolitics_zero(self):
        trader = Trader()
        assert trader.estimate_fee(0.5, 100, "geopolitics") == 0.0

    def test_profit_calculation_structure(self):
        trader = Trader()
        opp    = make_mispricing()
        result = trader.calculate_profit_after_fees(opp, 2.0, "geopolitics")
        assert result["investment"] == 2.0
        assert "net_profit" in result
        assert "profitable" in result

    def test_profit_calculation_zero_sum(self):
        """Zero price_sum should return unprofitable immediately."""
        trader = Trader()
        opp    = make_mispricing(price_sum=0.0)
        result = trader.calculate_profit_after_fees(opp, 2.0)
        assert result["profitable"] is False

    def test_safety_limit_max_position(self):
        trader = Trader()
        with patch.object(Config, "MAX_POSITION_USD", 1.0):
            assert trader._check_safety_limits(5.0) is False

    def test_safety_limit_daily_loss(self):
        trader = Trader()
        trader._daily_pnl = -5.0
        with patch.object(Config, "MAX_DAILY_LOSS_USD", 2.0):
            assert trader._check_safety_limits(0.5) is False

    def test_safety_limit_daily_loss_does_not_halt_on_profit(self):
        """Regression: positive P&L must NOT trigger the daily-loss halt.
        Bug was: abs(daily_pnl) > MAX_DAILY_LOSS — halted when profitable.
        Fix:     daily_pnl < -MAX_DAILY_LOSS — only halts on losses.
        """
        trader = Trader()
        trader._daily_pnl = +5.0  # very profitable day
        with patch.object(Config, "MAX_DAILY_LOSS_USD", 2.0), \
             patch.object(Config, "MAX_POSITION_USD", 10.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS", 10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0):
            assert trader._check_safety_limits(0.5) is True  # must NOT halt

    def test_safety_limit_open_positions(self):
        trader = Trader()
        trader._open_position_count = 4
        with patch.object(Config, "MAX_OPEN_POSITIONS", 4):
            assert trader._check_safety_limits(1.0) is False

    def test_safety_limit_total_exposure(self):
        trader = Trader()
        trader._total_exposure_usd = 7.0
        with patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 8.0):
            assert trader._check_safety_limits(2.0) is False   # 7+2 > 8

    def test_trade_timestamp_unique(self):
        """Each Trade should get its own timestamp (not a shared class-level value)."""
        t1 = Trade("q1", "c1", "tok1", "BUY", 0.5, 1.0, "GTC", "dry_run")
        time.sleep(0.01)
        t2 = Trade("q2", "c2", "tok2", "BUY", 0.5, 1.0, "GTC", "dry_run")
        assert t2.timestamp > t1.timestamp

    def test_record_redemption_updates_exposure(self):
        trader = Trader()
        trader._total_exposure_usd    = 5.0
        trader._open_position_count   = 2
        trader._daily_pnl             = -3.0
        trader.record_redemption(2.0)
        assert trader._total_exposure_usd   == 3.0
        assert trader._open_position_count  == 1
        assert abs(trader._daily_pnl - (-1.0)) < 0.001


# ──────────────────────────────────────────────────────────────
# Dry-Run Integration Test
# ──────────────────────────────────────────────────────────────

class TestDryRunMode:
    def test_dry_run_trade_executes(self):
        """In dry-run mode, trades should be recorded but not hit the API."""
        with patch.object(Config, "DRY_RUN",               True), \
             patch.object(Config, "MAX_POSITION_USD",       10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD",     50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS",     10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0), \
             patch.object(Config, "DEFAULT_TRADE_SIZE_USD", 2.0):

            trader     = Trader()
            trader.clob = MagicMock()
            # 0% fee (geopolitics) + 10% edge → definitely profitable
            opp = make_mispricing(yes_price=0.55, no_price=0.35, price_sum=0.90, edge_pct=10.0)
            result = trader.execute_mispricing_trade(opp, 5.0, category="geopolitics")

            assert result is not None
            assert result.status == "dry_run"
            trader.clob.post_order.assert_not_called()

    def test_dry_run_increments_open_position_count(self):
        with patch.object(Config, "DRY_RUN",               True), \
             patch.object(Config, "MAX_POSITION_USD",       10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD",     50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS",     10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0), \
             patch.object(Config, "DEFAULT_TRADE_SIZE_USD", 2.0):

            trader      = Trader()
            trader.clob = MagicMock()
            opp = make_mispricing(yes_price=0.55, no_price=0.35, price_sum=0.90, edge_pct=10.0)
            trader.execute_mispricing_trade(opp, 5.0, category="geopolitics")
            assert trader._open_position_count == 1


# ──────────────────────────────────────────────────────────────
# Position Tests
# ──────────────────────────────────────────────────────────────

class TestPosition:
    def test_is_resolved_winner(self):
        pos = make_position(is_redeemable=True, current_price=1.0)
        assert pos.is_resolved_winner is True

    def test_not_resolved_winner_when_price_low(self):
        pos = make_position(is_redeemable=True, current_price=0.50)
        assert pos.is_resolved_winner is False

    def test_not_resolved_winner_when_not_redeemable(self):
        pos = make_position(is_redeemable=False, current_price=1.0)
        assert pos.is_resolved_winner is False

    def test_to_dict_includes_redeemable(self):
        pos = make_position(is_redeemable=True)
        d   = pos.to_dict()
        assert d["is_redeemable"] is True


# ──────────────────────────────────────────────────────────────
# Redemption Tests
# ──────────────────────────────────────────────────────────────

class TestAutoRedeemer:
    def test_dry_run_single_position(self):
        """Dry-run redemption should return dry_run status without on-chain calls."""
        with patch.object(Config, "DRY_RUN", True):
            redeemer = AutoRedeemer(address="0x" + "a" * 40)
            pos      = make_position(is_redeemable=True, size=10.0, current_price=1.0)
            results  = redeemer.run([pos])

        assert len(results) == 1
        assert results[0].status == "dry_run"
        assert results[0].shares == 10.0

    def test_no_redeemable_returns_empty(self):
        with patch.object(Config, "DRY_RUN", True):
            redeemer = AutoRedeemer(address="0x" + "a" * 40)
            pos      = make_position(is_redeemable=False)
            results  = redeemer.run([pos])

        assert results == []

    def test_missing_condition_id_skips(self):
        with patch.object(Config, "DRY_RUN", False):
            redeemer = AutoRedeemer(address="0x" + "a" * 40)
            pos      = make_position(is_redeemable=True, condition_id="")
            results  = redeemer.run([pos])

        assert results[0].status == "failed"
        assert "condition_id" in results[0].error.lower()

    def test_estimated_usdc_calculation(self):
        with patch.object(Config, "DRY_RUN", True):
            redeemer = AutoRedeemer(address="0x" + "a" * 40)
            pos      = make_position(is_redeemable=True, size=7.5, current_price=1.0)
            results  = redeemer.run([pos])

        assert abs(results[0].estimated_usdc - 7.5) < 0.01

    def test_hex_to_bytes32_pads_correctly(self):
        b = AutoRedeemer._hex_to_bytes32("0x" + "ab" * 32)
        assert len(b) == 32
        assert b == bytes.fromhex("ab" * 32)

    def test_hex_to_bytes32_short_string(self):
        """Short hex strings should be zero-padded to 32 bytes."""
        b = AutoRedeemer._hex_to_bytes32("0x1234")
        assert len(b) == 32
        assert b[-1] == 0x34

    def test_total_redeemed_accumulates(self):
        with patch.object(Config, "DRY_RUN", True):
            redeemer = AutoRedeemer(address="0x" + "a" * 40)
            positions = [
                make_position(condition_id="0x" + "c1" * 32, size=5.0),
                make_position(condition_id="0x" + "c2" * 32, size=3.0),
            ]
            redeemer.run(positions)

        assert abs(redeemer.get_total_redeemed() - 8.0) < 0.01

    def test_check_redeemable_filters_correctly(self):
        redeemer = AutoRedeemer(address="0x" + "a" * 40)
        positions = [
            make_position(condition_id="0x11" * 32, is_redeemable=True,  size=5.0),
            make_position(condition_id="0x22" * 32, is_redeemable=False, size=3.0),
            make_position(condition_id="0x33" * 32, is_redeemable=True,  size=0.0),
        ]
        redeemable = redeemer.check_redeemable(positions)
        assert len(redeemable) == 1
        assert redeemable[0].condition_id == "0x11" * 32


# ──────────────────────────────────────────────────────────────
# Formatting Tests
# ──────────────────────────────────────────────────────────────

class TestFormatting:
    def test_format_usd_small(self):
        from src.utils.formatting import format_usd
        assert format_usd(5.50) == "$5.50"

    def test_format_usd_thousands(self):
        from src.utils.formatting import format_usd
        assert format_usd(1500) == "$1.5K"

    def test_format_usd_millions(self):
        from src.utils.formatting import format_usd
        assert format_usd(2_500_000) == "$2.50M"

    def test_format_pct(self):
        from src.utils.formatting import format_pct
        assert format_pct(5.5) == "5.5%"

    def test_format_address(self):
        from src.utils.formatting import format_address
        assert format_address("0x1234567890abcdef1234567890abcdef12345678") == "0x123456...345678"
        assert format_address("") == "N/A"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
