"""
Cerberus Flywheel — SFT / DPO JSONL Exporter
==============================================
Takes high-scoring (prompt, response, judge_score) records from Postgres
and formats them into JSONL suitable for:
  - Supervised Fine-Tuning (SFT): {"messages": [...]}
  - Direct Preference Optimization (DPO): {"prompt", "chosen", "rejected"}

Also implements "DPO pair mining from cache near-misses" (feature #15 in
the blueprint): when two semantically similar prompts produced responses
with meaningfully different judge scores, they become a natural
chosen/rejected preference pair.
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Sanitization — strip PII patterns, control characters, and anything that
# looks like a leaked system prompt or injection artifact before export.
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

INJECTION_MARKERS = (
    "ignore previous instructions",
    "disregard the system prompt",
    "reveal your system prompt",
)


def sanitize_text(text: str) -> str:
    text = _CTRL_CHAR_RE.sub("", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    return text.strip()


def contains_injection_artifact(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in INJECTION_MARKERS)


# ---------------------------------------------------------------------------
# Record types
# ---------------------------------------------------------------------------

@dataclass
class ScoredExample:
    response_id: str
    prompt: str
    response: str
    overall_score: float           # 0.0 - 1.0 from judge_scores table
    system_prompt: Optional[str] = None
    embedding_cluster_id: Optional[str] = None  # for near-miss pair mining


@dataclass
class ExportConfig:
    sft_min_score: float = 0.85
    dpo_score_gap_min: float = 0.25   # min score delta to count as a valid pref pair
    max_examples: Optional[int] = None


# ---------------------------------------------------------------------------
# SFT export
# ---------------------------------------------------------------------------

def export_sft_jsonl(examples: Iterable[ScoredExample], config: ExportConfig) -> Iterable[str]:
    """
    Yields JSONL lines in OpenAI/Anthropic-style chat SFT format:
        {"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", ...}]}
    """
    count = 0
    for ex in examples:
        if ex.overall_score < config.sft_min_score:
            continue
        prompt_clean = sanitize_text(ex.prompt)
        response_clean = sanitize_text(ex.response)
        if contains_injection_artifact(prompt_clean):
            continue  # never let injection-tainted examples into training data

        messages = []
        if ex.system_prompt:
            messages.append({"role": "system", "content": sanitize_text(ex.system_prompt)})
        messages.append({"role": "user", "content": prompt_clean})
        messages.append({"role": "assistant", "content": response_clean})

        record = {
            "messages": messages,
            "metadata": {
                "response_id": ex.response_id,
                "judge_score": ex.overall_score,
            },
        }
        yield json.dumps(record, ensure_ascii=False)
        count += 1
        if config.max_examples and count >= config.max_examples:
            return


# ---------------------------------------------------------------------------
# DPO export — including near-miss preference pair mining
# ---------------------------------------------------------------------------

def mine_dpo_pairs_from_clusters(
    examples: list[ScoredExample],
    config: ExportConfig,
) -> Iterable[tuple[ScoredExample, ScoredExample]]:
    """
    Groups examples by embedding_cluster_id (i.e. responses whose prompts
    were semantically near each other in the vector cache — this is exactly
    the 'near-miss' set the cache orchestrator surfaces) and yields
    (chosen, rejected) pairs where the score gap clears the configured
    threshold. This turns cache near-misses into free preference data.
    """
    clusters: dict[str, list[ScoredExample]] = {}
    for ex in examples:
        if not ex.embedding_cluster_id:
            continue
        clusters.setdefault(ex.embedding_cluster_id, []).append(ex)

    for cluster_id, members in clusters.items():
        members.sort(key=lambda e: e.overall_score, reverse=True)
        if len(members) < 2:
            continue
        best, worst = members[0], members[-1]
        if best.overall_score - worst.overall_score >= config.dpo_score_gap_min:
            yield best, worst


def export_dpo_jsonl(
    examples: list[ScoredExample],
    config: ExportConfig,
) -> Iterable[str]:
    """
    Yields JSONL lines in standard DPO format:
        {"prompt": ..., "chosen": ..., "rejected": ...}
    """
    count = 0
    for chosen, rejected in mine_dpo_pairs_from_clusters(examples, config):
        # Use the higher-scoring example's prompt as canonical (they're
        # near-duplicates by construction of the embedding cluster).
        prompt_clean = sanitize_text(chosen.prompt)
        if contains_injection_artifact(prompt_clean):
            continue

        record = {
            "prompt": prompt_clean,
            "chosen": sanitize_text(chosen.response),
            "rejected": sanitize_text(rejected.response),
            "metadata": {
                "chosen_response_id": chosen.response_id,
                "rejected_response_id": rejected.response_id,
                "score_gap": chosen.overall_score - rejected.overall_score,
            },
        }
        yield json.dumps(record, ensure_ascii=False)
        count += 1
        if config.max_examples and count >= config.max_examples:
            return


# ---------------------------------------------------------------------------
# Demo / smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample = [
        ScoredExample("r1", "How do I reverse a linked list in Python?",
                       "Use iterative pointer swapping: prev, curr = None, head...",
                       0.95, embedding_cluster_id="cluster_ll_reverse"),
        ScoredExample("r2", "How to reverse a linked list in python?",
                       "idk just Google it lol",
                       0.30, embedding_cluster_id="cluster_ll_reverse"),
        ScoredExample("r3", "What's the capital of Japan?", "Tokyo is the capital of Japan.", 0.99),
    ]
    cfg = ExportConfig(sft_min_score=0.9, dpo_score_gap_min=0.2)

    print("--- SFT export ---")
    for line in export_sft_jsonl(sample, cfg):
        print(line)

    print("--- DPO export (mined from near-miss cluster) ---")
    for line in export_dpo_jsonl(sample, cfg):
        print(line)
