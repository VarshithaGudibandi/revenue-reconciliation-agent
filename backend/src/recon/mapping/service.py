"""Choosing who proposes column mappings, from configuration."""

from dataclasses import dataclass
from pathlib import Path

from recon.config import Settings
from recon.llm.client import JsonModel, ReplayJson
from recon.llm.providers import live_client
from recon.mapping.baseline import propose_mapping
from recon.mapping.combine import combine
from recon.mapping.llm import LlmMapper
from recon.mapping.pairing import Mapper, each_file_alone
from recon.mapping.profile import FileProfile
from recon.mapping.proposal import MappingProposal


@dataclass(frozen=True)
class MapperChoice:
    """A mapper and how to describe it to the user."""

    name: str
    model: str | None
    propose: Mapper


def has_credentials(settings: Settings) -> bool:
    return settings.llm_key() is not None


def json_client(settings: Settings) -> JsonModel | None:
    """The language model client the settings call for, or None when only the rules are wanted.

    Without a key and without a replay file the rules are used, and the interface says so,
    so the application works for someone who has no API key.
    """
    wants_model = settings.mapper == "hybrid" or (
        settings.mapper == "auto" and (has_credentials(settings) or settings.llm_replay is not None)
    )
    if not wants_model:
        return None
    if settings.llm_replay is not None:
        replay = ReplayJson(Path(settings.llm_replay))
        if not len(replay):
            raise ValueError(f"{settings.llm_replay} holds no recorded answers")
        return replay
    if has_credentials(settings):
        return live_client(
            settings.llm_provider,
            model=settings.llm_model,
            key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
            base_url=settings.llm_base_url,
            effort=settings.llm_effort,
            fallbacks=settings.llm_fallbacks,
        )
    raise ValueError(
        "RECON_MAPPER=hybrid needs an API key or RECON_LLM_REPLAY; "
        "use RECON_MAPPER=rules to run without a model"
    )


def build_mapper(settings: Settings) -> MapperChoice:
    """The rules alone, or the rules combined with a language model."""
    client = json_client(settings)
    if client is None:
        return MapperChoice("rules", None, propose_mapping)

    model_mapper = each_file_alone(LlmMapper(client).propose_file)

    def hybrid(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        return combine(propose_mapping(invoices, payments), model_mapper(invoices, payments))

    return MapperChoice("rules and model", settings.llm_model_name(), hybrid)
