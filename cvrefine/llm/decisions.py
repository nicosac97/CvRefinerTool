"""Decision layer: typed yes/no, choice and score answers with probabilities.

Jev is the default backend. Its endpoint is still alpha on OpenRouter, so everything
else in the app depends only on `DecisionClient`, and `ChatDecisionClient` can stand in.
"""

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from cvrefine.config import settings

Questions = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class NoulResult:
    probability: float


@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    confidence: float
    probabilities: dict[str, float]


@dataclass(frozen=True)
class ScoreResult:
    score: float
    confidence: float
    probabilities: dict[int, float]
    levels: int


Result = NoulResult | ChoiceResult | ScoreResult


class DecisionError(RuntimeError):
    pass


class DecisionClient(Protocol):
    def decide(self, state: str, questions: Questions) -> dict[str, Result]: ...


def noul(instructions: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions}


def choice(instructions: str, criteria: dict[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, criteria: list[str]) -> dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": criteria}


class JevClient:
    def __init__(self) -> None:
        from typesafe_sdk import TypeSafeClient

        self._client = TypeSafeClient(api_key=settings.openrouter_api_key, base_url=settings.openrouter_base_url)

    def decide(self, state: str, questions: Questions) -> dict[str, Result]:
        try:
            response = self._client.system_one(model=settings.decision_model, state=state, questions=questions)
        except Exception as error:
            raise DecisionError(str(error)) from error
        results: dict[str, Result] = {}
        for name, answer in response.answers.items():
            if answer.type == "noul":
                results[name] = NoulResult(answer.noul)
            elif answer.type == "choice":
                results[name] = ChoiceResult(answer.choice, answer.confidence, dict(answer.probabilities))
            else:
                results[name] = ScoreResult(answer.score, answer.confidence, dict(answer.probabilities), len(answer.legend))
        return results


_CHAT_SYSTEM = """You answer decision questions about the given state. Reply with one JSON object keyed by question name.
- noul question: {"probability": <0..1 probability the answer is yes>}
- choice question: {"probabilities": {<label>: <0..1>, ...}} over exactly the given labels, summing to 1
- score question: {"probabilities": [<0..1 per level, in the given order>]} summing to 1
JSON only, no prose."""


class ChatDecisionClient:
    """Same contract through a chat model. Probabilities are self-reported, so less reliable than Jev's."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=60)

    def decide(self, state: str, questions: Questions) -> dict[str, Result]:
        payload = {
            "model": settings.writer_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _CHAT_SYSTEM},
                {"role": "user", "content": f"STATE:\n{state}\n\nQUESTIONS:\n{json.dumps(questions)}"},
            ],
        }
        try:
            response = self._http.post(
                f"{settings.openrouter_base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            raw = json.loads(response.json()["choices"][0]["message"]["content"])
            return {name: _parse_chat_answer(question, raw[name]) for name, question in questions.items()}
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise DecisionError(f"chat decision backend: {error}") from error


def _parse_chat_answer(question: dict[str, Any], raw: dict[str, Any]) -> Result:
    if question["type"] == "noul":
        return NoulResult(_clamp(raw["probability"]))
    if question["type"] == "choice":
        probabilities = _normalize({label: _clamp(raw["probabilities"].get(label, 0)) for label in question["criteria"]})
        best = max(probabilities, key=probabilities.__getitem__)
        return ChoiceResult(best, probabilities[best], probabilities)
    values = [_clamp(value) for value in raw["probabilities"]]
    if len(values) != len(question["criteria"]):
        raise ValueError("score probabilities do not match the number of levels")
    probabilities = _normalize(dict(enumerate(values)))
    expected = sum(level * p for level, p in probabilities.items())
    return ScoreResult(expected, max(probabilities.values()), probabilities, len(values))


def _clamp(value: Any) -> float:
    return min(1.0, max(0.0, float(value)))


def _normalize(probabilities: dict[Any, float]) -> dict[Any, float]:
    total = sum(probabilities.values())
    if total <= 0:
        raise ValueError("probabilities sum to zero")
    return {key: value / total for key, value in probabilities.items()}


def get_decision_client() -> DecisionClient:
    if not settings.openrouter_api_key:
        raise DecisionError("CVREFINE_OPENROUTER_API_KEY is not set (put it in .env)")
    return JevClient() if settings.decision_backend == "jev" else ChatDecisionClient()
