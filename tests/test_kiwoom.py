"""KiwoomBroker · KiwoomConfig 단위 테스트.

실 네트워크 호출은 하지 않는다. 자격증명 검증, URL 선택, config env 로딩,
Broker 인터페이스 준수만 검증.
"""
import os
import pytest

from autotrader.broker import KiwoomBroker
from autotrader.broker.base import Broker, BrokerError
from autotrader.config import Config, KiwoomConfig


def test_kiwoom_config_from_env_reads_env_vars(monkeypatch):
    monkeypatch.setenv("KIWOOM_APP_KEY", "K")
    monkeypatch.setenv("KIWOOM_APP_SECRET", "S")
    monkeypatch.setenv("KIWOOM_ACCOUNT_NUMBER", "123")
    monkeypatch.setenv("KIWOOM_MODE", "real")
    cfg = KiwoomConfig.from_env()
    assert cfg.app_key == "K" and cfg.app_secret == "S"
    assert cfg.account_number == "123"
    assert cfg.is_paper is False


def test_kiwoom_config_defaults_to_paper():
    cfg = KiwoomConfig()
    assert cfg.is_paper is True


def test_kiwoom_broker_rejects_missing_credentials():
    for kw in ({}, {"app_key": "x"}, {"app_key": "x", "app_secret": "y"}):
        with pytest.raises(BrokerError):
            KiwoomBroker(KiwoomConfig(**kw))


def test_kiwoom_broker_picks_paper_url_when_paper_true():
    kb = KiwoomBroker(KiwoomConfig(app_key="x", app_secret="y",
                                   account_number="z", is_paper=True))
    assert "mockapi" in kb.base


def test_kiwoom_broker_picks_real_url_when_paper_false():
    kb = KiwoomBroker(KiwoomConfig(app_key="x", app_secret="y",
                                   account_number="z", is_paper=False))
    assert kb.base.startswith("https://api.kiwoom.com")


def test_kiwoom_broker_conforms_to_broker_abstract():
    kb = KiwoomBroker(KiwoomConfig(app_key="x", app_secret="y", account_number="z"))
    assert isinstance(kb, Broker)


def test_config_contains_kiwoom_field():
    cfg = Config.default()
    assert isinstance(cfg.kiwoom, KiwoomConfig)


def test_broker_base_list_stocks_default_empty():
    from autotrader.broker import PaperBroker
    from autotrader.config import Costs
    pb = PaperBroker(1_000_000, Costs())
    assert pb.list_stocks() == []
    assert pb.list_stocks("10") == []
