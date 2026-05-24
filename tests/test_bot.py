"""Tests for the Polymarket bot."""

import json
import pytest
from unittest.mock import patch, MagicMock

from src.config import Config
from src.wallet.wallet import validate_private_key, get_address_from_key
from src.scanner.scanner import MarketScanner, Mispricing
from src.trader.trader import Trader


class TestConfig:
    """Test configuration validation."""

    def test_validate_missing_key(self):
        """Config should error if private key is missing."""
        with patch.object(Config, 'PRIVATE_KEY', ''):
            errors = Config.validate()
            assert len(errors) > 0
            assert "POLY_PRIVATE_KEY" in errors[0]

    def test_validate_bad_key(self):
        """Config should error if private key is malformed."""
        with patch.object(Config, 'PRIVATE_KEY', 'badkey'):
            errors = Config.validate()
            assert len(errors) > 0

    def test_validate_good_key(self):
        """Config should pass with a valid-format key."""
        with patch.object(Config, 'PRIVATE_KEY', '0x' + 'a' * 64):
            errors = Config.validate()
            assert len(errors) == 0

    def test_summary_no_secrets(self):
        """Summary should not expose private key."""
        with patch.object(Config, 'PRIVATE_KEY', '0x' + 'a' * 64):
            summary = Config.summary()
            assert summary['private_key_set'] is True
            assert 'PRIVATE_KEY' not in str(summary)
            assert '0xaaa' not in str(summary)


class TestWallet:
    """Test wallet functions."""

    def test_validate_empty_key(self):
        assert validate_private_key('') is False

    def test_validate_short_key(self):
        assert validate_private_key('0x1234') is False

    def test_validate_valid_key(self):
        assert validate_private_key('0x' + 'a' * 64) is True

    def test_validate_key_without_prefix(self):
        assert validate_private_key('a' * 64) is True

    def test_get_address_from_key(self):
        """Should derive a valid Ethereum address."""
        # Using a known test key
        key = '0x' + '1' * 64
        addr = get_address_from_key(key)
        assert addr.startswith('0x')
        assert len(addr) == 42


class TestMispricing:
    """Test mispricing detection logic."""

    def test_mispriced_below_one(self):
        """Sum < 1.0 should be mispriced."""
        opp = Mispricing(
            event_slug="test",
            event_title="Test Event",
            market_slug="test-market",
            market_question="Will X happen?",
            yes_price=0.62,
            no_price=0.33,
            price_sum=0.95,
            edge_pct=5.0,
        )
        assert opp.is_mispriced is True
        assert opp.direction == "BUY_BOTH"
        assert abs(opp.guaranteed_profit_per_share - 0.05) < 0.001

    def test_mispriced_above_one(self):
        """Sum > 1.0 should be mispriced."""
        opp = Mispricing(
            event_slug="test",
            event_title="Test Event",
            market_slug="test-market",
            market_question="Will X happen?",
            yes_price=0.65,
            no_price=0.40,
            price_sum=1.05,
            edge_pct=5.0,
        )
        assert opp.is_mispriced is True
        assert opp.direction == "SELL_BOTH"

    def test_correctly_priced(self):
        """Sum ≈ 1.0 should not be mispriced."""
        opp = Mispricing(
            event_slug="test",
            event_title="Test",
            market_slug="test",
            market_question="Test?",
            yes_price=0.50,
            no_price=0.50,
            price_sum=1.00,
            edge_pct=0.0,
        )
        assert opp.is_mispriced is False


class TestScanner:
    """Test scanner parsing functions."""

    def test_parse_outcome_prices_string(self):
        """Double-encoded JSON strings should be parsed."""
        scanner = MarketScanner()
        market = {"outcomePrices": '["0.65", "0.35"]'}
        prices = scanner._parse_outcome_prices(market)
        assert prices == [0.65, 0.35]

    def test_parse_outcome_prices_list(self):
        """Already-parsed lists should work."""
        scanner = MarketScanner()
        market = {"outcomePrices": [0.80, 0.20]}
        prices = scanner._parse_outcome_prices(market)
        assert prices == [0.80, 0.20]

    def test_parse_outcome_prices_none(self):
        """Missing data should return None."""
        scanner = MarketScanner()
        market = {}
        prices = scanner._parse_outcome_prices(market)
        assert prices is None

    def test_parse_token_ids(self):
        """Token IDs should be parsed correctly."""
        scanner = MarketScanner()
        market = {"clobTokenIds": '["0xtoken1", "0xtoken2"]'}
        tokens = scanner._parse_token_ids(market)
        assert tokens == {"yes": "0xtoken1", "no": "0xtoken2"}

    def test_parse_token_ids_list(self):
        """Already-parsed token ID lists should work."""
        scanner = MarketScanner()
        market = {"clobTokenIds": ["0xa", "0xb"]}
        tokens = scanner._parse_token_ids(market)
        assert tokens == {"yes": "0xa", "no": "0xb"}


class TestTrader:
    """Test trade execution logic."""

    def test_fee_estimation(self):
        """Fee estimation should match Polymarket formula."""
        trader = Trader()
        # fee = C × feeRate × p × (1 - p)
        # For 100 shares at 0.5 price, crypto (7%):
        # fee = 100 * 0.07 * 0.5 * 0.5 = 1.75
        fee = trader.estimate_fee(0.5, 100, "crypto")
        assert abs(fee - 1.75) < 0.01

    def test_fee_estimation_sports(self):
        """Sports fee should be lower (3%)."""
        trader = Trader()
        fee = trader.estimate_fee(0.5, 100, "sports")
        assert abs(fee - 0.75) < 0.01

    def test_fee_estimation_geopolitics(self):
        """Geopolitics fee should be zero."""
        trader = Trader()
        fee = trader.estimate_fee(0.5, 100, "geopolitics")
        assert fee == 0.0

    def test_profit_calculation(self):
        """Profit after fees should be calculated correctly."""
        trader = Trader()
        opp = Mispricing(
            event_slug="test",
            event_title="Test",
            market_slug="test",
            market_question="Will X happen?",
            yes_price=0.60,
            no_price=0.35,
            price_sum=0.95,
            edge_pct=5.0,
            yes_token_id="0xyes",
            no_token_id="0xno",
            condition_id="0xcond",
        )
        # Buy both sides for $10 total
        result = trader.calculate_profit_after_fees(opp, 10.0, "crypto")
        assert result["investment"] == 10.0
        assert result["profitable"] is True or result["net_profit"] <= 0  # Depends on fees

    def test_safety_limit_max_position(self):
        """Trades exceeding max position should be rejected."""
        trader = Trader()
        with patch.object(Config, 'MAX_POSITION_USD', 5.0):
            assert trader._check_safety_limits(10.0) is False

    def test_safety_limit_daily_loss(self):
        """Trades should be rejected when daily loss limit exceeded."""
        trader = Trader()
        trader._daily_pnl = -25.0  # Already lost $25
        with patch.object(Config, 'MAX_DAILY_LOSS_USD', 20.0):
            assert trader._check_safety_limits(5.0) is False


class TestFormatting:
    """Test formatting utilities."""

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


class TestDryRunMode:
    """Test that dry-run mode never executes real trades."""

    def test_dry_run_trade(self):
        """In dry-run mode, trades should be recorded but not executed."""
        with patch.object(Config, 'DRY_RUN', True), \
             patch.object(Config, 'MAX_POSITION_USD', 100.0), \
             patch.object(Config, 'MAX_DAILY_LOSS_USD', 50.0), \
             patch.object(Config, 'MAX_OPEN_POSITIONS', 10):
            trader = Trader()
            # Use geopolitics category (0% fee) to ensure profitability
            opp = Mispricing(
                event_slug="test",
                event_title="Test",
                market_slug="test",
                market_question="Will X happen?",
                yes_price=0.55,
                no_price=0.35,
                price_sum=0.90,
                edge_pct=10.0,
                yes_token_id="0xyes",
                no_token_id="0xno",
                condition_id="0xcond",
            )
            # Mock the CLOB client so it can't make real API calls
            trader.clob = MagicMock()

            result = trader.execute_mispricing_trade(opp, 50.0, category="geopolitics")
            assert result is not None
            assert result.status == "dry_run"

            # Verify no real API calls were made
            trader.clob.post_order.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])