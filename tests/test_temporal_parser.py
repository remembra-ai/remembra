"""Tests for Smart Auto-Forgetting Temporal Parser (v0.12)."""

import pytest

from remembra.client.temporal_parser import (
    TemporalDetection,
    TemporalGranularity,
    TemporalParser,
    detect_temporal,
    suggest_ttl,
)


class TestTemporalParser:
    """Tests for TemporalParser class."""

    def test_init_defaults(self):
        """Test parser initialization with defaults."""
        parser = TemporalParser()
        assert parser._use_dateparser is False
        assert parser._min_confidence == 0.5

    def test_init_custom(self):
        """Test parser initialization with custom values."""
        parser = TemporalParser(
            use_dateparser=False,
            min_confidence=0.7,
            default_buffer_hours=24,
        )
        assert parser._min_confidence == 0.7
        assert parser._default_buffer_hours == 24

    def test_detect_none_input(self):
        """Test detection with None input."""
        parser = TemporalParser()
        assert parser.detect(None) is None

    def test_detect_empty_input(self):
        """Test detection with empty input."""
        parser = TemporalParser()
        assert parser.detect("") is None

    def test_detect_no_temporal(self):
        """Test detection with no temporal phrases."""
        parser = TemporalParser()
        result = parser.detect("John works at Acme Corp as an engineer")
        assert result is None


class TestExplicitRememberPatterns:
    """Tests for 'remember for X' patterns."""

    @pytest.mark.parametrize("content,expected_granularity", [
        ("Remember this for 30 minutes", TemporalGranularity.MINUTES),
        ("remember for 2 hours", TemporalGranularity.HOURS),
        ("Remember this for 3 days", TemporalGranularity.DAYS),
        ("remember for a week", TemporalGranularity.WEEKS),
        ("remember this for 2 weeks", TemporalGranularity.WEEKS),
        ("remember for next week", TemporalGranularity.WEEKS),
        ("remember for a month", TemporalGranularity.MONTHS),
        ("remember this for 3 months", TemporalGranularity.MONTHS),
    ])
    def test_remember_patterns(self, content, expected_granularity):
        """Test explicit 'remember for X' patterns."""
        parser = TemporalParser()
        result = parser.detect(content)
        
        assert result is not None
        assert result.granularity == expected_granularity
        assert result.confidence >= 0.9  # High confidence for explicit patterns
        assert result.ttl_seconds > 0

    def test_remember_30_minutes(self):
        """Test 30 minutes remember pattern."""
        parser = TemporalParser()
        result = parser.detect("Remember this for 30 minutes")
        
        assert result is not None
        # 30 min + 30 min buffer = 60 min = 3600 seconds
        assert 1800 <= result.ttl_seconds <= 3700


class TestTomorrowPatterns:
    """Tests for 'tomorrow' related patterns."""

    @pytest.mark.parametrize("content", [
        "Meeting tomorrow at 3pm",
        "Call tomorrow",
        "tomorrow at 2pm",
        "Let's meet tomorrow",
    ])
    def test_tomorrow_detection(self, content):
        """Test tomorrow pattern detection."""
        parser = TemporalParser()
        result = parser.detect(content)
        
        assert result is not None
        assert result.granularity == TemporalGranularity.DAYS
        # Should be ~36 hours
        assert 100000 <= result.ttl_seconds <= 150000

    def test_meeting_tomorrow_ttl(self):
        """Test meeting tomorrow sets ~36 hour TTL."""
        parser = TemporalParser()
        result = parser.detect("Meeting tomorrow at 2pm with John")
        
        assert result is not None
        # 36 hours = 129600 seconds
        assert abs(result.ttl_seconds - 129600) < 10000


class TestNextWeekPatterns:
    """Tests for 'next week' related patterns."""

    @pytest.mark.parametrize("content", [
        "See you next week",
        "next week's meeting",
        "Call me next Monday",
        "Deadline next Friday",
    ])
    def test_next_week_detection(self, content):
        """Test next week pattern detection."""
        parser = TemporalParser()
        result = parser.detect(content)
        
        assert result is not None
        # Should be around 7-10 days
        assert result.ttl_seconds >= 6 * 86400  # At least 6 days
        assert result.ttl_seconds <= 12 * 86400  # At most 12 days


class TestRelativeTimePatterns:
    """Tests for 'in X minutes/hours/days' patterns."""

    def test_in_30_minutes(self):
        """Test 'in 30 minutes' pattern."""
        parser = TemporalParser()
        result = parser.detect("Call me in 30 minutes")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.MINUTES
        # 30 min + buffer
        assert result.ttl_seconds >= 30 * 60

    def test_in_2_hours(self):
        """Test 'in 2 hours' pattern."""
        parser = TemporalParser()
        result = parser.detect("Meeting in 2 hours")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.HOURS
        # 2 hours + 1 hour buffer
        assert result.ttl_seconds >= 2 * 3600

    def test_in_3_days(self):
        """Test 'in 3 days' pattern."""
        parser = TemporalParser()
        result = parser.detect("Deadline in 3 days")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.DAYS

    def test_in_2_weeks(self):
        """Test 'in 2 weeks' pattern."""
        parser = TemporalParser()
        result = parser.detect("Vacation in 2 weeks")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.WEEKS


class TestUntilPatterns:
    """Tests for 'until X' patterns."""

    def test_until_tomorrow(self):
        """Test 'until tomorrow' pattern."""
        parser = TemporalParser()
        result = parser.detect("Valid until tomorrow")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.DAYS

    def test_until_next_monday(self):
        """Test 'until next Monday' pattern."""
        parser = TemporalParser()
        result = parser.detect("Remember until next Monday")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.WEEKS


class TestRecurringPatterns:
    """Tests for recurring/periodic patterns."""

    def test_weekly(self):
        """Test weekly pattern."""
        parser = TemporalParser()
        result = parser.detect("Weekly team meeting")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.WEEKS
        # 2 weeks for recurring
        assert result.ttl_seconds >= 7 * 86400

    def test_monthly(self):
        """Test monthly pattern."""
        parser = TemporalParser()
        result = parser.detect("Monthly report due")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.MONTHS

    def test_annual(self):
        """Test annual pattern."""
        parser = TemporalParser()
        result = parser.detect("Annual performance review")
        
        assert result is not None
        assert result.granularity == TemporalGranularity.YEARS
        # About 13 months
        assert result.ttl_seconds >= 365 * 86400


class TestDeadlinePatterns:
    """Tests for deadline-related patterns."""

    def test_deadline_tomorrow(self):
        """Test 'deadline tomorrow' pattern."""
        parser = TemporalParser()
        result = parser.detect("Deadline tomorrow")
        
        assert result is not None
        assert result.confidence >= 0.7

    def test_due_friday(self):
        """Test 'due Friday' pattern."""
        parser = TemporalParser()
        result = parser.detect("Report due next Friday")
        
        assert result is not None
        assert result.ttl_seconds >= 5 * 86400

    def test_deadline_in_days(self):
        """Test 'deadline in N days' pattern."""
        parser = TemporalParser()
        result = parser.detect("Deadline in 5 days")
        
        assert result is not None
        # 5 days + buffer
        assert result.ttl_seconds >= 5 * 86400


class TestDetectAll:
    """Tests for detect_all method."""

    def test_detect_all_multiple(self):
        """Test detecting multiple temporal phrases."""
        parser = TemporalParser()
        results = parser.detect_all(
            "Meeting tomorrow at 3pm, then another meeting next week"
        )
        
        assert len(results) >= 2
        # Sorted by confidence
        assert results[0].confidence >= results[-1].confidence

    def test_detect_all_none(self):
        """Test detect_all with no matches."""
        parser = TemporalParser()
        results = parser.detect_all("No temporal content here")
        
        assert results == []


class TestTTLFormatting:
    """Tests for TTL string formatting."""

    def test_format_minutes(self):
        """Test minutes formatting."""
        parser = TemporalParser()
        result = parser.detect("Remember for 10 minutes")
        
        assert result is not None
        assert "m" in result.ttl_string

    def test_format_hours(self):
        """Test hours formatting."""
        parser = TemporalParser()
        result = parser.detect("Remember for 5 hours")
        
        assert result is not None
        assert "h" in result.ttl_string

    def test_format_days(self):
        """Test days formatting."""
        parser = TemporalParser()
        result = parser.detect("Remember for 3 days")
        
        assert result is not None
        assert "d" in result.ttl_string


class TestMinConfidence:
    """Tests for minimum confidence threshold."""

    def test_below_threshold_excluded(self):
        """Test that low-confidence matches are excluded."""
        parser = TemporalParser(min_confidence=0.9)
        
        # "this afternoon" has lower confidence (~0.70)
        result = parser.detect("This afternoon")
        
        # Should be excluded due to high threshold
        assert result is None

    def test_above_threshold_included(self):
        """Test that high-confidence matches are included."""
        parser = TemporalParser(min_confidence=0.5)
        
        # "remember for X" has high confidence (~0.95)
        result = parser.detect("Remember for 2 days")
        
        assert result is not None
        assert result.confidence >= 0.9


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_detect_temporal(self):
        """Test detect_temporal function."""
        result = detect_temporal("Meeting tomorrow")
        
        assert result is not None
        assert isinstance(result, TemporalDetection)

    def test_detect_temporal_no_match(self):
        """Test detect_temporal with no match."""
        result = detect_temporal("Normal text without temporal")
        
        assert result is None

    def test_suggest_ttl(self):
        """Test suggest_ttl function."""
        ttl = suggest_ttl("Meeting tomorrow at 3pm")
        
        assert ttl is not None
        assert isinstance(ttl, str)
        assert "h" in ttl or "d" in ttl

    def test_suggest_ttl_no_match(self):
        """Test suggest_ttl with no match."""
        ttl = suggest_ttl("Normal content")
        
        assert ttl is None


class TestEdgeCases:
    """Tests for edge cases."""

    def test_case_insensitive(self):
        """Test patterns are case insensitive."""
        parser = TemporalParser()
        
        assert parser.detect("TOMORROW") is not None
        assert parser.detect("Tomorrow") is not None
        assert parser.detect("tomorrow") is not None

    def test_embedded_in_sentence(self):
        """Test patterns work when embedded in longer text."""
        parser = TemporalParser()
        
        result = parser.detect(
            "Please note that the important deadline is tomorrow "
            "and we need to finish the project by then."
        )
        
        assert result is not None

    def test_multiple_matches_highest_confidence(self):
        """Test that detect returns highest confidence match."""
        parser = TemporalParser()
        
        # "remember for 3 days" (0.95) vs "tomorrow" (0.75)
        result = parser.detect("Remember for 3 days: meeting tomorrow")
        
        assert result is not None
        assert result.confidence >= 0.9  # Should pick higher confidence
