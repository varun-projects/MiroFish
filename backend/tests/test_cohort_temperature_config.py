"""COHORT_TEMPERATURE 覆盖人群构建链路上的三个采样点：

1. 本体生成 (OntologyGenerator.generate)
2. 人设生成 (OasisProfileGenerator._generate_profile_with_llm)
3. 模拟配置生成 (SimulationConfigGenerator._call_llm_with_retry，经 prepare_simulation 触发)
"""

import importlib.util
import os
import time

import pytest

from app.config import Config
from app.services import oasis_profile_generator as profile_module
from app.services import simulation_config_generator as sim_config_module
from app.services.oasis_profile_generator import OasisProfileGenerator
from app.services.ontology_generator import OntologyGenerator
from app.services.simulation_config_generator import SimulationConfigGenerator

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "app", "config.py")

# 历史默认值：本体 0.3，人设/模拟配置 0.7 并在每次重试时降低 0.1。
# 第三级写成 0.49999999999999994 而不是 0.5，因为这是 `0.7 - (2 * 0.1)` 的精确结果，
# 未配置时必须一字不差地保持原样。
LEGACY_ONTOLOGY_TEMPERATURE = 0.3
LEGACY_RETRY_LADDER = [0.7, 0.6, 0.49999999999999994]


class OntologyClientStub:
    """记录 chat_json 收到的 temperature"""

    def __init__(self):
        self.temperatures = []

    def chat_json(self, **kwargs):
        self.temperatures.append(kwargs["temperature"])
        return {"entity_types": [], "edge_types": [], "analysis_summary": "ok"}


class ChatCompletionStub:
    """记录每次请求的 temperature，并始终失败以走完整条重试阶梯"""

    def __init__(self):
        self.temperatures = []

    def __call__(self, client, *, model, messages, temperature=None, **kwargs):
        self.temperatures.append(temperature)
        raise RuntimeError("stubbed provider failure")


def _pin_cohort_temperature_unset(monkeypatch):
    """把配置固定为未设置状态，使默认值测试不受运行环境影响"""
    monkeypatch.delenv("COHORT_TEMPERATURE", raising=False)
    monkeypatch.setattr(Config, "COHORT_TEMPERATURE", None, raising=False)


def _ontology_temperature() -> float:
    client = OntologyClientStub()
    OntologyGenerator(llm_client=client).generate(
        document_texts=["一份很短的素材。"],
        simulation_requirement="模拟公众讨论。",
    )
    return client.temperatures[0]


def _persona_retry_ladder(monkeypatch) -> list:
    stub = ChatCompletionStub()
    monkeypatch.setattr(profile_module, "create_chat_completion", stub)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    generator = object.__new__(OasisProfileGenerator)
    generator.client = object()
    generator.model_name = "compatible-model"
    generator._generate_profile_with_llm(
        entity_name="张三",
        entity_type="PublicFigure",
        entity_summary="事件当事人",
        entity_attributes={},
        context="",
    )
    return stub.temperatures


def _simulation_config_retry_ladder(monkeypatch) -> list:
    stub = ChatCompletionStub()
    monkeypatch.setattr(sim_config_module, "create_chat_completion", stub)
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    generator = object.__new__(SimulationConfigGenerator)
    generator.client = object()
    generator.model_name = "compatible-model"
    with pytest.raises(RuntimeError):
        generator._call_llm_with_retry("prompt", "system prompt")
    return stub.temperatures


def _load_config_with_env(monkeypatch, raw_value):
    """用给定的环境变量值重新加载一份独立的 config 模块（不污染 sys.modules）"""
    if raw_value is None:
        monkeypatch.delenv("COHORT_TEMPERATURE", raising=False)
    else:
        monkeypatch.setenv("COHORT_TEMPERATURE", raw_value)
    # 本地 .env 不应影响测试结果
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: None)

    spec = importlib.util.spec_from_file_location("cohort_config_under_test", CONFIG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config


def test_unset_keeps_ontology_temperature(monkeypatch):
    _pin_cohort_temperature_unset(monkeypatch)

    assert _ontology_temperature() == LEGACY_ONTOLOGY_TEMPERATURE


def test_unset_keeps_persona_retry_ladder(monkeypatch):
    _pin_cohort_temperature_unset(monkeypatch)

    assert _persona_retry_ladder(monkeypatch) == LEGACY_RETRY_LADDER


def test_unset_keeps_simulation_config_retry_ladder(monkeypatch):
    _pin_cohort_temperature_unset(monkeypatch)

    assert _simulation_config_retry_ladder(monkeypatch) == LEGACY_RETRY_LADDER


def test_zero_pins_every_cohort_stage(monkeypatch):
    monkeypatch.setattr(Config, "COHORT_TEMPERATURE", 0.0)

    assert _ontology_temperature() == 0.0
    assert _persona_retry_ladder(monkeypatch) == [0.0, 0.0, 0.0]
    assert _simulation_config_retry_ladder(monkeypatch) == [0.0, 0.0, 0.0]


def test_configured_base_steps_down_and_stops_at_zero(monkeypatch):
    monkeypatch.setattr(Config, "COHORT_TEMPERATURE", 0.2)

    assert _ontology_temperature() == 0.2
    assert _persona_retry_ladder(monkeypatch) == [0.2, 0.1, 0.0]
    assert _simulation_config_retry_ladder(monkeypatch) == [0.2, 0.1, 0.0]


@pytest.mark.parametrize("base", [0.0, 0.05, 0.1, 0.2, -1.0])
def test_no_retry_rung_is_ever_negative(monkeypatch, base):
    monkeypatch.setattr(Config, "COHORT_TEMPERATURE", base)

    for ladder in (
        _persona_retry_ladder(monkeypatch),
        _simulation_config_retry_ladder(monkeypatch),
    ):
        assert len(ladder) == 3
        assert all(value >= 0.0 for value in ladder), ladder


def test_env_value_reaches_the_cohort_stages(monkeypatch):
    config = _load_config_with_env(monkeypatch, "0")

    assert config.COHORT_TEMPERATURE == 0.0
    assert [config.cohort_temperature(0.7, attempt) for attempt in range(3)] == [0.0, 0.0, 0.0]
    assert config.cohort_temperature(0.3) == 0.0


@pytest.mark.parametrize("raw_value", [None, "", "   "])
def test_missing_or_blank_env_value_keeps_legacy_defaults(monkeypatch, raw_value):
    config = _load_config_with_env(monkeypatch, raw_value)

    assert config.COHORT_TEMPERATURE is None
    assert config.cohort_temperature(0.3) == LEGACY_ONTOLOGY_TEMPERATURE
    assert [
        config.cohort_temperature(0.7, attempt) for attempt in range(3)
    ] == LEGACY_RETRY_LADDER


@pytest.mark.parametrize("raw_value", ["abc", "0.3abc", "0,3", "low"])
def test_unparsable_env_value_warns_and_keeps_legacy_defaults(monkeypatch, raw_value):
    with pytest.warns(RuntimeWarning, match="COHORT_TEMPERATURE"):
        config = _load_config_with_env(monkeypatch, raw_value)

    assert config.COHORT_TEMPERATURE is None
    assert config.cohort_temperature(0.3) == LEGACY_ONTOLOGY_TEMPERATURE
    assert [
        config.cohort_temperature(0.7, attempt) for attempt in range(3)
    ] == LEGACY_RETRY_LADDER
