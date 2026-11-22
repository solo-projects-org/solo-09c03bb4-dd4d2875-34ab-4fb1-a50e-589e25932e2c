"""CSV 解析与校验测试。"""

import pytest

from app.csvio import CsvError, parse_scores, parse_starters, parse_subs


def test_parse_basic_and_keeps_row_order():
    subs = parse_subs(
        "period,remaining,outgoing,incoming\n"
        "1,600,A5,B1\n2,100,A4,B2\n".encode()
    )
    assert [ev.row_no for ev in subs] == [1, 2]
    assert subs[0].outgoing == "A5" and subs[0].incoming == "B1"

    scores = parse_scores(
        "period,remaining,team,points,scorer\n"
        "1,600,本方,3,A1\n1,500,对方,2,\n".encode()
    )
    assert scores[0].team == "own" and scores[0].scorer == "A1"
    assert scores[1].team == "opp" and scores[1].scorer is None


def test_wrong_header_rejected():
    with pytest.raises(CsvError):
        parse_subs(b"period,remaining,out,who\n1,1,A,B\n")
    with pytest.raises(CsvError):
        parse_scores(b"period,remaining,team,points,scorer\n1,1,own,2,\n"
                     .replace(b"period,remaining,team,points,scorer",
                              b"period,left,team,points,scorer"))


def test_non_integer_cell_rejected():
    with pytest.raises(CsvError):
        parse_subs(b"period,remaining,outgoing,incoming\n1,x,A5,B1\n")


def test_backwards_timing_rejected_in_both_tables():
    with pytest.raises(CsvError, match="比赛时刻"):
        parse_subs(
            b"period,remaining,outgoing,incoming\n1,300,A4,B2\n1,600,A5,B1\n"
        )
    with pytest.raises(CsvError, match="比赛时刻"):
        parse_scores(
            b"period,remaining,team,points,scorer\n2,100,opp,2,\n1,600,opp,2,\n"
        )


def test_same_second_rows_are_allowed_and_ordered():
    subs = parse_subs(
        b"period,remaining,outgoing,incoming\n"
        b"1,600,A5,B1\n1,600,A4,B2\n"
    )
    assert [ev.row_no for ev in subs] == [1, 2]
    assert subs[0].remaining == subs[1].remaining == 600


def test_team_and_scorer_rules():
    with pytest.raises(CsvError):
        parse_scores(b"period,remaining,team,points,scorer\n1,1,home,2,A1\n")
    with pytest.raises(CsvError, match="得分者"):
        parse_scores(b"period,remaining,team,points,scorer\n1,1,own,2,\n")
    with pytest.raises(CsvError, match="对方得分不应"):
        parse_scores(b"period,remaining,team,points,scorer\n1,1,opp,2,X1\n")
    with pytest.raises(CsvError, match="分值"):
        parse_scores(b"period,remaining,team,points,scorer\n1,1,own,4,A1\n")


def test_substitution_same_player_rejected():
    with pytest.raises(CsvError):
        parse_subs(b"period,remaining,outgoing,incoming\n1,1,A5,A5\n")


def test_starters_parsing():
    assert parse_starters("A1, A2，A3,A4,A5") == ["A1", "A2", "A3", "A4", "A5"]
    with pytest.raises(CsvError):
        parse_starters("A1,A2,A3,A4")
    with pytest.raises(CsvError):
        parse_starters("A1,A2,A3,A4,A4")
