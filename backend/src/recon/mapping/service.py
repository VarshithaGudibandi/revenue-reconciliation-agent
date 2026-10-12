"""Choosing who proposes column mappings, from configuration."""

import os
from dataclasses import dataclass
from pathlib import Path

from recon.config import Settings
from recon.mapping.baseline import propose_mapping
from recon.mapping.combine import combine
from recon.mapping.llm import LlmMapper
from recon.mapping.llm_client import AnthropicJson, JsonModel, ReplayJson
from recon.mapping.pairing import Mapper, each_file_alone
from recon.mapping.profile import FileProfile
from recon.mapping.proposal import MappingProposal


@dataclass(frozen=True)
class MapperChoice:
    """A mapper and how to describe it to the user."""

    name: str
    model: str | None
    propose: Mapper


def has_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_mapper(settings: Settings) -> MapperChoice:
    """The rules alone, or the rules combined with a language model.

    Without credentials and without a replay file the rules are used, and the interface says so,
    so the application works for someone who has no API key.
    """
    wants_model = settings.mapper == "hybrid" or (
        settings.mapper == "auto" and (has_credentials() or settings.llm_replay is not None)
    )
    if not wants_model:
        return MapperChoice("rules", None, propose_mapping)

    client: JsonModel
    if settings.llm_replay is not None:
        replay = ReplayJson(Path(settings.llm_replay))
        if not len(replay):
            raise ValueError(f"{settings.llm_replay} holds no recorded answers")
        client = replay
    elif has_credentials():
        client = AnthropicJson(
            model=settings.llm_model,
            effort=settings.llm_effort,
            fallbacks=settings.llm_fallbacks,
        )
    else:
        raise ValueError(
            "RECON_MAPPER=hybrid needs ANTHROPIC_API_KEY or RECON_LLM_REPLAY; "
            "use RECON_MAPPER=rules to run without a model"
        )

    model_mapper = each_file_alone(LlmMapper(client).propose_file)

    def hybrid(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        return combine(propose_mapping(invoices, payments), model_mapper(invoices, payments))

    return MapperChoice("rules and model", settings.llm_model, hybrid)
