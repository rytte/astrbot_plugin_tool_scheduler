import pytest

from astrbot_plugin_tool_scheduler.policy import (
    DAY,
    DEFAULTS,
    decide,
    validate_rule,
    validate_settings,
)


def test_cold_start_and_observation_prevent_premature_dormancy():
    settings = {**DEFAULTS, "smart": True}
    assert decide(settings, {}, {}, {}, {}, 100 * DAY).mode == "skills_like"
    assert (
        decide(
            settings, {}, {}, {"opportunities": 49}, {"first_seen": 0}, 100 * DAY
        ).mode
        == "skills_like"
    )
    assert (
        decide(
            settings, {}, {}, {"opportunities": 50}, {"first_seen": 0}, 100 * DAY
        ).mode
        == "dormant"
    )


def test_hysteresis_and_fixed_policy_precedence():
    settings = {**DEFAULTS, "smart": True}
    assert decide(settings, {}, {}, {"requests": 10}, {}, 10 * DAY).mode == "full"
    decision = decide(settings, {}, {}, {"requests": 4}, {"mode": "full"}, 10 * DAY)
    assert decision.mode == "full" and decision.low_since == 10 * DAY
    assert (
        decide(
            settings,
            {},
            {},
            {"requests": 4},
            {"mode": "full", "low_since": 10 * DAY},
            12 * DAY,
        ).mode
        == "skills_like"
    )
    assert (
        decide(
            settings,
            {"strategy": "skills_like"},
            {"strategy": "full"},
            {"requests": 100},
            {},
            10 * DAY,
        ).mode
        == "skills_like"
    )
    assert (
        decide(
            settings,
            {"strategy": "full", "enabled": True},
            {"enabled": False},
            {},
            {},
            0,
        ).mode
        == "off"
    )
    assert (
        decide(
            {**settings, "block_all": True}, {"strategy": "full"}, {}, {}, {}, 0
        ).mode
        == "off"
    )


def test_paused_auto_keeps_native_mode_and_manual_overrides():
    assert decide(DEFAULTS, {}, {}, {}, {}, 0).mode == "full"
    assert decide(DEFAULTS, {}, {}, {}, {}, 0, "skills_like").mode == "skills_like"
    assert (
        decide(DEFAULTS, {"strategy": "skills_like"}, {}, {}, {}, 0).mode
        == "skills_like"
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"window_days": True},
        {"smart": 1},
        {"unknown": 4},
        {"promote_requests": 4, "demote_requests": 5},
        {"retention_days": 10},
    ],
)
def test_invalid_settings_are_rejected(patch):
    with pytest.raises(ValueError):
        validate_settings(patch, DEFAULTS)


def test_alias_validation_and_deduplication():
    assert validate_rule({"aliases": [" 词云 ", "词云"]})["aliases"] == ["词云"]
    with pytest.raises(ValueError):
        validate_rule({"enabled": "false"})
