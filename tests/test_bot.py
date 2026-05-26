"""Tests for the ClawBots Polymarket Bot — live mode only."""

import json
import time
import pytest
from unittest.mock import patch, MagicMock

from src.config import Config
from src.wallet.wallet import validate_private_key, get_address_from_key
from src.scanner.scanner import MarketScanner, Mispricing, NearResolvedOpportunity
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
    def test_dry_run_always_false(self):
        """DRY_RUN must be hardcoded False — live mode only."""
        assert Config.DRY_RUN is False

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

    def test_summary_contains_capital_keys(self):
        summary = Config.summary()
        assert "default_trade_size" in summary
        assert "max_position_usd" in summary
        assert "max_daily_loss" in summary

    def test_summary_no_private_key_value(self):
        """Private key value must never appear in the summary dict."""
        key = "0x" + "a" * 64
        with patch.object(Config, "PRIVATE_KEY", key):
            summary = Config.summary()
            assert key not in str(summary)

    def test_capital_defaults_sane(self):
        """Trade size must not exceed max position; daily loss must be positive."""
        assert Config.DEFAULT_TRADE_SIZE_USD <= Config.MAX_POSITION_USD
        assert Config.MAX_DAILY_LOSS_USD > 0
        assert Config.MAX_OPEN_POSITIONS > 0


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
        trader = Trader()
        fee = trader.estimate_fee(0.5, 100, "crypto")
        assert fee > 0

    def test_fee_estimation_sports(self):
        trader = Trader()
        fee = trader.estimate_fee(0.5, 100, "sports")
        assert fee > 0

    def test_fee_estimation_crypto_less_than_sports_false(self):
        """Crypto fee rate is higher than sports — crypto > sports at same size."""
        trader = Trader()
        assert trader.estimate_fee(0.5, 100, "crypto") > trader.estimate_fee(0.5, 100, "sports")

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
        """Regression: positive P&L must NOT trigger the daily-loss halt."""
        trader = Trader()
        trader._daily_pnl = +5.0
        with patch.object(Config, "MAX_DAILY_LOSS_USD", 2.0), \
             patch.object(Config, "MAX_POSITION_USD", 10.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS", 10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0):
            assert trader._check_safety_limits(0.5) is True

    def test_safety_limit_open_positions(self):
        trader = Trader()
        trader._open_position_count = 4
        with patch.object(Config, "MAX_OPEN_POSITIONS", 4):
            assert trader._check_safety_limits(1.0) is False

    def test_safety_limit_total_exposure(self):
        trader = Trader()
        trader._total_exposure_usd = 7.0
        with patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 8.0):
            assert trader._check_safety_limits(2.0) is False

    def test_trade_timestamp_unique(self):
        """Each Trade gets its own timestamp via field(default_factory=time.time)."""
        t1 = Trade("q1", "c1", "tok1", "BUY", 0.5, 1.0, "GTC", "pending")
        time.sleep(0.01)
        t2 = Trade("q2", "c2", "tok2", "BUY", 0.5, 1.0, "GTC", "pending")
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

    def test_live_trade_requires_authentication(self):
        """Without authentication, mispricing trade must return None."""
        trader = Trader()
        trader.clob = MagicMock()
        trader.clob._authenticated = False
        opp = make_mispricing(yes_price=0.55, no_price=0.35, price_sum=0.90, edge_pct=10.0)
        with patch.object(Config, "MAX_POSITION_USD",       10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD",     50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS",     10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0), \
             patch.object(Config, "DEFAULT_TRADE_SIZE_USD", 2.0):
            result = trader.execute_mispricing_trade(opp, 5.0, category="geopolitics")
        assert result is None

    def test_live_trade_places_order_when_authenticated(self):
        """With auth + mocked _place_order, trade executes and counters increment."""
        trader = Trader()
        mock_clob = MagicMock()
        mock_clob._authenticated = True
        trader.clob = mock_clob

        before = trader._open_position_count
        fake_trade = Trade("Will X happen?", "0x" + "a" * 64, "0xyes",
                           "BUY", 0.55, 5.0, "GTC", "filled")

        with patch.object(trader, "_place_order", return_value=fake_trade), \
             patch.object(trader, "_check_live_balance", return_value=True), \
             patch.object(Config, "MAX_POSITION_USD",       10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD",     50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS",     10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0), \
             patch.object(Config, "DEFAULT_TRADE_SIZE_USD", 2.0):
            opp = make_mispricing(yes_price=0.55, no_price=0.35,
                                  price_sum=0.90, edge_pct=10.0)
            result = trader.execute_mispricing_trade(opp, 5.0, category="geopolitics")

        assert result is not None
        assert result.status == "filled"
        # BUY_BOTH direction places 2 orders (YES + NO) → +2 positions
        assert trader._open_position_count >= before + 1


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
    def test_no_private_key_returns_failed(self):
        """Redemption without private key must return failed status, not dry_run."""
        from web3 import Web3
        checksum_addr = Web3.to_checksum_address("0x" + "a" * 40)
        with patch.object(Config, "PRIVATE_KEY", ""):
            redeemer = AutoRedeemer(address=checksum_addr)
        pos     = make_position(is_redeemable=True, size=10.0, current_price=1.0)
        results = redeemer.run([pos])

        assert len(results) == 1
        assert results[0].status == "failed"
        assert "POLY_PRIVATE_KEY" in results[0].error

    def test_no_redeemable_returns_empty(self):
        redeemer = AutoRedeemer(address="0x" + "a" * 40)
        pos      = make_position(is_redeemable=False)
        results  = redeemer.run([pos])
        assert results == []

    def test_missing_condition_id_skips(self):
        redeemer = AutoRedeemer(address="0x" + "a" * 40)
        pos      = make_position(is_redeemable=True, condition_id="")
        results  = redeemer.run([pos])
        assert results[0].status == "failed"
        assert "condition_id" in results[0].error.lower()

    def test_estimated_usdc_calculation(self):
        """estimated_usdc is set before the private_key check — value must be correct."""
        redeemer = AutoRedeemer(address="0x" + "a" * 40)
        pos      = make_position(is_redeemable=True, size=7.5, current_price=1.0)
        results  = redeemer.run([pos])
        assert abs(results[0].estimated_usdc - 7.5) < 0.01

    def test_succeeded_only_on_success_status(self):
        """succeeded property must be True only for 'success', not 'failed'."""
        ok  = RedemptionResult("0xcond", "Market", "Yes", 5.0, 5.0, "success")
        bad = RedemptionResult("0xcond", "Market", "Yes", 5.0, 5.0, "failed")
        skp = RedemptionResult("0xcond", "Market", "Yes", 5.0, 5.0, "skipped")
        assert ok.succeeded is True
        assert bad.succeeded is False
        assert skp.succeeded is False

    def test_total_redeemed_accumulates_on_success(self):
        """_total_redeemed_usd accumulates only when result.succeeded is True."""
        redeemer = AutoRedeemer(address="0x" + "a" * 40, private_key="0x" + "k" * 64)
        fake_ok = RedemptionResult("0xcond1", "M1", "Yes", 5.0, 5.0, "success")
        fake_ok2 = RedemptionResult("0xcond2", "M2", "Yes", 3.0, 3.0, "success")
        with patch.object(redeemer, "_redeem_position", side_effect=[fake_ok, fake_ok2]):
            positions = [
                make_position(condition_id="0x" + "c1" * 32, size=5.0),
                make_position(condition_id="0x" + "c2" * 32, size=3.0),
            ]
            redeemer.run(positions)
        assert abs(redeemer.get_total_redeemed() - 8.0) < 0.01

    def test_hex_to_bytes32_pads_correctly(self):
        b = AutoRedeemer._hex_to_bytes32("0x" + "ab" * 32)
        assert len(b) == 32
        assert b == bytes.fromhex("ab" * 32)

    def test_hex_to_bytes32_short_string(self):
        b = AutoRedeemer._hex_to_bytes32("0x1234")
        assert len(b) == 32
        assert b[-1] == 0x34

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


# ──────────────────────────────────────────────────────────────
# Near-Resolved Strategy Tests
# ──────────────────────────────────────────────────────────────

def make_near_resolved(
    winning_side: str = "YES",
    winning_price: float = 0.96,
    return_pct: float = 4.17,
    hours_to_close: float = 24.0,
    volume_24h: float = 500.0,
) -> NearResolvedOpportunity:
    return NearResolvedOpportunity(
        condition_id="0x" + "d1" * 32,
        market_question="Will X happen?",
        event_title="X Event",
        market_slug="will-x-happen",
        winning_side=winning_side,
        winning_price=winning_price,
        winning_token_id="0xtoken123",
        return_pct=return_pct,
        volume_24h=volume_24h,
        end_date="2026-05-26T00:00:00",
        hours_to_close=hours_to_close,
    )


class TestNearResolvedOpportunity:
    def test_maker_price_is_one_tick_below(self):
        opp = make_near_resolved(winning_price=0.96)
        assert abs(opp.maker_price - 0.95) < 1e-9

    def test_maker_return_pct_higher_than_taker(self):
        opp = make_near_resolved(winning_price=0.96)
        assert opp.maker_return_pct > opp.return_pct

    def test_return_pct_calculated_correctly(self):
        opp = make_near_resolved(winning_price=0.96)
        expected = ((1.0 - 0.96) / 0.96) * 100
        assert abs(opp.return_pct - expected) < 0.01

    def test_winning_side_no(self):
        opp = make_near_resolved(winning_side="NO", winning_price=0.97)
        assert opp.winning_side == "NO"
        assert abs(opp.maker_price - 0.96) < 1e-9


class TestNearResolvedScanner:
    def _make_scanner(self):
        with patch("src.scanner.scanner.GammaClient"), \
             patch("src.scanner.scanner.ClobClient"):
            return MarketScanner()

    def _market_data(self, yes_price: float, volume: float = 500.0, hours: float = 24.0) -> dict:
        import datetime
        close_time = datetime.datetime.utcnow() + datetime.timedelta(hours=hours)
        return {
            "conditionId": "0xcond1",
            "question": "Will this resolve?",
            "groupItemTitle": "Test Event",
            "slug": "test-market",
            "outcomePrices": [str(yes_price), str(round(1.0 - yes_price, 3))],
            "volume24hr": str(volume),
            "volumeNum": str(volume * 5),
            "endDate": close_time.isoformat(),
            "tokens": [
                {"token_id": "0xtokenYES", "outcome": "Yes"},
                {"token_id": "0xtokenNO",  "outcome": "No"},
            ],
        }

    def test_qualifies_when_above_threshold(self):
        scanner = self._make_scanner()
        market = self._market_data(yes_price=0.96)
        opp = scanner._check_near_resolved(market, min_confidence=0.94, min_volume=200.0, max_hours=72.0)
        assert opp is not None
        assert opp.winning_side == "YES"
        assert abs(opp.winning_price - 0.96) < 1e-6

    def test_no_opportunity_when_below_threshold(self):
        scanner = self._make_scanner()
        market = self._market_data(yes_price=0.90)
        opp = scanner._check_near_resolved(market, min_confidence=0.94, min_volume=200.0, max_hours=72.0)
        assert opp is None

    def test_no_opportunity_when_volume_too_low(self):
        scanner = self._make_scanner()
        market = self._market_data(yes_price=0.96, volume=10.0)
        opp = scanner._check_near_resolved(market, min_confidence=0.94, min_volume=200.0, max_hours=72.0)
        assert opp is None

    def test_no_opportunity_when_too_far_from_close(self):
        scanner = self._make_scanner()
        market = self._market_data(yes_price=0.96, hours=200.0)
        opp = scanner._check_near_resolved(market, min_confidence=0.94, min_volume=200.0, max_hours=72.0)
        assert opp is None

    def test_qualifies_no_side(self):
        scanner = self._make_scanner()
        market = {
            "conditionId": "0xcond2",
            "question": "No side wins?",
            "groupItemTitle": "",
            "slug": "no-side",
            "outcomePrices": ["0.03", "0.97"],
            "volume24hr": "600",
            "volumeNum": "3000",
            "endDate": (__import__("datetime").datetime.utcnow() + __import__("datetime").timedelta(hours=10)).isoformat(),
            "tokens": [
                {"token_id": "0xtY", "outcome": "Yes"},
                {"token_id": "0xtN", "outcome": "No"},
            ],
        }
        opp = scanner._check_near_resolved(market, min_confidence=0.94, min_volume=200.0, max_hours=72.0)
        assert opp is not None
        assert opp.winning_side == "NO"

    def test_scan_near_resolved_calls_fetch(self):
        scanner = self._make_scanner()
        market = self._market_data(yes_price=0.97, volume=800.0, hours=12.0)
        with patch.object(scanner, "_fetch_markets", return_value=[market]):
            results = scanner.scan_near_resolved(min_confidence=0.94, max_hours=72.0, min_volume=200.0)
        assert len(results) == 1

    def test_hours_to_close_future(self):
        import datetime
        scanner = self._make_scanner()
        future = datetime.datetime.utcnow() + datetime.timedelta(hours=48)
        market = {"endDate": future.isoformat()}
        hours = scanner._hours_to_close(market)
        assert 47.5 < hours < 48.5

    def test_hours_to_close_no_date(self):
        scanner = self._make_scanner()
        hours = scanner._hours_to_close({})
        assert hours == float("inf")

    def test_hours_to_close_expired(self):
        import datetime
        scanner = self._make_scanner()
        past = datetime.datetime.utcnow() - datetime.timedelta(hours=2)
        market = {"endDate": past.isoformat()}
        hours = scanner._hours_to_close(market)
        assert hours < 0


class TestNearResolvedTrader:
    def _make_trader(self):
        with patch("src.trader.trader.ClobClient"):
            trader = Trader(clob=MagicMock())
            trader.clob._authenticated = True
            return trader

    def _fake_trade(self, price: float, size: float, side: str = "BUY") -> Trade:
        return Trade("Will X happen?", "0x" + "d1" * 32, "0xtoken123",
                     side, price, size, "GTC", "filled")

    def test_unauthenticated_returns_none(self):
        """Without auth, near-resolved trade must return None."""
        trader = self._make_trader()
        trader.clob._authenticated = False
        opp = make_near_resolved()
        result = trader.execute_near_resolved_trade(opp, investment_usd=1.0)
        assert result is None

    def test_uses_maker_price_by_default(self):
        trader = self._make_trader()
        opp = make_near_resolved(winning_price=0.97)
        expected_maker = round(0.97 - 0.01, 10)
        fake = self._fake_trade(price=expected_maker, size=1.0)
        with patch.object(trader, "_place_order", return_value=fake), \
             patch.object(trader, "_check_live_balance", return_value=True), \
             patch.object(Config, "MAX_POSITION_USD", 10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD", 50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS", 10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0):
            trade = trader.execute_near_resolved_trade(opp, investment_usd=1.0)
        assert trade is not None
        assert abs(trade.price - expected_maker) < 1e-9

    def test_uses_winning_price_when_maker_disabled(self):
        trader = self._make_trader()
        opp = make_near_resolved(winning_price=0.96)
        fake = self._fake_trade(price=0.96, size=1.0)
        with patch.object(trader, "_place_order", return_value=fake), \
             patch.object(trader, "_check_live_balance", return_value=True), \
             patch.object(Config, "MAX_POSITION_USD", 10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD", 50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS", 10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0):
            trade = trader.execute_near_resolved_trade(opp, investment_usd=1.0, use_maker_price=False)
        assert trade is not None
        assert abs(trade.price - 0.96) < 1e-9

    def test_respects_max_position_usd(self):
        """Investment capped at MAX_POSITION_USD even if caller passes more."""
        trader = self._make_trader()
        opp = make_near_resolved()
        with patch.object(Config, "MAX_POSITION_USD", 2.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD", 50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS", 10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0):
            trader.clob._authenticated = False
            result = trader.execute_near_resolved_trade(opp, investment_usd=50.0)
        assert result is None

    def test_updates_open_position_count(self):
        trader = self._make_trader()
        opp = make_near_resolved()
        before = trader._open_position_count
        fake = self._fake_trade(price=opp.maker_price, size=1.0)
        with patch.object(trader, "_place_order", return_value=fake), \
             patch.object(trader, "_check_live_balance", return_value=True), \
             patch.object(Config, "MAX_POSITION_USD", 10.0), \
             patch.object(Config, "MAX_DAILY_LOSS_USD", 50.0), \
             patch.object(Config, "MAX_OPEN_POSITIONS", 10), \
             patch.object(Config, "MAX_TOTAL_EXPOSURE_USD", 100.0):
            trader.execute_near_resolved_trade(opp, investment_usd=1.0)
        assert trader._open_position_count == before + 1

    def test_blocked_by_safety_limits(self):
        trader = self._make_trader()
        opp = make_near_resolved()
        trader._daily_pnl = -99.0
        result = trader.execute_near_resolved_trade(opp, investment_usd=1.0)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
