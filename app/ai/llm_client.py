from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from openai import APIConnectionError, APITimeoutError, BadRequestError, OpenAI, OpenAIError
from openai.types.chat import ChatCompletion


DEFAULT_BASE_URL = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
DEFAULT_API_KEY = os.getenv("LM_STUDIO_API_KEY", "lm-studio")
DEFAULT_MODEL = os.getenv("LM_STUDIO_MODEL", "local-model")


class LocalLLMError(RuntimeError):
    pass


@dataclass
class ModelSelectionState:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = DEFAULT_API_KEY
    selected_model: str = DEFAULT_MODEL
    last_models: list[dict[str, Any]] = field(default_factory=list)


MODEL_STATE = ModelSelectionState()


class LocalLLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 700.0,
    ) -> None:
        self.base_url = base_url or MODEL_STATE.base_url
        self.api_key = api_key or MODEL_STATE.api_key
        self.model = model or MODEL_STATE.selected_model
        self.timeout = timeout
        _ensure_local_base_url(self.base_url)
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=timeout)

    def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> ChatCompletion:
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": 0.1,
            }
            if tools:
                request["tools"] = tools
                request["tool_choice"] = tool_choice

            response = self.client.chat.completions.create(
                **request,
            )
        except APITimeoutError as exc:
            raise LocalLLMError("Timeout ao chamar o LM Studio.") from exc
        except APIConnectionError as exc:
            raise LocalLLMError("LM Studio indisponivel em http://localhost:1234/v1.") from exc
        except BadRequestError as exc:
            raise LocalLLMError(f"Modelo ou requisicao recusada pelo LM Studio: {exc}") from exc
        except (OpenAIError, IndexError, AttributeError) as exc:
            raise LocalLLMError(f"Erro de comunicacao com o LM Studio: {exc}") from exc

        return response

    def chat(self, messages: list[dict[str, Any]]) -> str:
        try:
            response = self.chat_completion(messages)
            content = response.choices[0].message.content
        except LocalLLMError:
            raise
        except (IndexError, AttributeError) as exc:
            raise LocalLLMError(f"Resposta invalida do LM Studio: {exc}") from exc

        if not content:
            raise LocalLLMError("Resposta vazia ou invalida do modelo.")
        return content

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ChatCompletion:
        return self.chat_completion(messages=messages, tools=tools, tool_choice="auto")

    def list_models(self) -> dict[str, Any]:
        try:
            response = self.client.models.list()
            models = []
            for item in response.data:
                model_data = item.model_dump() if hasattr(item, "model_dump") else dict(item)
                models.append(model_data)
            MODEL_STATE.last_models = models
            return {
                "ok": True,
                "base_url": self.base_url,
                "selected_model": MODEL_STATE.selected_model,
                "models": models,
                "message": "Modelos carregados do LM Studio.",
            }
        except APITimeoutError as exc:
            raise LocalLLMError("Timeout ao listar modelos no LM Studio.") from exc
        except APIConnectionError as exc:
            raise LocalLLMError("LM Studio indisponivel. Inicie o servidor local na porta 1234.") from exc
        except OpenAIError as exc:
            raise LocalLLMError(f"Erro ao listar modelos do LM Studio: {exc}") from exc

    def get_current_model(self) -> str:
        return MODEL_STATE.selected_model

    def set_model(self, model_name: str) -> dict[str, Any]:
        if not model_name or not model_name.strip():
            raise LocalLLMError("Nome do modelo nao pode ficar vazio.")

        cleaned = model_name.strip()
        MODEL_STATE.selected_model = cleaned
        self.model = cleaned
        known_ids = {str(model.get("id")) for model in MODEL_STATE.last_models if model.get("id")}
        is_known = cleaned in known_ids if known_ids else None
        return {
            "selected_model": cleaned,
            "is_known_to_last_model_list": is_known,
            "message": (
                "Modelo selecionado para as proximas chamadas. "
                "Se ele nao estiver carregado no LM Studio, a chamada de chat pode falhar."
            ),
        }

    def load_model(self, model_name: str) -> dict[str, Any]:
        """Optional LM Studio native API load attempt.

        The OpenAI-compatible API selects a model by name per request. LM Studio's
        native REST API exposes model loading separately at /api/v1/models/load.
        """
        import urllib.error
        import urllib.request
        import json

        native_base = self.base_url.replace("/v1", "/api/v1").rstrip("/")
        url = f"{native_base}/models/load"
        payload = json.dumps({"model": model_name}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        token = os.getenv("LM_STUDIO_API_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                parsed = json.loads(body) if body else {}
                return {"ok": True, "message": "Solicitacao de carga enviada.", "data": parsed}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise LocalLLMError(f"LM Studio recusou o carregamento do modelo: {body}") from exc
        except urllib.error.URLError as exc:
            raise LocalLLMError("Endpoint nativo de carga do LM Studio indisponivel.") from exc
        except TimeoutError as exc:
            raise LocalLLMError("Timeout ao tentar carregar modelo no LM Studio.") from exc


def _ensure_local_base_url(base_url: str) -> None:
    host = urlparse(base_url).hostname
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise LocalLLMError(
            "Por seguranca, a IA so pode chamar LM Studio local em localhost, 127.0.0.1 ou ::1."
        )
