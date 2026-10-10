import json
import logging

import pytest

from app.services import oasis_profile_generator as generator_module
from app.services import simulation_manager as simulation_manager_module
from app.services.oasis_profile_generator import (
    OasisProfileGenerator,
    _persona_entity_reject_reason,
    partition_persona_entities,
)
from app.services.simulation_manager import SimulationManager, SimulationState
from app.services.zep_entity_reader import EntityNode, FilteredEntities
from app.utils.locale import t


# 来自 issue #759 的真实抽取结果：这些"实体"全部被提升成了模拟Agent
REPORTED_JUNK_NAMES = {
    "None": "placeholder",
    "edges": "placeholder",
    "market event": "descriptor_phrase",
    "Posts 3-5 times daily: cross-asset takes, historical analogies "
    "('I've seen this movie before'), decay-of-panic arguments.": "prose_fragment",
    "supply chain detail, management call parsing, unit economics": "clause_list",
    "MiroFish": "self_reference",
}

PERSON_NAMES = [
    "Elon Musk",
    "Dr. Jane Goodall",
    "Jean-Luc Picard",
    "O'Brien",
    "Smith, John, Jr.",
    "张伟",
    "山田太郎",
]

# 群体/机构人设是有意支持的能力（_build_group_persona_prompt），不能被过滤掉
GROUP_NAMES = [
    "Goldman Sachs",
    "Tsinghua University",
    "Reuters",
    "The Federal Reserve Board of Governors",
    "National Oceanic and Atmospheric Administration",
    "Acme Co.",
    "Berkshire Hathaway Inc.",
    "OpenAI",
    "国家发展和改革委员会",
]


class _LogRecorder(logging.Handler):
    """Collect formatted log messages emitted by the generator logger."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _entity(name, label="Person"):
    return EntityNode(
        uuid=f"uuid::{name}",
        name=name,
        labels=["Entity", label],
        summary=f"Summary of {name}",
        attributes={},
    )


def _stub_generator(monkeypatch):
    """Build a generator that never touches the network (no LLM, no Zep)."""
    generator = object.__new__(OasisProfileGenerator)
    generator.client = None
    generator.model_name = "stub-model"
    generator.zep_client = None
    generator.graph_id = None

    def _fail_on_llm_call(*args, **kwargs):
        raise AssertionError("LLM must not be called from tests")

    monkeypatch.setattr(generator_module, "create_chat_completion", _fail_on_llm_call)
    return generator


@pytest.mark.parametrize("name, expected_reason", sorted(REPORTED_JUNK_NAMES.items()))
def test_reported_junk_names_are_rejected(name, expected_reason):
    assert _persona_entity_reject_reason(name) == expected_reason


@pytest.mark.parametrize("name", ["", "   ", "\n", None, 42])
def test_empty_and_non_string_names_are_rejected(name):
    assert _persona_entity_reject_reason(name) == "empty_name"


@pytest.mark.parametrize("name", PERSON_NAMES)
def test_person_names_are_accepted(name):
    assert _persona_entity_reject_reason(name) is None


@pytest.mark.parametrize("name", GROUP_NAMES)
def test_organization_and_group_names_are_accepted(name):
    assert _persona_entity_reject_reason(name) is None


def test_invalid_entities_are_dropped_without_leaving_holes(monkeypatch, tmp_path):
    generator = _stub_generator(monkeypatch)
    output_path = tmp_path / "reddit_profiles.json"
    entities = [
        _entity("Elon Musk"),
        _entity("None"),
        _entity("Goldman Sachs", label="Company"),
        _entity("market event"),
        _entity("MiroFish"),
        _entity("Tsinghua University", label="University"),
    ]

    profiles = generator.generate_profiles_from_entities(
        entities=entities,
        use_llm=False,
        parallel_count=1,
        realtime_output_path=str(output_path),
        output_platform="reddit",
    )

    assert [p.name for p in profiles] == [
        "Elon Musk",
        "Goldman Sachs",
        "Tsinghua University",
    ]
    assert [p.user_id for p in profiles] == [0, 1, 2]
    assert all(profile is not None for profile in profiles)

    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert [row["user_id"] for row in saved] == [0, 1, 2]
    assert [row["name"] for row in saved] == [p.name for p in profiles]


def test_skipped_entities_are_logged_and_surfaced_in_progress(monkeypatch):
    generator = _stub_generator(monkeypatch)
    recorder = _LogRecorder()
    generator_module.logger.addHandler(recorder)
    events = []

    try:
        profiles = generator.generate_profiles_from_entities(
            entities=[_entity("Elon Musk"), _entity("edges"), _entity("market event")],
            use_llm=False,
            progress_callback=lambda current, total, message: events.append(
                (current, total, message)
            ),
            parallel_count=1,
        )
    finally:
        generator_module.logger.removeHandler(recorder)

    assert [p.name for p in profiles] == ["Elon Musk"]
    assert t('progress.personaEntitySkipped', name='edges', reason='placeholder') in recorder.messages
    assert t(
        'progress.personaEntitySkipped', name='market event', reason='descriptor_phrase'
    ) in recorder.messages
    assert events[0] == (0, 1, t('progress.personaEntitiesSkipped', count=2, total=1))


def test_partition_is_idempotent(monkeypatch):
    entities = [_entity("Elon Musk"), _entity("None"), _entity("market event")]

    first_pass, first_skipped = partition_persona_entities(entities)
    second_pass, second_skipped = partition_persona_entities(first_pass)

    assert [e.name for e in first_pass] == ["Elon Musk"]
    assert len(first_skipped) == 2
    # 纯函数：对已过滤的列表再过滤是空操作，所以兜底过滤不会二次改变编号
    assert second_pass == first_pass
    assert second_skipped == []


def test_prepare_simulation_uses_one_entity_list_for_profiles_and_configs(tmp_path, monkeypatch):
    entities = [
        _entity("Elon Musk"),
        _entity("None"),
        _entity("Goldman Sachs", label="Company"),
        _entity("market event"),
        _entity("Tsinghua University", label="University"),
    ]
    seen = {}

    class StubReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=list(entities),
                entity_types={"Person", "Company", "University"},
                total_count=len(entities),
                filtered_count=len(entities),
            )

    class StubProfileGenerator:
        def __init__(self, *args, **kwargs):
            pass

        def generate_profiles_from_entities(self, entities, **kwargs):
            seen["profiles"] = list(entities)
            return [
                generator_module.OasisAgentProfile(
                    user_id=idx,
                    user_name=f"user_{idx}",
                    name=entity.name,
                    bio="",
                    persona="",
                )
                for idx, entity in enumerate(entities)
            ]

        def save_profiles(self, **kwargs):
            seen["saved_platform"] = kwargs.get("platform")

    class StubParameters:
        generation_reasoning = "stub reasoning"

        def to_json(self):
            return "{}"

    class StubConfigGenerator:
        def __init__(self, *args, **kwargs):
            pass

        def generate_config(self, entities, **kwargs):
            seen["configs"] = list(entities)
            return StubParameters()

    monkeypatch.setattr(simulation_manager_module, "ZepEntityReader", StubReader)
    monkeypatch.setattr(simulation_manager_module, "OasisProfileGenerator", StubProfileGenerator)
    monkeypatch.setattr(
        simulation_manager_module, "SimulationConfigGenerator", StubConfigGenerator
    )

    manager = SimulationManager()
    manager.SIMULATION_DATA_DIR = str(tmp_path)
    manager._save_simulation_state(
        SimulationState(
            simulation_id="sim_filter",
            project_id="proj_filter",
            graph_id="graph_filter",
            enable_twitter=False,
            enable_reddit=True,
        )
    )

    state = manager.prepare_simulation(
        simulation_id="sim_filter",
        simulation_requirement="requirement",
        document_text="document",
    )

    # 人设和Agent活动配置必须收到完全相同的实体列表，
    # 否则 profile 的 user_id 会和 agent_config 的 agent_id 指向不同实体
    assert [e.name for e in seen["profiles"]] == [
        "Elon Musk",
        "Goldman Sachs",
        "Tsinghua University",
    ]
    assert seen["profiles"] == seen["configs"]
    assert all(left is right for left, right in zip(seen["profiles"], seen["configs"]))
    assert state.entities_count == len(seen["profiles"]) == state.profiles_count


def test_all_invalid_entities_yield_no_profiles_and_no_progress_callback(monkeypatch):
    generator = _stub_generator(monkeypatch)
    events = []

    profiles = generator.generate_profiles_from_entities(
        entities=[_entity("None"), _entity("  "), _entity("edges")],
        use_llm=False,
        progress_callback=lambda current, total, message: events.append(
            (current, total, message)
        ),
        parallel_count=1,
    )

    assert profiles == []
    # total为0时不能回调，否则调用方 int(current / total * 100) 会除零
    assert events == []
