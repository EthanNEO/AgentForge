"""Environment-configured gateway for OpenAI-compatible model providers."""
from __future__ import annotations

import json
import os
import re
import time
import base64
import binascii
import urllib.error
import urllib.request
from dataclasses import dataclass
from http.client import IncompleteRead
from typing import Callable, Mapping
from threading import Lock


from contextvars import ContextVar
from contextlib import contextmanager
import logging

_usage_scope = ContextVar("model_usage_scope", default=None)


@contextmanager
def track_usage(phase: str, run_id=None, node_id=None, *, budget=None):
    token = _usage_scope.set(((phase, run_id, node_id), budget))
    try:
        yield
    finally:
        _usage_scope.reset(token)


class GatewayError(RuntimeError):
    """A model gateway failure safe to record in events and API responses."""


class ModelBudgetExceeded(GatewayError):
    tokens = 0
    model = ""
    provider = ""

    def __init__(self, message: str, *, used=None, limit=None):
        super().__init__(message)
        self.used = used
        self.limit = limit


class ModelBudget:
    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.lock = Lock()

    def check(self):
        with self.lock:
            if self.used >= self.limit:
                raise ModelBudgetExceeded(
                    f"执行已达到 {self.limit:,} tokens 的模型用量上限（已报告 {self.used:,}）",
                    used=self.used, limit=self.limit,
                )

    def record(self, tokens: int):
        with self.lock:
            self.used += tokens


class ToolRoundLimitError(GatewayError):
    def __init__(self, limit: int, tokens: int, model: str, provider: str):
        super().__init__(f"工具调用轮数超过上限 {limit}")
        self.tokens = tokens
        self.model = model
        self.provider = provider


class TransientTransportError(RuntimeError):
    """A network-level failure that is safe to retry within a single generation."""


def _failure_category(exc: Exception) -> str:
    """Classify a gateway failure so fallback policy and evidence can differ by cause."""
    text = str(exc)
    if isinstance(exc, TransientTransportError):
        if "HTTP 429" in text:
            return "rate_limited"
        if re.search(r"HTTP 5\d\d", text):
            return "server_error"
        if "timed out" in text or "timeout" in text.lower():
            return "timeout"
        return "network"
    match = re.search(r"HTTP (\d{3})", text)
    if match:
        code = int(match[1])
        if code in (401, 403, 404):
            return "auth_rejected"
        if code == 429:
            return "rate_limited"
        if code >= 500:
            return "server_error"
        return "http_error"
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_response"
    if "响应未包含文本内容" in text:
        return "empty_response"
    if "长度上限" in text:
        return "length_limit"
    return "unknown"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    display_name: str
    base_url: str
    api_key: str
    default_model: str
    model_prefixes: tuple[str, ...]
    vision_models: tuple[str, ...]


@dataclass(frozen=True)
class LLMResult:
    text: str
    tokens: int
    latency: float
    model: str
    provider: str
    tool_calls: int = 0
    tool_trace: tuple[dict, ...] = ()


Transport = Callable[..., dict]


def _first(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = env.get(name, "").strip()
        if value:
            return value
    return default


class ModelGateway:
    def __init__(self, environ: Mapping[str, str] | None = None,
                 transport: Transport | None = None,
                 retry_delays: tuple[float, ...] = (1.0, 2.0)):
        env = os.environ if environ is None else environ
        legacy_provider = env.get("AF_LLM_PROVIDER", "").strip().lower()
        legacy_key = env.get("AF_LLM_API_KEY", "").strip()
        legacy_url = env.get("AF_LLM_BASE_URL", "").strip()
        legacy_model = env.get("AF_LLM_MODEL", "").strip()

        deepseek_key = _first(env, "AF_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY")
        mimo_key = _first(env, "AF_MIMO_API_KEY", "MIMO_API_KEY")
        if legacy_key and legacy_provider in {"deepseek", "openai"} and not deepseek_key:
            deepseek_key = legacy_key
        if legacy_key and legacy_provider == "mimo" and not mimo_key:
            mimo_key = legacy_key
        mimo_default_url = (
            "https://token-plan-cn.xiaomimimo.com/v1"
            if mimo_key.startswith(("tp-", "ttp-"))
            else "https://api.xiaomimimo.com/v1"
        )

        qwen_model = "qwen-latest-series-invite-1102"
        self.providers = {
            "qwen": ProviderConfig(
                "qwen", "Qwen4-Max-Preview", "https://qweneval.aliyuncs.com/v1",
                _first(env, "MDATAPLUS_API_KEY"), qwen_model, ("qwen",), (qwen_model,),
            ),
            "deepseek": ProviderConfig(
                name="deepseek",
                display_name="DeepSeek",
                base_url=_first(
                    env, "AF_DEEPSEEK_BASE_URL",
                    default=legacy_url if legacy_provider in {"deepseek", "openai"} and legacy_url
                    else "https://api.deepseek.com",
                ).rstrip("/"),
                api_key=deepseek_key,
                default_model=_first(
                    env, "AF_DEEPSEEK_MODEL",
                    default=legacy_model if legacy_provider in {"deepseek", "openai"} and legacy_model
                    else "deepseek-flash",
                ),
                model_prefixes=("deepseek",),
                vision_models=("deepseek-flash", "deepseek-v4-flash-vision-exp"),
            ),
            "mimo": ProviderConfig(
                name="mimo",
                display_name="Xiaomi MiMo",
                base_url=_first(
                    env, "AF_MIMO_BASE_URL",
                    default=legacy_url if legacy_provider == "mimo" and legacy_url
                    else mimo_default_url,
                ).rstrip("/"),
                api_key=mimo_key,
                default_model=_first(
                    env, "AF_MIMO_MODEL",
                    default=legacy_model if legacy_provider == "mimo" and legacy_model
                    else "mimo-v2.6-pro",
                ),
                model_prefixes=("mimo",),
                vision_models=tuple(
                    value.strip() for value in env.get(
                        "AF_MIMO_VISION_MODELS", "mimo-v2.6-pro,mimo-v2.5"
                    ).split(",") if value.strip()
                ),
            ),
        }
        requested_default = env.get("AF_DEFAULT_PROVIDER", "").strip().lower()
        self.default_provider = requested_default if requested_default in self.providers else "deepseek"
        self._qwen_disabled_until = 0.0
        self._qwen_failures = 0
        self._transport = transport or self._http_transport
        self._retry_delays = tuple(retry_delays)

    def available(self, provider: str | None = None) -> bool:
        if provider:
            item = self.providers.get(provider.lower())
            return bool(item and item.api_key and (item.name != "qwen" or time.monotonic() >= self._qwen_disabled_until))
        return any(self.available(name) for name in self.providers)

    def status(self) -> dict:
        return {
            "mode": "live" if self.available() else "mock",
            "default_provider": self.default_provider,
            "providers": {
                name: {
                    "configured": bool(item.api_key),
                    "available": self.available(name),
                    "base_url": item.base_url,
                    "default_model": item.default_model,
                    "vision": item.default_model in item.vision_models,
                }
                for name, item in self.providers.items()
            },
        }

    def _resolve(self, provider: str | None, model: str | None) -> tuple[ProviderConfig, str]:
        forced_provider = bool(provider and provider.strip())
        selected_name = provider.lower().strip() if provider else ""
        inferred_name = ""
        if model:
            lowered = model.lower()
            inferred_name = next((
                name for name, item in self.providers.items()
                if lowered.startswith(item.model_prefixes)
            ), "")
        if not selected_name:
            selected_name = inferred_name or self.default_provider
            if not inferred_name and not self.available(selected_name):
                selected_name = next(
                    (name for name in ("deepseek", "mimo") if self.available(name)),
                    selected_name,
                )
        item = self.providers.get(selected_name)
        if not item:
            raise GatewayError(f"未知模型 Provider：{selected_name or '<empty>'}")
        if not item.api_key and not forced_provider:
            fallback_name = next(
                (name for name in (self.default_provider, "deepseek", "mimo")
                 if name != selected_name and self.available(name)),
                "",
            )
            if fallback_name:
                selected_name = fallback_name
                item = self.providers[selected_name]
        if not item.api_key:
            raise GatewayError(f"{item.display_name} 未配置 API Key")
        actual_model = model if inferred_name == selected_name else item.default_model
        return item, actual_model or item.default_model

    def complete(self, system: str, user: str, model: str | None = None,
                 temperature: float = 0.2, provider: str | None = None, **kwargs) -> LLMResult:
        wants_qwen = (
            (provider or "").strip().lower() == "qwen"
            or (provider is None and bool(model and model.lower().startswith("qwen")))
            or (provider is None and not model and bool(self.providers["qwen"].api_key))
        )
        if not wants_qwen:
            return self._complete(system, user, model, temperature, provider, **kwargs)

        # 冷却/停用期间普通调用直接走备用、不再等待；显式钉住千问（评测）绕过临时冷却、
        # 不做跨模型回退；永久停用对钉住调用也生效。
        pinned_qwen = (provider or "").strip().lower() == "qwen"
        permanent = self._qwen_disabled_until == float("inf")
        cooling = time.monotonic() < self._qwen_disabled_until
        if pinned_qwen and permanent:
            raise GatewayError("千问评测模型已停用")

        failure = None
        qwen_category = None
        qwen_wait = 0.0
        qwen_cooldown = None
        if not permanent and (pinned_qwen or not cooling):
            qwen_started = time.monotonic()
            try:
                options = {**kwargs, "timeout": min(kwargs.get("timeout", 90), 20)}
                result = self._complete(system, user, model, temperature, "qwen", **options)
            except (ModelBudgetExceeded, ToolRoundLimitError):
                raise
            except GatewayError as exc:
                failure = exc
                qwen_wait = round(time.monotonic() - qwen_started, 2)
                qwen_category = getattr(exc, "category", None) or _failure_category(exc)
                self._qwen_failures += 1
                if qwen_category == "auth_rejected":
                    self._qwen_disabled_until = float("inf")
                elif qwen_category in ("timeout", "rate_limited"):
                    # 连续失败递增冷却：60 → 120 → 240 → 300 秒封顶；成功即复位。
                    qwen_cooldown = min(60 * (2 ** (self._qwen_failures - 1)), 300)
                    self._qwen_disabled_until = time.monotonic() + qwen_cooldown
                else:
                    qwen_cooldown = 300
                    self._qwen_disabled_until = time.monotonic() + 300
            else:
                self._qwen_failures = 0
                return result
        if pinned_qwen:
            if qwen_category is not None:
                self._record_fallback(qwen_category, qwen_wait, None, None, str(failure), qwen_cooldown)
            raise failure or GatewayError("千问评测模型不可用")
        for candidate in dict.fromkeys((self.default_provider, "deepseek", "mimo")):
            if candidate == "qwen" or not self.available(candidate):
                continue
            item = self.providers[candidate]
            if kwargs.get("image_inputs") and item.default_model not in item.vision_models:
                continue
            try:
                result = self._complete(system, user, None, temperature, candidate, **kwargs)
            except (ModelBudgetExceeded, ToolRoundLimitError):
                raise
            except GatewayError as exc:
                failure = exc
            else:
                if qwen_category is not None:
                    self._record_fallback(qwen_category, qwen_wait, result.provider, result.model,
                                          str(failure), qwen_cooldown)
                return result
        if qwen_category is not None:
            self._record_fallback(qwen_category, qwen_wait, None, None, str(failure), qwen_cooldown)
        raise failure or GatewayError("千问评测模型不可用，且没有可用的备用模型")

    def _record_fallback(self, category: str, wait: float, provider: str | None,
                         model: str | None, detail: str, cooldown: float | None = None) -> None:
        """Keep fallback evidence (cause, wait, cooldown, takeover model) for the run monitor."""
        scope, _ = _usage_scope.get() or (None, None)
        phase, run_id, node_id = scope or (None, None, None)
        try:
            from . import db

            db.execute(
                "INSERT INTO gateway_fallbacks(run_id,phase,node_id,from_provider,from_model,"
                "category,wait_seconds,to_provider,to_model,detail,cooldown_seconds,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, phase, node_id, "qwen", self.providers["qwen"].default_model,
                 category, wait, provider, model, (detail or "")[:300], cooldown, db.now()),
            )
        except Exception:
            logging.getLogger(__name__).exception("Could not persist gateway fallback")

    def _complete(self, system: str, user: str, model: str | None = None,
                 temperature: float = 0.2, provider: str | None = None,
                 timeout: int = 90, max_output_tokens: int = 4096,
                 tool_definitions: list[dict] | None = None,
                 tool_executor: Callable[[str, dict], dict | str] | None = None,
                 tool_observer: Callable[[dict], None] | None = None,
                 max_tool_rounds: int = 6,
                 image_inputs: list[dict] | None = None) -> LLMResult:
        item, actual_model = self._resolve(provider, model)
        if image_inputs and actual_model not in item.vision_models:
            raise GatewayError(f"{item.display_name} 模型 {actual_model} 不支持图片输入")
        user_content: str | list[dict] = user
        if image_inputs:
            blocks = [{"type": "text", "text": user}]
            for image in image_inputs:
                mime = image.get("mime") if isinstance(image, dict) else None
                data = image.get("data") if isinstance(image, dict) else None
                if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                    raise GatewayError("图片 MIME 类型不受支持")
                if not isinstance(data, str):
                    raise GatewayError("图片数据无效")
                try:
                    base64.b64decode(data, validate=True)
                except (binascii.Error, ValueError):
                    raise GatewayError("图片 base64 数据无效") from None
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}", "detail": "auto"},
                })
            user_content = blocks
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        started = time.time()
        total_tokens = 0
        responses = metered_responses = reported_tokens = 0
        scope, budget = _usage_scope.get() or (None, None)
        outcome = "failed"
        reported_model = actual_model
        tool_rounds = 0
        trace: list[dict] = []
        try:
            while True:
                payload = {
                    "model": actual_model,
                    "messages": messages,
                    "temperature": temperature,
                    "stream": False,
                    "thinking": {"type": "disabled"},
                }
                if item.name == "qwen":
                    payload.pop("thinking")
                if item.name == "mimo":
                    payload["max_completion_tokens"] = max_output_tokens
                else:
                    payload["max_tokens"] = max_output_tokens
                if tool_definitions:
                    payload["tools"] = tool_definitions
                    payload["tool_choice"] = "auto"
                response = self._call_with_retry(
                    url=f"{item.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {item.api_key}",
                        "Content-Type": "application/json",
                    },
                    payload=payload,
                    timeout=timeout,
                )
                responses += 1
                reported_model = str(response.get("model") or actual_model)
                usage = response.get("usage") or {}
                count = usage.get("total_tokens")
                if type(count) is int and count >= 0:
                    metered_responses += 1
                    reported_tokens += count
                    total_tokens += count
                    if budget is not None:
                        budget.record(count)
                choice = response["choices"][0]
                if choice.get("finish_reason") == "length":
                    error = GatewayError("模型输出达到长度上限，候选响应不完整")
                    error.category = "length_limit"
                    raise error
                message = choice["message"]
                calls = message.get("tool_calls") or []
                if calls:
                    if not tool_definitions or tool_executor is None:
                        raise GatewayError("模型返回工具调用，但未配置工具执行器")
                    if tool_rounds >= max_tool_rounds:
                        raise ToolRoundLimitError(max_tool_rounds, total_tokens, actual_model, item.name)
                    assistant_message = {
                        "role": "assistant",
                        "content": message.get("content"),
                        "tool_calls": calls,
                    }
                    if message.get("reasoning_content") is not None:
                        assistant_message["reasoning_content"] = message["reasoning_content"]
                    messages.append(assistant_message)
                    tool_rounds += 1
                    for call in calls:
                        call_id = str(call.get("id") or "")
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        raw_arguments = function.get("arguments") or "{}"
                        failure_kind = None
                        try:
                            arguments = json.loads(raw_arguments)
                            if not isinstance(arguments, dict):
                                raise ValueError("arguments must be an object")
                        except (TypeError, ValueError, json.JSONDecodeError) as exc:
                            arguments = {}
                            failure_kind = "invalid_arguments"
                            result = {"ok": False, "error": f"工具参数无效：{exc}"}
                        else:
                            try:
                                result = tool_executor(name, arguments)
                            except Exception as exc:  # noqa: BLE001
                                failure_kind = "execution_error"
                                result = {"ok": False, "error": self._sanitize(str(exc))}
                        rendered = result if isinstance(result, str) else json.dumps(
                            result, ensure_ascii=False
                        )
                        # Local file reads and test output already have their own bounds.
                        # A second cut here would discard assertions and break JSON.
                        limit = 26_000 if name in {"read_file", "run_tests"} else 6_000
                        if len(rendered) > limit:
                            rendered = json.dumps({
                                "ok": False,
                                "error": "工具结果超过传输上限",
                                "preview": rendered[:limit // 2],
                            }, ensure_ascii=False)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": rendered,
                        })
                        trace_item = {
                            "name": name,
                            "round": tool_rounds,
                            "ok": bool(result.get("ok", True)) if isinstance(result, dict) else True,
                            "source": (
                                "mcp" if name.startswith("github__") else "workspace"
                            ),
                        }
                        if not trace_item["ok"]:
                            trace_item["failure_kind"] = failure_kind or "tool_rejected"
                            if isinstance(raw_arguments, str):
                                trace_item["argument_chars"] = len(raw_arguments)
                        if isinstance(arguments.get("path"), str):
                            trace_item["path"] = arguments["path"][:300]
                        if isinstance(result, dict):
                            if isinstance(result.get("server"), str):
                                trace_item["server"] = result["server"][:100]
                            if isinstance(result.get("repository"), str):
                                trace_item["repository"] = result["repository"][:300]
                        trace.append(trace_item)
                        if tool_observer is not None:
                            tool_observer(dict(trace_item))
                    continue

                text = str(message.get("content") or "").strip()
                if not text:
                    raise ValueError("响应未包含文本内容")
                if not total_tokens:
                    total_tokens = max(1, (len(system) + len(user) + len(text)) // 4)
                outcome = "completed"
                return LLMResult(
                    text=text,
                    tokens=total_tokens,
                    latency=round(time.time() - started, 3),
                    model=str(response.get("model") or actual_model),
                    provider=item.name,
                    tool_calls=len(trace),
                    tool_trace=tuple(trace),
                )
        except ModelBudgetExceeded as exc:
            exc.tokens = total_tokens
            exc.model = reported_model
            exc.provider = item.name
            raise
        except GatewayError:
            raise
        except Exception as exc:  # noqa: BLE001
            error = GatewayError(f"{item.display_name} 调用失败：{self._sanitize(str(exc))}")
            error.category = _failure_category(exc)
            raise error from None

        finally:
            if scope is not None:
                from . import db

                try:
                    db.execute(
                        "INSERT INTO model_usage(phase,run_id,node_id,provider,model,reported_tokens,"
                        "responses,metered_responses,outcome,latency,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (*scope, item.name, reported_model, reported_tokens, responses, metered_responses,
                         outcome, round(time.time() - started, 3), db.now()),
                    )
                except Exception:
                    logging.getLogger(__name__).exception("Could not persist model usage")

    def supports_vision(self, model: str | None = None, provider: str | None = None) -> bool:
        try:
            item, actual_model = self._resolve(provider, model)
        except GatewayError:
            return False
        return actual_model in item.vision_models

    def _sanitize(self, message: str) -> str:
        cleaned = message
        for item in self.providers.values():
            if item.api_key:
                cleaned = cleaned.replace(item.api_key, "***")
        return cleaned[:500] or "未知错误"

    def _call_with_retry(self, *, url: str, headers: dict, payload: dict, timeout: int) -> dict:
        delays = () if payload["model"].startswith("qwen") else self._retry_delays
        _, budget = _usage_scope.get() or (None, None)
        for attempt in range(len(delays) + 1):
            if budget is not None:
                budget.check()
            try:
                return self._transport(url=url, headers=headers, payload=payload, timeout=timeout)
            except TransientTransportError:
                if attempt >= len(delays):
                    raise
                time.sleep(delays[attempt])
        raise AssertionError("unreachable")

    @staticmethod
    def _http_transport(*, url: str, headers: dict, payload: dict, timeout: int) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code in (408, 429) or exc.code >= 500:
                raise TransientTransportError(f"HTTP {exc.code}: {detail}") from None
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError, IncompleteRead) as exc:
            raise TransientTransportError(f"网络错误：{exc}") from None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError("服务返回了无效 JSON") from None


# Compatibility import used by the execution engine.
llm = ModelGateway()
