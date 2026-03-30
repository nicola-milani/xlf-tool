"""
Ollama HTTP client for local LLM translation.
Server lifecycle (start/stop/download) is handled by ollama_manager.py.
"""
import re
import requests
from typing import List


class OllamaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", model: str = "llama3.2"):
        self.base_url = base_url.rstrip("/")
        self.model    = model

    def list_models(self) -> List[str]:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return []

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate *text* and return only the translated string."""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a professional translator. "
                        "Output ONLY the translated text — no explanations, no notes, no extra words. "
                        "Preserve all special characters, placeholders, variables, and XML tags exactly. "
                        "Preserve the original capitalisation: if a word was uppercase keep it uppercase, if lowercase keep it lowercase. "
                        "Never convert digits to words: '10' must remain '10', not 'ten' or 'dieci'. "
                        "Preserve numbered lists exactly: if the source starts with '1.' or '1)' keep that prefix in the translation."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Translate from {source_lang} to {target_lang}:\n\n{text}",
                },
            ],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        resp = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()

    def translate_batch(
        self,
        texts: List[str],
        source_lang: str,
        target_lang: str,
    ) -> List[str]:
        """
        Translate a list of spans belonging to the same unit (same sentence/paragraph).
        Sends them as a single numbered-list prompt so the LLM has full context.
        Returns a list of the same length.
        Falls back to individual translate() calls if the response cannot be parsed.
        """
        if len(texts) == 1:
            return [self.translate(texts[0], source_lang, target_lang)]

        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        prompt = (
            f"Translate from {source_lang} to {target_lang}. "
            "These are styled text spans from the same sentence or paragraph — "
            "use them as context for each other.\n"
            "Return ONLY the translations as a numbered list in the same order, "
            "one per line, format: '1. text'\n\n"
            f"{numbered}"
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a professional translator. "
                        "Output ONLY the translated numbered list — no explanations, no extra words. "
                        "Preserve the original capitalisation: if a word was uppercase keep it uppercase, if lowercase keep it lowercase. "
                        "Never convert digits to words: '10' must remain '10', not 'ten' or 'dieci'. "
                        "Preserve numbered lists exactly: if the source starts with '1.' or '1)' keep that prefix in the translation."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        resp = requests.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"].strip()

        results = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^\d+\.\s*(.*)", line)
            if m:
                results.append(m.group(1).strip())

        if len(results) == len(texts):
            return results

        # Fallback: translate each span individually
        return [self.translate(t, source_lang, target_lang) for t in texts]
