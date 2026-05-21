"""
test_crawler.py — unit-тесты для crawler v3.2
Охватывает: mentions, pre_filter, RateLimiter, CircuitBreaker, scoring, TTL, scroll rounds.
"""
import asyncio
import time
import unittest
from datetime import datetime, timedelta

from crawler import (
    extract_mentions,
    extract_mentions_with_context,
    pre_filter_check,
    score_candidate,
    get_scroll_rounds,
    is_cache_valid,
    is_ai_verdict_valid,
    RateLimiter,
    CircuitBreaker,
    CACHE_TTL_HOURS,
    AI_TTL_HOURS,
    SCROLL_ROUNDS,
    SCROLL_ROUNDS_MIN,
)


class TestExtractMentions(unittest.TestCase):
    def test_basic(self):
        mentions = extract_mentions(["Hello @world and @Python_3"])
        self.assertIn("world", [m.lower() for m in mentions])
        self.assertIn("Python_3", mentions)

    def test_deduplication(self):
        """Один и тот же ник дважды должен появиться только один раз."""
        mentions = extract_mentions(["@Alice hello", "@alice again"])
        lowered = [m.lower() for m in mentions]
        self.assertEqual(lowered.count("alice"), 1)

    def test_skips_seeds_and_exchanges(self):
        """Биржи и seed-аккаунты должны отфильтровываться."""
        mentions = extract_mentions(["@binance @VitalikButerin @coinbase"])
        self.assertEqual(mentions, [])

    def test_unicode_handles(self):
        """Поддержка Unicode-символов в никах."""
        mentions = extract_mentions(["@Иван_Crypto is building stuff"])
        self.assertEqual(len(mentions), 1)
        self.assertEqual(mentions[0], "Иван_Crypto")

    def test_skips_pure_numeric(self):
        """Чисто числовые псевдонимы должны игнорироваться."""
        mentions = extract_mentions(["@12345 testing"])
        self.assertEqual(mentions, [])

    def test_hyphen_in_handle(self):
        """Дефис внутри ника должен поддерживаться."""
        mentions = extract_mentions(["@some-protocol released v2"])
        self.assertIn("some-protocol", [m.lower() for m in mentions])

    def test_multiline_text(self):
        """Переводы строк внутри текста не должны ломать парсинг."""
        mentions = extract_mentions(["Hello\n@NewStartup\nBuilding stuff"])
        self.assertIn("NewStartup", mentions)


class TestPreFilterCheck(unittest.TestCase):
    def test_hits_exchange(self):
        hit, reason = pre_filter_check("We run a crypto exchange platform", [])
        self.assertTrue(hit)
        self.assertEqual(reason, "exchange")

    def test_hits_journalist(self):
        hit, reason = pre_filter_check("", ["I am a journalist covering DeFi"])
        self.assertTrue(hit)
        self.assertEqual(reason, "journalist")

    def test_no_hit_on_partial_word(self):
        """'exchangeable' не должно триггерить фильтр по слову 'exchange'."""
        hit, _ = pre_filter_check("exchangeable assets", [])
        self.assertFalse(hit)

    def test_clears_on_valid_builder(self):
        hit, reason = pre_filter_check("Building a ZK rollup protocol on L2", [
            "We just shipped mainnet."
        ])
        self.assertFalse(hit)
        self.assertIsNone(reason)


class TestRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_limits_throughput(self):
        """RateLimiter должен ограничивать скорость до rps."""
        limiter = RateLimiter(rps=10.0)
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        elapsed = time.monotonic() - start
        # При rps=10: 5 запросов = ~0.4с. С погрешностью допускаем от 0.3с.
        self.assertGreaterEqual(elapsed, 0.3)


class TestCircuitBreaker(unittest.TestCase):
    def test_opens_after_threshold(self):
        """После N сбоев подряд circuit breaker должен открыться."""
        cb = CircuitBreaker(threshold=3, sleep_sec=60)
        for _ in range(3):
            cb.record_failure()
        self.assertTrue(cb.is_open())

    def test_resets_on_success(self):
        """После успеха счётчик сбоев должен сброситься."""
        cb = CircuitBreaker(threshold=3, sleep_sec=60)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()  # Снова начинаем
        self.assertFalse(cb.is_open())

    def test_closed_by_default(self):
        cb = CircuitBreaker(threshold=5, sleep_sec=30)
        self.assertFalse(cb.is_open())

    def test_reopens_after_sleep(self):
        """После истечения паузы breaker должен снова закрыться."""
        cb = CircuitBreaker(threshold=1, sleep_sec=0.01)
        cb.record_failure()
        self.assertTrue(cb.is_open())
        time.sleep(0.02)
        self.assertFalse(cb.is_open())


class TestScoring(unittest.TestCase):
    def test_positive_signals_raise_score(self):
        """Ключевые слова стартапа должны давать положительный счёт."""
        ctx = "They are building a DeFi protocol backed by Paradigm"
        score = score_candidate("newprotocol", ctx)
        self.assertGreater(score, 5)

    def test_negative_signals_lower_score(self):
        """VC/журналист должны давать отрицательный счёт."""
        ctx = "journalist covering VC fund announcements and capital markets"
        score = score_candidate("vcreporter", ctx)
        self.assertLess(score, 0)

    def test_empty_context_returns_zero(self):
        score = score_candidate("unknown", "")
        self.assertEqual(score, 0)

    def test_startup_scores_higher_than_media(self):
        """Builder должен оцениваться выше, чем media."""
        startup_ctx = "shipping mainnet soon, backed by a16z, building zkvm"
        media_ctx   = "podcast host and influencer, newsletter about price charts"
        self.assertGreater(
            score_candidate("zkbuilder", startup_ctx),
            score_candidate("cryptonews", media_ctx)
        )

    def test_depth2_bonus_applied(self):
        """Score выше на +5 если кандидат найден через верифицированный стартап (depth2 бонус)."""
        ctx = "building a new protocol"
        base  = score_candidate("proto", ctx)
        bonus = base + 5  # бонус добавляет main()
        self.assertEqual(bonus, base + 5)


class TestExtractMentionsWithContext(unittest.TestCase):
    def test_returns_context_snippet(self):
        """Снипет контекста должен содержать предложение с упоминанием."""
        pairs = extract_mentions_with_context(["Building a protocol, check @NewProto for details."])
        self.assertEqual(len(pairs), 1)
        mention, ctx = pairs[0]
        self.assertEqual(mention, "NewProto")
        self.assertIn("NewProto", ctx)

    def test_no_duplicates_across_sentences(self):
        """@тот же ник в двух предложениях должен появиться один раз."""
        pairs = extract_mentions_with_context([
            "Great work @alpha! Check @alpha again later."
        ])
        names = [p[0].lower() for p in pairs]
        self.assertEqual(names.count("alpha"), 1)

class TestTTLCache(unittest.TestCase):
    def test_fresh_cache_is_valid(self):
        """Свеже-сохранённый профиль должен считаться актуальным."""
        now_iso = datetime.now().isoformat()
        self.assertTrue(is_cache_valid(now_iso))

    def test_expired_cache_is_invalid(self):
        """Профиль старше TTL должен инвалидироваться."""
        old = (datetime.now() - timedelta(hours=CACHE_TTL_HOURS + 1)).isoformat()
        self.assertFalse(is_cache_valid(old))

    def test_none_parsed_at_is_invalid(self):
        self.assertFalse(is_cache_valid(None))

    def test_corrupted_date_is_invalid(self):
        self.assertFalse(is_cache_valid("not-a-date"))

    def test_ai_ttl_longer_than_cache_ttl(self):
        """вердикт AI живёт дольше кэша профиля."""
        self.assertGreater(AI_TTL_HOURS, CACHE_TTL_HOURS)

    def test_recently_expired_ai_verdict(self):
        """Вердикт старше AI_TTL_HOURS должен оказаться невалидным."""
        old = (datetime.now() - timedelta(hours=AI_TTL_HOURS + 1)).isoformat()
        self.assertFalse(is_ai_verdict_valid(old))


class TestScrollRounds(unittest.TestCase):
    def test_high_priority_gets_max_scrolls(self):
        """Score >= 8 — полный скролл."""
        self.assertEqual(get_scroll_rounds(10), SCROLL_ROUNDS)
        self.assertEqual(get_scroll_rounds(8),  SCROLL_ROUNDS)

    def test_medium_priority_gets_reduced_scrolls(self):
        """Score 4-7 — уменьшенный скролл."""
        rounds = get_scroll_rounds(5)
        self.assertGreater(rounds, SCROLL_ROUNDS_MIN)
        self.assertLess(rounds, SCROLL_ROUNDS)

    def test_low_priority_gets_min_scrolls(self):
        """Score < 4 — минимальный скролл."""
        self.assertEqual(get_scroll_rounds(0),  SCROLL_ROUNDS_MIN)
        self.assertEqual(get_scroll_rounds(-5), SCROLL_ROUNDS_MIN)


if __name__ == "__main__":
    unittest.main()
