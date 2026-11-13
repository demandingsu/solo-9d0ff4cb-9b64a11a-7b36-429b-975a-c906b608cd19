import pytest

from app.worker.csvparse import CSVValidationError, file_sha256, parse_csv


def test_chinese_header_and_types():
    data = "孔号,层顶深度,层底深度,岩性\nZK-1,0,5.5, 黏土 \n".encode()
    rows = parse_csv(data)
    assert rows == [{
        "row_no": 1, "hole_no": "ZK-1", "top_depth": 0.0,
        "bottom_depth": 5.5, "lithology": "黏土",
    }]


def test_english_header():
    data = b"hole_no,top_depth,bottom_depth,lithology\nA,1,2,sand\n"
    rows = parse_csv(data)
    assert rows[0]["hole_no"] == "A" and rows[0]["lithology"] == "sand"


def test_missing_column():
    with pytest.raises(CSVValidationError):
        parse_csv("孔号,层顶深度,岩性\nA,1,黏土\n".encode())


def test_bad_depth():
    with pytest.raises(CSVValidationError):
        parse_csv("孔号,层顶深度,层底深度,岩性\nA,x,2,黏土\n".encode())


def test_bottom_must_exceed_top():
    with pytest.raises(CSVValidationError):
        parse_csv("孔号,层顶深度,层底深度,岩性\nA,5,5,黏土\n".encode())


def test_empty_and_blank_lines():
    with pytest.raises(CSVValidationError):
        parse_csv("孔号,层顶深度,层底深度,岩性\n\n".encode())


def test_deterministic_hash():
    assert file_sha256(b"abc") == file_sha256(b"abc")
    assert file_sha256(b"abc") != file_sha256(b"abd")
