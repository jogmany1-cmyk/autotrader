import os
import tempfile
from autotrader.data import CsvProvider, SyntheticProvider
from autotrader.data.base import DataError


def test_synthetic_reproducible():
    a = SyntheticProvider().history("AAA", 50)
    b = SyntheticProvider().history("AAA", 50)
    assert [x.close for x in a] == [x.close for x in b]


def test_synthetic_universe_returns_symbols():
    p = SyntheticProvider()
    assert len(p.universe()) >= 3


def test_synthetic_unknown_symbol_raises():
    import pytest
    with pytest.raises(DataError):
        SyntheticProvider().history("NOPE", 5)


def test_csv_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "X.csv")
        with open(path, "w") as fh:
            fh.write("date,open,high,low,close,volume\n")
            fh.write("2024-01-02,100,101,99,100.5,1000\n")
            fh.write("2024-01-03,100.5,102,100,101.5,1200\n")
        p = CsvProvider(d)
        bars = p.history("X")
        assert len(bars) == 2
        assert bars[0].close == 100.5
        assert p.universe() == ["X"]
