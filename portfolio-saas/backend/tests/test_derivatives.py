from marketdata.derivatives import select_roll_series


def test_roll_series_excludes_near_expiry_and_marks_contract_changes():
    series = select_roll_series([
        {"day": "1405-01-01", "contract_code": "OLD", "days_to_expiry": 4, "volume": 100},
        {"day": "1405-01-01", "contract_code": "NEXT", "days_to_expiry": 30, "volume": 10},
        {"day": "1405-01-02", "contract_code": "NEXT", "days_to_expiry": 29, "volume": 12},
        {"day": "1405-01-03", "contract_code": "NEW", "days_to_expiry": 35, "volume": 20},
    ])

    assert [row["contract_code"] for row in series] == ["NEXT", "NEXT", "NEW"]
    assert [row["rolled"] for row in series] == [False, False, True]
