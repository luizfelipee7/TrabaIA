from __future__ import annotations

import json
import mimetypes
import os
import tempfile
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


STT_ENGINE = os.getenv("STT_ENGINE", "browser").strip().lower()
STT_PROXY_URL = os.getenv("STT_BASE_URL", "http://127.0.0.1:8010")
STT_MODEL = os.getenv("STT_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2")
STT_DEVICE = os.getenv("STT_DEVICE", "auto").strip().lower()
STT_COMPUTE_TYPE = os.getenv("STT_COMPUTE_TYPE", "auto").strip().lower()
STT_LANGUAGE = os.getenv("STT_LANGUAGE", "").strip() or None
STT_VAD_FILTER = os.getenv("STT_VAD_FILTER", "false").strip().lower() in {"1", "true", "yes", "on"}
STT_ALLOWED_EXTENSIONS = {
    ext.strip().lower()
    for ext in os.getenv("STT_ALLOWED_EXTENSIONS", "mp3,wav,m4a,webm,ogg,flac,mp4,mpga,mpeg,aac,wma").split(",")
    if ext.strip()
}

_WHISPER_MODEL: Any | None = None
_WHISPER_ERROR: str | None = None


def status() -> dict[str, Any]:
    if STT_ENGINE == "browser":
        return {
            "available": True,
            "engine": STT_ENGINE,
            "message": "STT principal usa reconhecimento nativo do navegador; backend pesado nao e carregado.",
        }
    if STT_ENGINE == "disabled":
        return {
            "available": False,
            "engine": STT_ENGINE,
            "message": "STT desabilitado por configuracao.",
        }
    if STT_ENGINE == "proxy":
        return _proxy_status()
    if STT_ENGINE != "embedded":
        return {
            "available": False,
            "engine": STT_ENGINE,
            "message": "STT_ENGINE invalido. Use browser, proxy, embedded ou disabled.",
        }

    if _WHISPER_ERROR:
        return {
            "available": False,
            "engine": "embedded",
            "model": STT_MODEL,
            "message": _WHISPER_ERROR,
        }

    try:
        _import_faster_whisper()
    except Exception as exc:
        return {
            "available": False,
            "engine": "embedded",
            "model": STT_MODEL,
            "message": f"faster-whisper indisponivel: {type(exc).__name__}: {exc}",
        }

    return {
        "available": True,
        "engine": "embedded",
        "model": STT_MODEL,
        "device": _resolved_device(),
        "compute_type": _resolved_compute_type(),
        "message": "STT embutido pronto; modelo sera carregado sob demanda.",
    }


def transcribe(filename: str, content: bytes, content_type: str | None = None) -> dict[str, Any]:
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension and extension not in STT_ALLOWED_EXTENSIONS:
        return {
            "status": "invalid_audio",
            "message": f"Extensao .{extension} nao permitida para STT.",
            "allowed_extensions": sorted(STT_ALLOWED_EXTENSIONS),
        }

    if STT_ENGINE == "proxy":
        return _proxy_transcribe(filename, content, content_type)
    if STT_ENGINE == "browser":
        return {
            "status": "browser_stt_only",
            "engine": "browser",
            "message": "Use o microfone em tempo real no navegador ou configure STT_ENGINE=proxy/embedded para transcrever arquivos enviados.",
        }
    if STT_ENGINE == "disabled":
        return {"status": "stt_unavailable", "message": "STT desabilitado por configuracao."}

    current = status()
    if not current["available"]:
        return {"status": "stt_unavailable", "message": current["message"], "engine": STT_ENGINE}

    try:
        model = _load_model()
        suffix = Path(filename).suffix or ".webm"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp:
            temp.write(content)
            temp_path = temp.name

        segments, info = model.transcribe(
            temp_path,
            language=STT_LANGUAGE,
            vad_filter=STT_VAD_FILTER,
            beam_size=5,
        )
        transcript = []
        segment_rows = []
        for segment in segments:
            text = segment.text.strip()
            if text:
                transcript.append(text)
            segment_rows.append(
                {
                    "start": round(float(segment.start), 2),
                    "end": round(float(segment.end), 2),
                    "text": text,
                }
            )
        return {
            "status": "completed",
            "engine": "embedded",
            "text": " ".join(transcript).strip(),
            "language": getattr(info, "language", None),
            "language_probability": round(float(getattr(info, "language_probability", 0) or 0), 2),
            "segments": segment_rows,
        }
    except Exception as exc:
        return {"status": "stt_failed", "engine": "embedded", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        if "temp_path" in locals():
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass


def _load_model() -> Any:
    global _WHISPER_MODEL, _WHISPER_ERROR
    if _WHISPER_MODEL is not None:
        return _WHISPER_MODEL
    try:
        WhisperModel = _import_faster_whisper()
        _WHISPER_MODEL = WhisperModel(
            STT_MODEL,
            device=_resolved_device(),
            compute_type=_resolved_compute_type(),
        )
        _WHISPER_ERROR = None
        return _WHISPER_MODEL
    except Exception as exc:
        _WHISPER_ERROR = f"Falha ao carregar STT embutido: {type(exc).__name__}: {exc}"
        raise


def _import_faster_whisper() -> Any:
    from faster_whisper import WhisperModel

    return WhisperModel


def _resolved_device() -> str:
    if STT_DEVICE in {"cpu", "cuda", "auto"}:
        return "cuda" if STT_DEVICE == "auto" and os.getenv("USE_CUDA") == "1" else ("cpu" if STT_DEVICE == "auto" else STT_DEVICE)
    return "cpu"


def _resolved_compute_type() -> str:
    if STT_COMPUTE_TYPE != "auto":
        return STT_COMPUTE_TYPE
    return "float16" if _resolved_device() == "cuda" else "int8"


def _proxy_status() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{STT_PROXY_URL.rstrip('/')}/health", timeout=1.5) as response:
            body = json.loads(response.read().decode("utf-8"))
            return {"available": True, "engine": "proxy", "base_url": STT_PROXY_URL, "message": "STT proxy disponivel.", "data": body}
    except Exception as exc:
        return {
            "available": False,
            "engine": "proxy",
            "base_url": STT_PROXY_URL,
            "message": f"STT proxy indisponivel em {STT_PROXY_URL}: {type(exc).__name__}",
        }


def _proxy_transcribe(filename: str, content: bytes, content_type: str | None) -> dict[str, Any]:
    current = _proxy_status()
    if not current["available"]:
        return {"status": "stt_unavailable", "message": current["message"], "engine": "proxy"}

    boundary = f"----codex{uuid.uuid4().hex}"
    mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            f'Content-Disposition: form-data; name="file"; filename="{Path(filename).name}"\r\n'.encode("utf-8"),
            f"Content-Type: {mime}\r\n\r\n".encode("utf-8"),
            content,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    request = urllib.request.Request(
        f"{STT_PROXY_URL.rstrip('/')}/transcribe",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return {
                "status": "completed",
                "engine": "proxy",
                "text": payload.get("text", ""),
                "language": payload.get("language"),
                "language_probability": payload.get("language_probability"),
                "segments": payload.get("segments") or [],
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"status": "stt_failed", "engine": "proxy", "message": detail}
    except Exception as exc:
        return {"status": "stt_failed", "engine": "proxy", "message": f"{type(exc).__name__}: {exc}"}
