"""Building an adapter for each configured model (the API legs; CLI legs: `cli_agents`)."""

from __future__ import annotations

from collections.abc import Callable

from benchmark.k2val.config import K2valConfig, ModelConf, read_model_key
from benchmark.k2val.spend import SpendTracker
from benchmark.llm_adapters import LLMConfig, OpenAICompatAdapter
from benchmark.llm_adapters.anthropic_api import AnthropicApiAdapter, AnthropicSettings

# (model conf, callback receiving the cost of each Anthropic call) -> adapter
AdapterFactory = Callable[[ModelConf, Callable[[float], None]], object]


def make_api_adapter_factory(cfg: K2valConfig, spend: SpendTracker | None) -> AdapterFactory:
    """A factory for the API configs. Keys are read from their files here, at run time."""
    keys: dict[str, str] = {}

    def key_of(conf: ModelConf) -> str:
        if conf.name not in keys:
            keys[conf.name] = read_model_key(conf).reveal()
        return keys[conf.name]

    def factory(conf: ModelConf, on_cost: Callable[[float], None]) -> object:
        llm = LLMConfig(
            name=conf.name,
            api_base=conf.base_url or None,
            api_key=key_of(conf),
            model_id=conf.api_id,
            temperature=conf.temperature,
            max_tokens=conf.max_tokens,
            max_tool_rounds=cfg.limits.max_tool_rounds,
            extra=dict(conf.extra_body),
            top_p=conf.top_p,
        )
        if conf.backend == "openai_compat":
            return OpenAICompatAdapter(llm)
        if conf.backend == "anthropic":
            assert spend is not None and conf.price is not None
            price = conf.price

            def on_usage(name: str, response_id: str, tokens_in: int, tokens_out: int) -> float:
                cost = spend.record(name, response_id, tokens_in, tokens_out, price)
                on_cost(cost)
                return cost

            return AnthropicApiAdapter(
                llm,
                settings=AnthropicSettings(effort=conf.effort),
                on_usage=on_usage,
                before_call=spend.before_call,
            )
        raise ValueError(f"no API adapter for backend {conf.backend!r}")

    return factory
