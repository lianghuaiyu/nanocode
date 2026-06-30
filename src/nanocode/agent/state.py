"""agent/state.py — AgentState：SessionManager.build_context() 的可丢弃运行态投影（docs/15 §6）。

§0 硬不变量：AgentState 绝不是 durable truth。它由 `build_context()`（中立 Message[] + ScalarState）
+ AgentProfile / runtime config 重建，并能 render 成任一 provider 的请求 payload，**无需任何
provider-specific durable messages**——旧的 `_anthropic_messages` / `_openai_messages` 降为
request-local `ProviderProjection`，每个请求由 `AgentState.project()` 重建。

管线（与 engine._build_request_messages 等价，形式化）：
    SessionManager.build_context() → BuiltContext(messages, scalar)
        → AgentState.hydrate(...) → AgentState.project() → render(...) → provider payload
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..session.context import ScalarState
from ..session.manager import BuiltContext
from ..session.render import ModelCtx, render


def provider_api(provider: str) -> str:
    """provider → 默认 api 字符串（render/ModelCtx 用）。openai 兼容族统一 openai-completions。"""
    return "openai-completions" if provider == "openai" else "anthropic"


@dataclass
class ProviderProjection:
    """单次请求的 provider-shaped payload（render 产物）。**request-local、可丢弃**——
    取代旧的 durable `self._{provider}_messages`。每个请求由 `AgentState.project()` 重建。

    anthropic：system 走 out-of-band（`system` 字段，喂给 SDK 的 system 参数）；
    openai：system 已在 `messages[0]`，`system` 字段为 None。
    """

    provider: str
    messages: list[dict]
    system: str | None = None


@dataclass
class AgentState:
    """active branch 的可丢弃运行态投影（docs/15 §6）。

    durable truth = SessionManager。本对象由 `hydrate()` 从 `build_context()` + config 重建，
    `project()` 渲染成 provider 请求。**不**持有 provider-specific durable messages。
    """

    # 中立事实（来自 build_context；branch 折叠后的 root-first 中立 Message[]）
    messages: list[dict] = field(default_factory=list)
    scalar: ScalarState = field(default_factory=ScalarState)
    # provider / model 运行配置
    provider: str = "anthropic"
    api: str = "anthropic"
    model: str = ""
    thinking_level: str = "disabled"          # disabled | enabled | adaptive
    active_tools: list[str] | None = None      # None = 全量工具表
    supports_images: bool = True
    # 系统前缀（稳定身份 + 行为规则；ContextRuntime 之后即 stable prefix）
    system_prompt: str | None = None
    # runtime counters（可丢弃投影；真相由树的 usage / turn_end 派生）
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_input_token_count: int = 0
    current_turns: int = 0
    # flags
    aborted: bool = False
    streaming: bool = False

    # ── hydrate（tree → state，§3.2/§6）────────────────────────────────────────
    @classmethod
    def hydrate(
        cls,
        built: BuiltContext,
        *,
        provider: str,
        model: str,
        system_prompt: str | None = None,
        thinking_level: str = "disabled",
        active_tools: list[str] | None = None,
        supports_images: bool = True,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        last_input_token_count: int = 0,
        current_turns: int = 0,
    ) -> "AgentState":
        """从 canonical 树的 `build_context()` 重建 AgentState。

        分支折叠出的 scalar（provider/model/thinking/active_tools，末态胜）优先于传入默认值——
        这保证 model_change / 末条 assistant 记录的 provider/model 被忠实重建（resume 一致性）。
        """
        scalar = built.scalar
        eff_provider = scalar.provider or provider
        return cls(
            messages=list(built.messages),
            scalar=scalar,
            provider=eff_provider,
            api=provider_api(eff_provider),
            model=scalar.model_id or model,
            thinking_level=scalar.thinking_level or thinking_level,
            active_tools=(scalar.active_tools if scalar.active_tools is not None else active_tools),
            supports_images=supports_images,
            system_prompt=system_prompt,
            total_input_tokens=total_input_tokens,
            total_output_tokens=total_output_tokens,
            last_input_token_count=last_input_token_count,
            current_turns=current_turns,
        )

    # ── project（state → provider 请求，§6）────────────────────────────────────
    def model_ctx(self) -> ModelCtx:
        return ModelCtx(
            provider=self.provider, api=self.api, model_id=self.model,
            supports_images=self.supports_images,
        )

    def project(self) -> ProviderProjection:
        """render active branch → provider 请求 payload。**每次请求重建**，绝不缓存为 durable。

        与 engine._build_request_messages 行为一致：
        - anthropic：render(system_prompt=None)，system 单独走 out-of-band（projection.system）；
        - openai：render(system_prompt=self.system_prompt)，system 进 messages[0]，projection.system=None。
        """
        if self.provider == "openai":
            payload = render(self.messages, self.model_ctx(), system_prompt=self.system_prompt)
            return ProviderProjection(provider="openai", messages=payload["messages"], system=None)
        payload = render(self.messages, self.model_ctx(), system_prompt=None)
        return ProviderProjection(provider=self.provider, messages=payload["messages"],
                                  system=self.system_prompt)
