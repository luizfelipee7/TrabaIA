from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Iterator

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.ai.llm_client import LocalLLMClient, LocalLLMError, MODEL_STATE
from app.ai.prompts import DEFAULT_OBJECTIVE, SYSTEM_PROMPT
from app.ai.schemas import AIReportScopeValidation, DailyInventoryReviewReport
from app.ai.tools import TOOL_DEFINITIONS, create_ai_report_tool, register_ai_log_tool, run_tool


READY_SIGNAL = "READY_FOR_FINAL_REPORT"
FINAL_REPORT_MAX_TOKENS = 2500


class InventoryAIAgent:
    def __init__(
        self,
        db: Session,
        llm_client: LocalLLMClient | None = None,
        max_steps: int = 8,
        max_repeated_calls: int = 2,
    ) -> None:
        self.db = db
        self.llm_client = llm_client or LocalLLMClient()
        self.max_steps = max_steps
        self.max_repeated_calls = max_repeated_calls

    def run_daily_inventory_review(self, objective: str | None = None) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": objective or DEFAULT_OBJECTIVE},
        ]
        steps: list[dict[str, Any]] = []
        repeated_calls: dict[str, int] = {}
        final_report_correction_requested = False
        ai_validation_correction_requested = False
        original_request = objective or DEFAULT_OBJECTIVE

        register_ai_log_tool(
            "Inicio da revisao diaria de estoque.",
            {"model": MODEL_STATE.selected_model, "objective": objective or DEFAULT_OBJECTIVE},
        )

        for step_number in range(1, self.max_steps + 1):
            try:
                response = self.llm_client.chat_with_tools(messages, tools=TOOL_DEFINITIONS)
                message = response.choices[0].message
                response_usage = _usage_from_response(response)
            except LocalLLMError as exc:
                error = {"status": "error", "message": str(exc), "steps": steps}
                register_ai_log_tool("Falha ao chamar modelo local.", error)
                return error
            except (IndexError, AttributeError) as exc:
                error = {
                    "status": "error",
                    "message": f"Resposta invalida do modelo: {type(exc).__name__}: {exc}",
                    "steps": steps,
                }
                register_ai_log_tool("Resposta invalida do modelo local.", error)
                return error

            tool_calls = list(message.tool_calls or [])
            assistant_message = message.model_dump(exclude_none=True)

            if tool_calls:
                messages.append(assistant_message)
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = _parse_tool_args(tool_call.function.arguments)
                    call_key = json.dumps(
                        {"tool": tool_name, "args": tool_args},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    repeated_calls[call_key] = repeated_calls.get(call_key, 0) + 1

                    if repeated_calls[call_key] > self.max_repeated_calls:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Chamada de tool repetida muitas vezes. Escolha outra acao ou finalize.",
                        }
                    elif isinstance(tool_args, dict):
                        result = run_tool(self.db, tool_name, tool_args)
                    else:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Argumentos da tool nao sao um objeto JSON valido.",
                            "raw_arguments": tool_call.function.arguments,
                        }

                    steps.append(
                        {
                            "step": step_number,
                            "action_type": "tool_call",
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "tool_result": _summarize_for_trace(result),
                        }
                    )
                    register_ai_log_tool(
                        "Tool executada pela IA.",
                        {
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "result_summary": _summarize_for_trace(result),
                        },
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(
                                _compact_tool_result_for_model(result),
                                ensure_ascii=False,
                                default=str,
                            ),
                        }
                    )
                continue

            final_content = "" if (message.content or "").strip() == READY_SIGNAL else message.content or ""
            structured_result = _request_structured_final_report(
                self.llm_client,
                messages,
                fallback_content=final_content,
            )
            steps.append(
                {
                    "step": step_number,
                    "action_type": "structured_output",
                    "used": structured_result["used"],
                    "fallback_used": structured_result["fallback_used"],
                    "error": structured_result.get("error"),
                    "usage": structured_result.get("usage"),
                }
            )
            if structured_result["fallback_used"]:
                register_ai_log_tool(
                    "Structured output indisponivel; usando fallback com parse e validacao.",
                    {"error": structured_result.get("error")},
                )
            final_content = structured_result["content"]
            messages.extend(structured_result.get("history", []))
            validation = _validate_final_report(final_content)
            if not validation["ok"]:
                steps.append(
                    {
                        "step": step_number,
                        "action_type": "invalid_final_report",
                        "message": validation["error"],
                        "raw_message": final_content,
                    }
                )
                register_ai_log_tool(
                    "Relatorio final invalido retornado pela IA.",
                    {"error": validation["error"], "raw_message": final_content[:2000]},
                )
                if not final_report_correction_requested:
                    final_report_correction_requested = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "O JSON final anterior nao validou no schema DailyInventoryReviewReport. "
                                "Corrija uma unica vez. Responda apenas com JSON valido, sem markdown, "
                                "sem texto extra, mantendo todas as categorias obrigatorias, mesmo vazias. "
                                f"Erro de validacao: {validation['error']}"
                            ),
                        }
                    )
                    continue

                error = {
                    "status": "aborted",
                    "message": "Relatorio final invalido apos uma tentativa de correcao.",
                    "validation_error": validation["error"],
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por relatorio final invalido.", error)
                return error

            final_report = validation["report"]
            deterministic_validation = _validate_report_deterministically(self.db, final_report)
            steps.append(
                {
                    "step": step_number,
                    "action_type": "deterministic_report_validation",
                    "validation": deterministic_validation,
                }
            )
            register_ai_log_tool("Validacao deterministica do relatorio executada.", deterministic_validation)
            if not deterministic_validation["ok"]:
                if not final_report_correction_requested:
                    final_report_correction_requested = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "O relatorio final anterior violou validacoes deterministicas do sistema. "
                                "Corrija uma unica vez. Responda apenas com JSON valido no schema "
                                "DailyInventoryReviewReport, sem markdown e sem texto extra. "
                                f"Erros: {json.dumps(deterministic_validation['errors'], ensure_ascii=False)}"
                            ),
                        }
                    )
                    continue

                error = {
                    "status": "aborted",
                    "message": "Relatorio final falhou na validacao deterministica apos uma correcao.",
                    "deterministic_validation": deterministic_validation,
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por falha deterministica no relatorio.", error)
                return error

            ai_validation = _validate_report_against_request_with_ai(
                self.llm_client,
                original_request=original_request,
                final_report=final_report,
            )
            steps.append(
                {
                    "step": step_number,
                    "action_type": "ai_report_validation",
                    "validation": ai_validation,
                }
            )
            register_ai_log_tool("Validacao por IA executada.", ai_validation)
            if not ai_validation.get("ok"):
                error = {
                    "status": "aborted",
                    "message": "Nao foi possivel validar o relatorio final por IA.",
                    "validation_error": ai_validation.get("error"),
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por falha na validacao por IA.", error)
                return error

            scope_validation = ai_validation["validation"]
            if not scope_validation["passed"]:
                if not ai_validation_correction_requested:
                    ai_validation_correction_requested = True
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "A validacao por IA reprovou o relatorio final em relacao ao pedido original. "
                                "Corrija uma unica vez. Responda apenas com JSON valido no schema "
                                "DailyInventoryReviewReport, sem markdown e sem texto extra. "
                                f"Motivo: {scope_validation['reason']} "
                                f"Violacoes: {json.dumps(scope_validation['violations'], ensure_ascii=False)} "
                                f"Instrucao de correcao: {scope_validation['correction_instruction']}"
                            ),
                        }
                    )
                    continue

                saved_report = create_ai_report_tool("[REPROVADO] Revisao diaria de estoque", final_report)
                error = {
                    "status": "aborted",
                    "message": "Relatorio final reprovado pela validacao por IA apos uma correcao.",
                    "scope_validation": scope_validation,
                    "report": saved_report,
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por reprovacao na validacao por IA.", error)
                return error

            saved_report = create_ai_report_tool("Revisao diaria de estoque", final_report)
            steps.append(
                {
                    "step": step_number,
                    "action_type": "final_message",
                    "message": final_content,
                }
            )
            register_ai_log_tool(
                "Relatorio final gerado pela IA.",
                {"report": saved_report, "summary": final_report.get("executive_summary")},
            )
            return {
                "status": "completed",
                "message": "Revisao concluida.",
                "model": MODEL_STATE.selected_model,
                "ai_validation": scope_validation,
                "final_report": final_report,
                "report": saved_report,
                "steps": steps,
            }

        register_ai_log_tool("Revisao abortada por limite de passos.", {"steps": steps})
        return {
            "status": "aborted",
            "message": f"Limite de {self.max_steps} passos atingido sem relatorio final.",
            "steps": steps,
        }

    def stream_daily_inventory_review(self, objective: str | None = None) -> Iterator[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": objective or DEFAULT_OBJECTIVE},
        ]
        steps: list[dict[str, Any]] = []
        repeated_calls: dict[str, int] = {}
        final_report_correction_requested = False
        ai_validation_correction_requested = False
        original_request = objective or DEFAULT_OBJECTIVE

        register_ai_log_tool(
            "Inicio da revisao diaria de estoque.",
            {"model": MODEL_STATE.selected_model, "objective": objective or DEFAULT_OBJECTIVE},
        )
        yield {
            "event": "run_started",
            "type": "system",
            "message": "Revisao diaria iniciada.",
            "model": MODEL_STATE.selected_model,
            "objective": objective or DEFAULT_OBJECTIVE,
        }

        for step_number in range(1, self.max_steps + 1):
            yield {
                "event": "model_call_start",
                "type": "model",
                "step": step_number,
                "message": "Chamando modelo local com tools disponiveis.",
            }
            try:
                response = self.llm_client.chat_with_tools(messages, tools=TOOL_DEFINITIONS)
                message = response.choices[0].message
                response_usage = _usage_from_response(response)
            except LocalLLMError as exc:
                error = {"status": "error", "message": str(exc), "steps": steps}
                register_ai_log_tool("Falha ao chamar modelo local.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return
            except (IndexError, AttributeError) as exc:
                error = {
                    "status": "error",
                    "message": f"Resposta invalida do modelo: {type(exc).__name__}: {exc}",
                    "steps": steps,
                }
                register_ai_log_tool("Resposta invalida do modelo local.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            tool_calls = list(message.tool_calls or [])
            assistant_message = message.model_dump(exclude_none=True)
            yield {
                "event": "model_message",
                "type": "response" if not tool_calls else "model",
                "step": step_number,
                "message": _visible_message_summary(message.content, tool_calls),
                "tool_call_count": len(tool_calls),
                "usage": response_usage,
            }

            if tool_calls:
                messages.append(assistant_message)
                for tool_call in tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = _parse_tool_args(tool_call.function.arguments)
                    yield {
                        "event": "tool_call",
                        "type": "tool",
                        "step": step_number,
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "message": f"Solicitando tool {tool_name}.",
                    }
                    call_key = json.dumps(
                        {"tool": tool_name, "args": tool_args},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    repeated_calls[call_key] = repeated_calls.get(call_key, 0) + 1

                    if repeated_calls[call_key] > self.max_repeated_calls:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Chamada de tool repetida muitas vezes. Escolha outra acao ou finalize.",
                        }
                    elif isinstance(tool_args, dict):
                        result = run_tool(self.db, tool_name, tool_args)
                    else:
                        result = {
                            "ok": False,
                            "tool_name": tool_name,
                            "error": "Argumentos da tool nao sao um objeto JSON valido.",
                            "raw_arguments": tool_call.function.arguments,
                        }

                    step = {
                        "step": step_number,
                        "action_type": "tool_call",
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "tool_args": tool_args,
                        "tool_result": _summarize_for_trace(result),
                    }
                    steps.append(step)
                    register_ai_log_tool(
                        "Tool executada pela IA.",
                        {
                            "tool_call_id": tool_call.id,
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                            "result_summary": _summarize_for_trace(result),
                        },
                    )
                    yield {
                        "event": "tool_result",
                        "type": "tool",
                        "step": step_number,
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_name,
                        "ok": not (isinstance(result, dict) and result.get("ok") is False),
                        "result": _summarize_for_trace(result),
                    }
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(
                                _compact_tool_result_for_model(result),
                                ensure_ascii=False,
                                default=str,
                            ),
                        }
                    )
                continue

            final_content = "" if (message.content or "").strip() == READY_SIGNAL else message.content or ""
            structured_result = _request_structured_final_report(
                self.llm_client,
                messages,
                fallback_content=final_content,
            )
            structured_step = {
                "step": step_number,
                "action_type": "structured_output",
                "used": structured_result["used"],
                "fallback_used": structured_result["fallback_used"],
                "error": structured_result.get("error"),
                "usage": structured_result.get("usage"),
            }
            steps.append(structured_step)
            yield {
                "event": "structured_output",
                "type": "validation",
                "step": step_number,
                "message": (
                    "Relatorio final recebido via structured output."
                    if structured_result["used"]
                    else "Structured output indisponivel; usando fallback com parse e validacao."
                ),
                "used": structured_result["used"],
                "fallback_used": structured_result["fallback_used"],
                "error": structured_result.get("error"),
                "usage": structured_result.get("usage"),
            }
            if structured_result["fallback_used"]:
                register_ai_log_tool(
                    "Structured output indisponivel; usando fallback com parse e validacao.",
                    {"error": structured_result.get("error")},
                )
            final_content = structured_result["content"]
            messages.extend(structured_result.get("history", []))
            validation = _validate_final_report(final_content)
            if not validation["ok"]:
                invalid_step = {
                    "step": step_number,
                    "action_type": "invalid_final_report",
                    "message": validation["error"],
                    "raw_message": final_content,
                }
                steps.append(invalid_step)
                register_ai_log_tool(
                    "Relatorio final invalido retornado pela IA.",
                    {"error": validation["error"], "raw_message": final_content[:2000]},
                )
                yield {
                    "event": "validation_error",
                    "type": "validation",
                    "step": step_number,
                    "message": validation["error"],
                    "raw_message": final_content,
                }
                if not final_report_correction_requested:
                    final_report_correction_requested = True
                    correction_message = (
                        "O JSON final anterior nao validou no schema DailyInventoryReviewReport. "
                        "Corrija uma unica vez. Responda apenas com JSON valido, sem markdown, "
                        "sem texto extra, mantendo todas as categorias obrigatorias, mesmo vazias. "
                        f"Erro de validacao: {validation['error']}"
                    )
                    messages.append({"role": "user", "content": correction_message})
                    yield {
                        "event": "correction_requested",
                        "type": "validation",
                        "step": step_number,
                        "message": "Solicitada uma correcao do relatorio final.",
                    }
                    continue

                error = {
                    "status": "aborted",
                    "message": "Relatorio final invalido apos uma tentativa de correcao.",
                    "validation_error": validation["error"],
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por relatorio final invalido.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            final_report = validation["report"]
            deterministic_validation = _validate_report_deterministically(self.db, final_report)
            steps.append(
                {
                    "step": step_number,
                    "action_type": "deterministic_report_validation",
                    "validation": deterministic_validation,
                }
            )
            register_ai_log_tool("Validacao deterministica do relatorio executada.", deterministic_validation)
            yield {
                "event": "deterministic_validation_result",
                "type": "validation",
                "step": step_number,
                "validation": deterministic_validation,
                "message": (
                    "Relatorio passou nas validacoes deterministicas."
                    if deterministic_validation["ok"]
                    else "Relatorio falhou nas validacoes deterministicas."
                ),
            }
            if not deterministic_validation["ok"]:
                if not final_report_correction_requested:
                    final_report_correction_requested = True
                    correction_message = (
                        "O relatorio final anterior violou validacoes deterministicas do sistema. "
                        "Corrija uma unica vez. Responda apenas com JSON valido no schema "
                        "DailyInventoryReviewReport, sem markdown e sem texto extra. "
                        f"Erros: {json.dumps(deterministic_validation['errors'], ensure_ascii=False)}"
                    )
                    messages.append({"role": "user", "content": correction_message})
                    yield {
                        "event": "correction_requested",
                        "type": "validation",
                        "step": step_number,
                        "message": "Solicitada uma correcao por falha deterministica do relatorio.",
                    }
                    continue

                error = {
                    "status": "aborted",
                    "message": "Relatorio final falhou na validacao deterministica apos uma correcao.",
                    "deterministic_validation": deterministic_validation,
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por falha deterministica no relatorio.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            yield {
                "event": "ai_validation_start",
                "type": "validation",
                "step": step_number,
                "message": "Validando relatorio final contra o pedido original.",
            }
            ai_validation = _validate_report_against_request_with_ai(
                self.llm_client,
                original_request=original_request,
                final_report=final_report,
            )
            steps.append(
                {
                    "step": step_number,
                    "action_type": "ai_report_validation",
                    "validation": ai_validation,
                }
            )
            register_ai_log_tool("Validacao por IA executada.", ai_validation)
            yield {
                "event": "ai_validation_result",
                "type": "validation",
                "step": step_number,
                "validation": ai_validation,
                "message": _validation_message(ai_validation),
            }
            if not ai_validation.get("ok"):
                error = {
                    "status": "aborted",
                    "message": "Nao foi possivel validar o relatorio final por IA.",
                    "validation_error": ai_validation.get("error"),
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por falha na validacao por IA.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            scope_validation = ai_validation["validation"]
            if not scope_validation["passed"]:
                if not ai_validation_correction_requested:
                    ai_validation_correction_requested = True
                    correction_message = (
                        "A validacao por IA reprovou o relatorio final em relacao ao pedido original. "
                        "Corrija uma unica vez. Responda apenas com JSON valido no schema "
                        "DailyInventoryReviewReport, sem markdown e sem texto extra. "
                        f"Motivo: {scope_validation['reason']} "
                        f"Violacoes: {json.dumps(scope_validation['violations'], ensure_ascii=False)} "
                        f"Instrucao de correcao: {scope_validation['correction_instruction']}"
                    )
                    messages.append({"role": "user", "content": correction_message})
                    yield {
                        "event": "ai_scope_correction_requested",
                        "type": "validation",
                        "step": step_number,
                        "message": scope_validation["correction_instruction"],
                        "validation": scope_validation,
                    }
                    continue

                saved_report = create_ai_report_tool("[REPROVADO] Revisao diaria de estoque", final_report)
                error = {
                    "status": "aborted",
                    "message": "Relatorio final reprovado pela validacao por IA apos uma correcao.",
                    "scope_validation": scope_validation,
                    "report": saved_report,
                    "steps": steps,
                }
                register_ai_log_tool("Revisao abortada por reprovacao na validacao por IA.", error)
                yield {"event": "run_error", "type": "error", "step": step_number, "result": error}
                return

            saved_report = create_ai_report_tool("Revisao diaria de estoque", final_report)
            steps.append(
                {
                    "step": step_number,
                    "action_type": "final_message",
                    "message": final_content,
                }
            )
            register_ai_log_tool(
                "Relatorio final gerado pela IA.",
                {"report": saved_report, "summary": final_report.get("executive_summary")},
            )
            result = {
                "status": "completed",
                "message": "Revisao concluida.",
                "model": MODEL_STATE.selected_model,
                "ai_validation": scope_validation,
                "final_report": final_report,
                "report": saved_report,
                "steps": steps,
            }
            yield {
                "event": "final_report",
                "type": "response",
                "step": step_number,
                "message": "Relatorio final validado.",
                "final_report": final_report,
                "report": saved_report,
            }
            yield {"event": "run_completed", "type": "system", "step": step_number, "result": result}
            return

        result = {
            "status": "aborted",
            "message": f"Limite de {self.max_steps} passos atingido sem relatorio final.",
            "steps": steps,
        }
        register_ai_log_tool("Revisao abortada por limite de passos.", {"steps": steps})
        yield {"event": "run_error", "type": "error", "result": result}


def _parse_tool_args(raw_arguments: str | None) -> dict[str, Any] | str:
    if not raw_arguments:
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return raw_arguments
    return parsed if isinstance(parsed, dict) else raw_arguments


def _request_structured_final_report(
    llm_client: LocalLLMClient,
    messages: list[dict[str, Any]],
    *,
    fallback_content: str,
) -> dict[str, Any]:
    final_request = {
        "role": "user",
        "content": (
            "Materialize agora o relatorio final como JSON puro no schema DailyInventoryReviewReport. "
            "Use apenas dados observaveis vindos das tools no historico. "
            "Nao inclua markdown, comentarios, texto fora do objeto JSON, nem campos fora do schema. "
            "Todas as categorias obrigatorias devem existir; categorias fora do escopo solicitado devem ficar vazias."
        ),
    }
    structured_messages = [*messages, final_request]
    try:
        response = llm_client.chat_completion(
            structured_messages,
            response_format=_daily_inventory_review_response_format(),
            max_tokens=FINAL_REPORT_MAX_TOKENS,
        )
        content = response.choices[0].message.content
    except (LocalLLMError, IndexError, AttributeError) as exc:
        return _request_plain_json_final_report(
            llm_client,
            structured_messages,
            fallback_content=fallback_content,
            structured_error=str(exc),
        )

    if not content:
        return _request_plain_json_final_report(
            llm_client,
            structured_messages,
            fallback_content=fallback_content,
            structured_error="Structured output retornou conteudo vazio.",
        )

    return {
        "used": True,
        "fallback_used": False,
        "content": content,
        "history": [final_request, {"role": "assistant", "content": content}],
        "usage": _usage_from_response(response),
    }


def _request_plain_json_final_report(
    llm_client: LocalLLMClient,
    messages: list[dict[str, Any]],
    *,
    fallback_content: str,
    structured_error: str,
) -> dict[str, Any]:
    try:
        response = llm_client.chat_completion(messages, max_tokens=FINAL_REPORT_MAX_TOKENS)
        content = response.choices[0].message.content or fallback_content
        return {
            "used": False,
            "fallback_used": True,
            "error": structured_error,
            "content": content,
            "history": [{"role": "assistant", "content": content}] if content else [],
            "usage": _usage_from_response(response),
        }
    except (LocalLLMError, IndexError, AttributeError) as exc:
        return {
            "used": False,
            "fallback_used": True,
            "error": f"{structured_error}; fallback tambem falhou: {exc}",
            "content": fallback_content,
        }


def _daily_inventory_review_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "DailyInventoryReviewReport",
            "strict": True,
            "schema": _daily_inventory_review_json_schema(),
        },
    }


def _daily_inventory_review_json_schema() -> dict[str, Any]:
    nullable_int = {"type": ["integer", "null"]}
    nullable_string = {"type": ["string", "null"]}
    severity = {"type": "string", "enum": ["low", "medium", "high"]}

    product_item = {
        "type": "object",
        "properties": {
            "product_id": nullable_int,
            "sku": nullable_string,
            "product_name": nullable_string,
            "supplier_id": nullable_int,
            "severity": severity,
            "evidence": {"type": "string"},
            "recommended_action": {"type": "string"},
            "requires_approval": {"type": "boolean"},
        },
        "required": [
            "product_id",
            "sku",
            "product_name",
            "supplier_id",
            "severity",
            "evidence",
            "recommended_action",
            "requires_approval",
        ],
        "additionalProperties": False,
    }

    purchase_suggestion = {
        "type": "object",
        "properties": {
            **product_item["properties"],
            "suggested_quantity": {"type": ["number", "null"]},
        },
        "required": [*product_item["required"], "suggested_quantity"],
        "additionalProperties": False,
    }

    supplier_issue = {
        "type": "object",
        "properties": {
            "supplier_id": nullable_int,
            "supplier_name": nullable_string,
            "severity": severity,
            "evidence": {"type": "string"},
            "recommended_action": {"type": "string"},
            "requires_approval": {"type": "boolean"},
            "related_product_ids": {"type": "array", "items": {"type": "integer"}},
        },
        "required": [
            "supplier_id",
            "supplier_name",
            "severity",
            "evidence",
            "recommended_action",
            "requires_approval",
            "related_product_ids",
        ],
        "additionalProperties": False,
    }

    approval_action = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "severity": severity,
            "evidence": {"type": "string"},
            "approval_reason": {"type": "string"},
            "related_product_id": nullable_int,
            "supplier_id": nullable_int,
        },
        "required": [
            "action",
            "severity",
            "evidence",
            "approval_reason",
            "related_product_id",
            "supplier_id",
        ],
        "additionalProperties": False,
    }

    next_action = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "priority": severity,
            "owner": nullable_string,
            "evidence": {"type": "string"},
            "requires_approval": {"type": "boolean"},
        },
        "required": ["action", "priority", "owner", "evidence", "requires_approval"],
        "additionalProperties": False,
    }

    data_quality_issue = {
        "type": "object",
        "properties": {
            "issue_type": {"type": "string"},
            "severity": severity,
            "evidence": {"type": "string"},
            "recommended_action": {"type": "string"},
            "related_product_id": nullable_int,
            "supplier_id": nullable_int,
        },
        "required": [
            "issue_type",
            "severity",
            "evidence",
            "recommended_action",
            "related_product_id",
            "supplier_id",
        ],
        "additionalProperties": False,
    }

    return {
        "type": "object",
        "properties": {
            "report_type": {"type": "string", "enum": ["daily_inventory_review"]},
            "generated_at": {"type": "string"},
            "scope": {"type": "array", "items": {"type": "string"}},
            "executive_summary": {"type": "string"},
            "stock_shortages": {"type": "array", "items": product_item},
            "expiration_risks": {"type": "array", "items": product_item},
            "abnormal_consumption": {"type": "array", "items": product_item},
            "supplier_issues": {"type": "array", "items": supplier_issue},
            "purchase_suggestions": {"type": "array", "items": purchase_suggestion},
            "actions_requiring_approval": {"type": "array", "items": approval_action},
            "next_actions": {"type": "array", "items": next_action},
            "data_quality_issues": {"type": "array", "items": data_quality_issue},
        },
        "required": [
            "report_type",
            "generated_at",
            "scope",
            "executive_summary",
            "stock_shortages",
            "expiration_risks",
            "abnormal_consumption",
            "supplier_issues",
            "purchase_suggestions",
            "actions_requiring_approval",
            "next_actions",
            "data_quality_issues",
        ],
        "additionalProperties": False,
    }


def _validate_final_report(content: str) -> dict[str, Any]:
    if not content.strip():
        return {"ok": False, "error": "Resposta final vazia."}

    try:
        parsed = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"JSON invalido: {exc}"}

    if not isinstance(parsed, dict):
        return {"ok": False, "error": "Relatorio final precisa ser um objeto JSON."}

    try:
        report = DailyInventoryReviewReport.model_validate(parsed)
    except ValidationError as exc:
        return {"ok": False, "error": str(exc)}

    return {"ok": True, "report": report.model_dump(mode="json")}


def _validate_report_deterministically(db: Session, report: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    today = datetime.utcnow().date()
    products = db.scalars(select(models.Product)).all()
    suppliers = db.scalars(select(models.Supplier)).all()
    product_by_id = {product.id: product for product in products}
    supplier_by_id = {supplier.id: supplier for supplier in suppliers}
    scoped_sections = set(report.get("scope") or [])
    product_sections = [
        "stock_shortages",
        "expiration_risks",
        "abnormal_consumption",
        "purchase_suggestions",
    ]
    list_sections = [
        *product_sections,
        "supplier_issues",
        "actions_requiring_approval",
        "next_actions",
        "data_quality_issues",
    ]

    for section in list_sections:
        if section not in scoped_sections and report.get(section):
            errors.append(f"A secao {section} contem itens, mas nao aparece em scope.")

    for section in product_sections:
        for index, item in enumerate(report.get(section) or []):
            product = _validate_product_item(
                item,
                section=section,
                index=index,
                product_by_id=product_by_id,
                errors=errors,
                warnings=warnings,
            )
            if not product:
                continue
            if section == "stock_shortages" and product.current_stock >= product.minimum_stock:
                errors.append(
                    f"stock_shortages[{index}] indica falta, mas o estoque atual "
                    f"{product.current_stock} nao esta abaixo do minimo {product.minimum_stock}."
                )
            if section == "expiration_risks":
                if not product.expiration_date:
                    errors.append(f"expiration_risks[{index}] indica validade proxima sem data cadastrada.")
                elif not (today <= product.expiration_date <= today + timedelta(days=30)):
                    errors.append(
                        f"expiration_risks[{index}] indica validade proxima, mas a data cadastrada "
                        f"e {product.expiration_date.isoformat()}."
                    )

    shortage_product_ids = {
        item.get("product_id")
        for item in report.get("stock_shortages") or []
        if item.get("product_id") is not None
    }
    expiration_product_ids = {
        item.get("product_id")
        for item in report.get("expiration_risks") or []
        if item.get("product_id") is not None
    }
    risky_keywords = ("compr", "descart", "bloque", "contato formal", "aprova")

    for section in product_sections:
        for index, item in enumerate(report.get(section) or []):
            action_text = str(item.get("recommended_action") or "").lower()
            if any(keyword in action_text for keyword in risky_keywords) and not item.get("requires_approval"):
                errors.append(
                    f"{section}[{index}] recomenda acao arriscada sem requires_approval=true."
                )

    for index, item in enumerate(report.get("purchase_suggestions") or []):
        product_id = item.get("product_id")
        suggested_quantity = item.get("suggested_quantity")
        product = product_by_id.get(product_id)
        if suggested_quantity is not None and not isinstance(suggested_quantity, (int, float)):
            errors.append(f"purchase_suggestions[{index}].suggested_quantity precisa ser numerico ou null.")
        if product and product.current_stock < product.ideal_stock and suggested_quantity is None:
            errors.append(
                f"purchase_suggestions[{index}].suggested_quantity precisa ser numerico; "
                f"estoque atual {product.current_stock}, ideal {product.ideal_stock}."
            )
        if product_id in expiration_product_ids and product_id not in shortage_product_ids:
            errors.append(
                f"purchase_suggestions[{index}] sugere compra para produto com risco de validade "
                "sem tambem haver falta de estoque."
            )
        if not item.get("requires_approval"):
            errors.append(f"purchase_suggestions[{index}] precisa exigir aprovacao humana.")

    approval_product_ids = {
        item.get("related_product_id")
        for item in report.get("actions_requiring_approval") or []
        if item.get("related_product_id") is not None
    }
    for section in product_sections:
        for index, item in enumerate(report.get(section) or []):
            product_id = item.get("product_id")
            if item.get("requires_approval") and product_id is not None and product_id not in approval_product_ids:
                warnings.append(
                    f"{section}[{index}] exige aprovacao, mas nao ha action_requiring_approval vinculada."
                )

    for index, item in enumerate(report.get("supplier_issues") or []):
        supplier_id = item.get("supplier_id")
        supplier = supplier_by_id.get(supplier_id) if supplier_id is not None else None
        if supplier_id is not None and supplier is None:
            errors.append(f"supplier_issues[{index}].supplier_id inexistente: {supplier_id}.")
        if supplier and item.get("supplier_name") is not None and item.get("supplier_name") != supplier.name:
            errors.append(
                f"supplier_issues[{index}].supplier_name nao bate com supplier_id {supplier_id}: "
                f"{item.get('supplier_name')} != {supplier.name}."
            )
        for product_id in item.get("related_product_ids") or []:
            if product_id not in product_by_id:
                errors.append(f"supplier_issues[{index}].related_product_ids contem product_id inexistente: {product_id}.")
        action_text = str(item.get("recommended_action") or "").lower()
        if any(keyword in action_text for keyword in risky_keywords) and not item.get("requires_approval"):
            errors.append(
                f"supplier_issues[{index}] recomenda acao arriscada sem requires_approval=true."
            )

    for index, item in enumerate(report.get("actions_requiring_approval") or []):
        product_id = item.get("related_product_id")
        if product_id is not None and product_id not in product_by_id:
            errors.append(f"actions_requiring_approval[{index}].related_product_id inexistente: {product_id}.")

    for index, item in enumerate(report.get("data_quality_issues") or []):
        product_id = item.get("related_product_id")
        if product_id is not None and product_id not in product_by_id:
            errors.append(f"data_quality_issues[{index}].related_product_id inexistente: {product_id}.")

    return {"ok": not errors, "errors": errors, "warnings": warnings}


def _validate_product_item(
    item: dict[str, Any],
    *,
    section: str,
    index: int,
    product_by_id: dict[int, models.Product],
    errors: list[str],
    warnings: list[str],
) -> models.Product | None:
    product_id = item.get("product_id")
    if product_id is None:
        warnings.append(f"{section}[{index}] nao informou product_id; validacao de identidade ficou limitada.")
        return None

    product = product_by_id.get(product_id)
    if product is None:
        errors.append(f"{section}[{index}].product_id inexistente no contexto preparado: {product_id}.")
        return None

    if item.get("sku") is not None and item.get("sku") != product.sku:
        errors.append(
            f"{section}[{index}].sku nao bate com product_id {product_id}: "
            f"{item.get('sku')} != {product.sku}."
        )
    if item.get("product_name") is not None and item.get("product_name") != product.name:
        errors.append(
            f"{section}[{index}].product_name nao bate com product_id {product_id}: "
            f"{item.get('product_name')} != {product.name}."
        )
    if item.get("supplier_id") is not None and item.get("supplier_id") != product.supplier_id:
        errors.append(
            f"{section}[{index}].supplier_id nao bate com product_id {product_id}: "
            f"{item.get('supplier_id')} != {product.supplier_id}."
        )
    return product


def _validate_report_against_request_with_ai(
    llm_client: LocalLLMClient,
    *,
    original_request: str,
    final_report: dict[str, Any],
) -> dict[str, Any]:
    messages = [
        {
            "role": "system",
            "content": (
                "Voce e um validador de QA. Compare o pedido original do usuario com o relatorio entregue. "
                "Avalie apenas se o relatorio respeita o escopo solicitado e se nao deixou de cobrir pontos "
                "explicitamente pedidos. Nao invente requisitos novos. O relatorio usa schema fixo, entao "
                "categorias obrigatorias vazias nao sao violacao de escopo; somente itens preenchidos fora do "
                "escopo ou ausencias reais devem reprovar. Responda apenas JSON valido com os campos: "
                "passed boolean, reason string, violations array de strings, correction_instruction string."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "original_request": original_request,
                    "final_report": final_report,
                },
                ensure_ascii=False,
                default=str,
            ),
        },
    ]
    try:
        raw_response = llm_client.chat(messages, max_tokens=512)
    except LocalLLMError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        parsed = json.loads(_extract_json(raw_response))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"Resposta invalida do validador de IA: {exc}",
            "raw_response": raw_response,
        }

    try:
        validation = AIReportScopeValidation.model_validate(parsed)
    except ValidationError as exc:
        return {
            "ok": False,
            "error": f"Schema invalido na validacao por IA: {exc}",
            "raw_response": raw_response,
        }

    return {"ok": True, "validation": validation.model_dump(mode="json")}


def _validation_message(ai_validation: dict[str, Any]) -> str:
    if not ai_validation.get("ok"):
        return f"Falha na validacao por IA: {ai_validation.get('error')}"
    validation = ai_validation["validation"]
    status = "aprovado" if validation["passed"] else "reprovado"
    return f"Relatorio {status} pela validacao por IA: {validation['reason']}"


def _extract_json(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        return stripped[start : end + 1]
    return stripped


def _compact_tool_result_for_model(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    if result.get("ok") is False:
        return {
            "ok": False,
            "tool_name": result.get("tool_name"),
            "error": result.get("error"),
        }

    tool_name = result.get("tool_name")
    payload = result.get("result")
    if not isinstance(payload, dict):
        return result

    if tool_name == "list_products_tool":
        return {
            "tool_name": tool_name,
            "count": payload.get("count"),
            "products": [
                {
                    "id": product.get("id"),
                    "sku": product.get("sku"),
                    "name": product.get("name"),
                    "current_stock": product.get("current_stock"),
                    "minimum_stock": product.get("minimum_stock"),
                    "ideal_stock": product.get("ideal_stock"),
                    "criticality": product.get("criticality"),
                    "expiration_date": product.get("expiration_date"),
                    "supplier_id": product.get("supplier_id"),
                }
                for product in payload.get("products", [])
                if isinstance(product, dict)
            ],
        }

    if tool_name == "list_open_alerts_tool":
        return {
            "tool_name": tool_name,
            "count": payload.get("count"),
            "alerts": [
                {
                    "id": alert.get("id"),
                    "product_id": alert.get("product_id"),
                    "alert_type": alert.get("alert_type"),
                    "severity": alert.get("severity"),
                    "title": alert.get("title"),
                    "description": alert.get("description"),
                    "data": alert.get("data"),
                }
                for alert in payload.get("alerts", [])
                if isinstance(alert, dict)
            ],
        }

    if tool_name == "run_stock_check_tool":
        return {
            "tool_name": tool_name,
            "checked_products": payload.get("checked_products"),
            "alerts_created": payload.get("alerts_created"),
            "alerts": payload.get("alerts", []),
        }

    if tool_name == "get_product_movements_tool":
        return {
            "tool_name": tool_name,
            "product": payload.get("product"),
            "days": payload.get("days"),
            "count": payload.get("count"),
            "movements": (payload.get("movements") or [])[:20],
        }

    if tool_name == "get_supplier_tool":
        return {"tool_name": tool_name, **payload}

    return {"tool_name": tool_name, "result": payload}


def _usage_from_response(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def _summarize_for_trace(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= 2000:
        return value
    return {"truncated": True, "preview": text[:2000]}


def _visible_message_summary(content: str | None, tool_calls: list[Any]) -> str:
    if tool_calls:
        names = [tool_call.function.name for tool_call in tool_calls]
        return "Modelo solicitou tools: " + ", ".join(names)
    if content:
        compact = " ".join(content.split())
        return compact[:240] + ("..." if len(compact) > 240 else "")
    return "Modelo respondeu sem texto visivel."
