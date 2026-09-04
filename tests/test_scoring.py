from datetime import date

from ontheline.scoring import PrioritySignals, priority_score

TODAY = date(2026, 8, 11)


def test_live_failure_report_outranks_plain_staleness():
    stale = priority_score(PrioritySignals(date(2025, 8, 11)), today=TODAY)
    failed = priority_score(PrioritySignals(date(2026, 8, 10), failure_reports_30d=1), today=TODAY)
    assert failed > stale


def test_recent_contact_penalty_prevents_harassing_an_org():
    recent = priority_score(PrioritySignals(date(2026, 5, 1), days_since_contact=1), today=TODAY)
    rested = priority_score(PrioritySignals(date(2026, 5, 1), days_since_contact=30), today=TODAY)
    assert recent < rested


def test_schedule_is_more_volatile_than_address():
    schedule = priority_score(PrioritySignals(date(2026, 5, 1), fields=("schedule",)), today=TODAY)
    address = priority_score(PrioritySignals(date(2026, 5, 1), fields=("address",)), today=TODAY)
    assert schedule > address
