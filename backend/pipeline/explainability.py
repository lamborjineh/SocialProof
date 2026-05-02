"""
SocialProof — Module 9: Explainability Engine  (v3.1)
Generates human-readable explanations for credibility scores.

v3.1 Changes:
  - SYSTEM_PROMPT updated: MIL-aware framing for high school / general public.
    Ollama now explains WHY something is credible or misleading in plain
    language, not just that it is. It names specific red flags or trust signals.
  - generate_mil_tip(): new classmethod that returns a short standalone
    teaching tip (1-2 sentences) the frontend can render as a callout card.
    Falls back to a rule-based tip when Ollama is unavailable.

Setup Ollama (optional):
  1. Install : https://ollama.com
  2. Pull    : ollama pull llama3.2
  3. Serve   : ollama serve
"""

import json
import urllib.request
from typing import List, Dict, Optional, Tuple

from config import OLLAMA_URL, OLLAMA_MODEL, logger


class ExplainabilityEngine:
    """
    Rule-based explainability engine.
    Every sentence maps to a measurable signal from the pipeline,
    satisfying the thesis panel requirement for non-black-box decisions.
    """

    @classmethod
    def explain(
        cls,
        score:             int,
        label:             str,
        source_result:     Dict,
        bias_result:       Dict,
        evidence_items:    List[Dict],
        claim_results:     List[Dict],
        sub_scores:        Dict,
        is_partial:        bool = False,
        unverified_claims: List[str] = None,
    ) -> str:
        unverified_claims = unverified_claims or []
        parts: List[str] = []

        # ── Lead sentence ─────────────────────────────────────────────────────
        if is_partial:
            parts.append(
                "This analysis is incomplete — no evidence was found in the corpus "
                "for any of the detected claims. "
                "The score below is based on source credibility and language analysis only, "
                "and should not be treated as a final verdict."
            )
            if unverified_claims:
                claim_list = "; ".join(f'"{c[:80]}"' for c in unverified_claims[:3])
                parts.append(
                    f"The following claim(s) could not be verified: {claim_list}. "
                    "These require manual verification through trusted sources."
                )
        elif label == "Likely Credible":
            parts.append(
                f"This content scored {score}/100, indicating it is likely credible "
                "based on the signals analysed."
            )
        elif label == "Likely Misleading":
            parts.append(
                f"This content scored {score}/100, indicating it is likely misleading "
                "or contains unsupported claims."
            )
        else:
            parts.append(
                f"This content scored {score}/100. The analysis returned an uncertain verdict "
                "because signals were mixed or evidence was limited."
            )

        # ── Source credibility ────────────────────────────────────────────────
        src_label = source_result.get("label", "unknown")
        src_score = int(source_result.get("score", 0) * 100)
        parts.append(
            f"Source credibility is rated {src_label} ({src_score}/100). "
            + source_result.get("reason", "")
        )

        # ── Bias analysis ─────────────────────────────────────────────────────
        bias_label = bias_result.get("label", "unknown")
        bias_score = int(bias_result.get("score", 0) * 100)
        if bias_score > 50:
            parts.append(
                f"Language analysis detected {bias_label} bias (score {bias_score}/100), "
                "which may affect how information is presented."
            )
        else:
            parts.append(
                f"Language appears relatively neutral ({bias_label}, score {bias_score}/100)."
            )

        # ── Evidence & claims ─────────────────────────────────────────────────
        if not is_partial and evidence_items:
            support_count    = sum(1 for e in evidence_items if e["type"] == "support")
            contradict_count = sum(1 for e in evidence_items if e["type"] == "contradict")
            parts.append(
                f"{len(evidence_items)} evidence item(s) retrieved: "
                f"{support_count} supporting, {contradict_count} contradicting."
            )

        verified   = sum(1 for c in claim_results if c.get("label") == "supported")
        misleading = sum(1 for c in claim_results if c.get("label") == "misleading")
        unverified = sum(1 for c in claim_results if c.get("label") == "unverified")
        if claim_results:
            parts.append(
                f"Of {len(claim_results)} claim(s) detected: "
                f"{verified} supported, {misleading} contradicted by evidence, "
                f"{unverified} could not be verified."
            )

        return " ".join(parts)

    @classmethod
    def mil_tip(
        cls,
        score:          int,
        label:          str,
        bias_result:    Dict,
        source_result:  Dict,
        claim_results:  List[Dict],
        evidence_items: List[Dict] = None,
        is_partial:     bool = False,
    ) -> str:
        """
        Rule-based MIL tip fallback.
        Returns a single short teaching point (1-2 sentences) for the frontend
        to render as a callout card. Targets high school / general public level.

        Uses actual claim text and evidence text where available so the tip
        is grounded in what was specifically found, not just aggregate statistics.
        """
        evidence_items   = evidence_items or []
        misleading_count = sum(1 for c in claim_results if c.get("label") == "misleading")
        unverified_count = sum(1 for c in claim_results if c.get("label") == "unverified")
        bias_score       = bias_result.get("score", 0)
        src_score        = source_result.get("score", 0)

        # Find the most concrete contradicted claim + evidence pair to name specifically
        contradicted_pair = None
        for claim in claim_results:
            if claim.get("label") == "misleading":
                matching_ev = [
                    e for e in evidence_items
                    if e.get("claim_text", "")[:80] == claim["text"][:80]
                    and e.get("type") == "contradict"
                ]
                if matching_ev:
                    contradicted_pair = (claim["text"], matching_ev[0])
                    break

        if is_partial:
            return (
                "💡 Tip: When a claim can't be verified, that doesn't mean it's false — "
                "it means there isn't enough evidence yet. Before sharing, try searching "
                "for the same claim on a trusted fact-checking site like Vera Files or tsek.ph."
            )
        if misleading_count > 0:
            if contradicted_pair:
                claim_snippet = contradicted_pair[0][:80].rstrip()
                ev_source     = contradicted_pair[1].get("source_label", "a credible source")
                return (
                    f"🚩 Red flag: The claim \"{claim_snippet}\" was contradicted by "
                    f"{ev_source}. Misleading content often mixes real facts with false "
                    "details — that's what makes it easy to believe and hard to spot."
                )
            return (
                "🚩 Red flag: At least one claim here was contradicted by credible evidence. "
                "Misleading content often mixes real facts with false details — "
                "that's what makes it easy to believe and hard to spot."
            )
        if bias_score > 0.6:
            bias_phrases = bias_result.get("flagged_phrases", [])
            phrase_note  = (
                f" Phrases like \"{bias_phrases[0]}\" are a signal."
                if bias_phrases else ""
            )
            return (
                f"⚠️ Watch the language: This content uses emotionally charged or one-sided "
                f"wording.{phrase_note} Biased language isn't always wrong, but it can make "
                "something sound more certain than it really is — look for other sources that "
                "cover the same story."
            )
        if src_score < 0.5:
            return (
                "🔍 Check the source: This came from a source with low credibility. "
                "Before trusting or sharing it, search for who published it and whether "
                "established outlets like BBC, Rappler, or Inquirer are reporting the same thing."
            )
        if unverified_count > 0:
            return (
                "💡 Tip: Some claims here couldn't be matched to verified evidence. "
                "That's a good reminder to look for official sources — government agencies, "
                "researchers, or established newsrooms — before treating something as fact."
            )
        if label == "Likely Credible":
            return (
                "✅ Good sign: The claims here align with evidence from credible sources. "
                "Still, no system is perfect — for important decisions, always check "
                "the original source directly."
            )
        return (
            "💡 Tip: Next time you see something that surprises you online, ask three things: "
            "Who wrote this? What evidence do they give? Is anyone else reporting the same? "
            "These three questions catch most misinformation before it spreads."
        )


class OllamaExplainer:
    """
    LLM-based explanation via local Ollama. Falls back to rule-based on failure.
    explain_with_source() returns a (explanation_text, source_label) tuple
    so callers can report whether Ollama or the rule engine generated the text.
    """

    # v3.1: MIL-aware system prompt for high school / general public level.
    # Instructs Ollama to name specific red flags or trust signals, explain
    # WHY something is credible or misleading in plain everyday language,
    # and keep it short enough for a social media user to read in 20 seconds.
    SYSTEM_PROMPT = (
        "You are a media literacy assistant helping everyday social media users — "
        "including high school students — understand why a piece of content is "
        "credible or misleading. "
        "Write a clear explanation in 3-4 sentences using plain, conversational language. "
        "Do not use jargon. Do not say 'the content' repeatedly — vary your phrasing. "
        "Go beyond just stating the score: name specific signals. "
        "For example, if the source is low-credibility, say WHY that matters. "
        "If claims are contradicted by evidence, explain what that means in real terms. "
        "If language is biased, give a concrete example of what biased language looks like. "
        "Be factual, honest, and educational. Do not add information not present in the data."
    )

    # v3.1: Separate prompt for generating the MIL tip card.
    MIL_TIP_PROMPT = (
        "You are a media literacy teacher. Based on the analysis data below, "
        "write ONE short teaching tip (1-2 sentences) that a high school student "
        "or social media user can immediately apply the next time they see similar content. "
        "Start with an emoji that fits the tone (e.g. 💡 for neutral tip, 🚩 for warning, "
        "✅ for positive reinforcement). "
        "Be specific — do not write generic advice like 'always check your sources'. "
        "Make it feel like advice from a friend who knows about this stuff, not a lecture. "
        "Return only the tip sentence(s), no preamble, no explanation of what you're doing."
    )

    @classmethod
    def explain_with_source(
        cls,
        score:             int,
        label:             str,
        source_result:     Dict,
        bias_result:       Dict,
        evidence_items:    List[Dict],
        claim_results:     List[Dict],
        sub_scores:        Dict,
        is_partial:        bool = False,
        unverified_claims: List[str] = None,
    ) -> Tuple[str, str]:
        """
        Returns (explanation_text, source_label).
        source_label is "ollama" on success or "rule_based" on fallback.
        """
        unverified_claims = unverified_claims or []
        prompt_data = {
            "score":              score,
            "label":              label,
            "is_partial":         is_partial,
            "unverified_claims":  unverified_claims[:3],
            "source_credibility": source_result.get("label"),
            "source_reason":      source_result.get("reason", ""),
            "bias":               bias_result.get("label"),
            "bias_score":         round(bias_result.get("score", 0), 2),
            "evidence_count":     len(evidence_items),
            "support_count":      sum(1 for e in evidence_items if e["type"] == "support"),
            "contradict_count":   sum(1 for e in evidence_items if e["type"] == "contradict"),
            "claims_supported":   sum(1 for c in claim_results if c.get("label") == "supported"),
            "claims_misleading":  sum(1 for c in claim_results if c.get("label") == "misleading"),
            "claims_unverified":  sum(1 for c in claim_results if c.get("label") == "unverified"),
            "sub_scores":         sub_scores,
        }

        user_message = (
            "Generate a credibility explanation for the following analysis:\n"
            f"{json.dumps(prompt_data, indent=2)}"
        )

        try:
            payload = json.dumps({
                "model":  OLLAMA_MODEL,
                "prompt": f"{cls.SYSTEM_PROMPT}\n\n{user_message}",
                "stream": False,
            }).encode()

            req = urllib.request.Request(
                OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:  # LLM is slow — raised from 10s
                data = json.loads(resp.read().decode())
                text = data.get("response", "").strip()
                if text:
                    logger.info("[Explainability] Ollama explanation generated.")
                    return text, "ollama"
        except Exception as e:
            logger.warning(f"[Explainability] Ollama unavailable: {e} — using rule-based fallback.")

        fallback = ExplainabilityEngine.explain(
            score=score,
            label=label,
            source_result=source_result,
            bias_result=bias_result,
            evidence_items=evidence_items,
            claim_results=claim_results,
            sub_scores=sub_scores,
            is_partial=is_partial,
            unverified_claims=unverified_claims,
        )
        return fallback, "rule_based"

    @classmethod
    def generate_mil_tip(
        cls,
        score:          int,
        label:          str,
        bias_result:    Dict,
        source_result:  Dict,
        claim_results:  List[Dict],
        evidence_items: List[Dict] = None,
        is_partial:     bool = False,
    ) -> Tuple[str, str]:
        """
        Generate a short MIL teaching tip grounded in the specific claim and
        evidence found in THIS analysis — not just aggregate statistics.

        Returns (tip_text, source_label) where source_label is
        "ollama" or "rule_based".
        """
        evidence_items = evidence_items or []

        # Find the most concrete contradicted claim + evidence pair.
        # The tip should name WHAT was wrong, not just THAT something was wrong.
        contradicted_example = None
        for claim in claim_results:
            if claim.get("label") == "misleading":
                matching_ev = [
                    e for e in evidence_items
                    if e.get("claim_text", "")[:80] == claim["text"][:80]
                    and e.get("type") == "contradict"
                ]
                if matching_ev:
                    contradicted_example = {
                        "claim":    claim["text"][:120],
                        "evidence": matching_ev[0]["evidence_text"][:120],
                        "source":   matching_ev[0].get("source_label", "a credible source"),
                    }
                    break

        prompt_data = {
            "score":             score,
            "label":             label,
            "is_partial":        is_partial,
            "bias_label":        bias_result.get("label"),
            "bias_score":        round(bias_result.get("score", 0), 2),
            "bias_phrases":      bias_result.get("flagged_phrases", [])[:3],
            "source_label":      source_result.get("label"),
            "source_score":      round(source_result.get("score", 0), 2),
            "claims_misleading": sum(1 for c in claim_results if c.get("label") == "misleading"),
            "claims_unverified": sum(1 for c in claim_results if c.get("label") == "unverified"),
            "claims_supported":  sum(1 for c in claim_results if c.get("label") == "supported"),
        }
        if contradicted_example:
            prompt_data["contradicted_example"] = contradicted_example

        user_message = (
            "Generate a media literacy tip for the following analysis.\n"
            "If a contradicted_example is present, your tip MUST reference the specific "
            "claim text and name the source that contradicted it — do not write a generic tip.\n\n"
            f"{json.dumps(prompt_data, indent=2)}"
        )

        try:
            payload = json.dumps({
                "model":  OLLAMA_MODEL,
                "prompt": f"{cls.MIL_TIP_PROMPT}\n\n{user_message}",
                "stream": False,
            }).encode()

            req = urllib.request.Request(
                OLLAMA_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:  # LLM is slow — raised from 8s
                data = json.loads(resp.read().decode())
                tip  = data.get("response", "").strip()
                if tip:
                    logger.info("[Explainability] Ollama MIL tip generated.")
                    return tip, "ollama"
        except Exception as e:
            logger.warning(f"[Explainability] Ollama MIL tip failed: {e} — using rule-based.")

        fallback_tip = ExplainabilityEngine.mil_tip(
            score=score,
            label=label,
            bias_result=bias_result,
            source_result=source_result,
            claim_results=claim_results,
            evidence_items=evidence_items,
            is_partial=is_partial,
        )
        return fallback_tip, "rule_based"

    @classmethod
    def explain(cls, **kwargs) -> str:
        """Convenience wrapper — returns text only (drops source label)."""
        text, _ = cls.explain_with_source(**kwargs)
        return text
