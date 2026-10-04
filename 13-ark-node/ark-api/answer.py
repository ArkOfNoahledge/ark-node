#!/usr/bin/env python3
"""
answer.py - the grounded answer, and the cross-check. Standard library only.

    python 13-ark-node/ark-api/answer.py "how much chlorine to disinfect water"
    python 13-ark-node/ark-api/answer.py --selftest

THE ONE RULE THIS FILE EXISTS TO ENFORCE. Spec §11.1: a 4-bit 27B model "will
state incorrect specifics with complete confidence." §9.3: prefer the archive
directly for factual lookup, and use the model for reasoning and synthesis. So the
model here is not a source. It is a reader. It gets the passages the retrieval
layer found, it is required to cite them, and an answer it produces without
citations is labelled MODEL ONLY rather than presented as an answer.

That labelling is the whole design. It is easy to build a RAG surface where an
ungrounded answer and a grounded one look identical, and it is the single most
harmful thing this node could do.

THREE GROUNDING STATES, and the surface must render them differently:

    grounded     at least one valid citation into the retrieved passages
    uncovered    the model said the archive does not cover this. A GOOD outcome:
                 §11.2 says retrieval quality bounds the answer, and the archive
                 admitting a gap beats it inventing a filler
    model_only   an answer with no citation. Shown with a warning, never plainly

THE CROSS-CHECK COMPARES FIGURES, NOT PROSE, and that is a deliberate limit.
§9.4 wants two model families asked the same question so disagreement becomes a
signal. Comparing two free-text answers for semantic agreement needs a third model
and a judgement call. Comparing the DOSES, VOLTAGES AND PRESSURES they each state
needs neither, uses the detector already built for §9.5, and is the class §9.2
actually cares about: nobody is harmed by two models phrasing a definition
differently, and someone can be harmed by 500 mg against 250 mg.

    What it catches:  two answers stating different figures in the same category.
    What it misses:   subtle semantic disagreement with no numbers in it.

That miss is stated here rather than discovered later. A cross-check that claimed
to compare meaning would be making a claim it cannot support.
"""

import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import llm                                                     # noqa: E402
import safety                                                  # noqa: E402

MAX_PASSAGES = 5

# PASSAGES ARE NOT TRUNCATED IN PRACTICE, AND THE CAP EXISTS ONLY FOR OUTLIERS.
# This was 1400, chosen to keep the prompt small, and on 2026-09-05 it silently
# deleted the answer. The Hesperian chlorine table sits at character 1375 of a
# 2045-character chunk, so the model was handed "Table (WATER BLEACH). For 1 lit"
# and nothing more, and correctly reported that the passages gave no dose. Before
# the table fix the flattened figures happened to fall inside 1400 characters;
# the cut had always been there and only became visible when it removed the thing
# being looked for.
#
# The chunker already bounds a chunk at 512 BGE-M3 tokens, roughly 2,000 to 2,400
# characters, so THE CHUNK IS THE LIMIT and this cap should never fire. Five
# passages at that size is about 3,000 tokens against an 8,192-token context, and
# the primary reads them at 1,170 t/s - under three seconds. There was nothing to
# save.
PASSAGE_CHARS = 4000

# ---------------------------------------------------------------------------
# 1. THE PROMPT
#
# A SOURCE OF KNOWLEDGE THAT CHECKS ITSELF AGAINST THE ARCHIVE, SINCE 2026-09-27.
#
# Until that day the prompt opened "You answer strictly from the numbered
# passages provided. You are a reader of an offline archive, not a source of
# knowledge", and rule 6 said "Be brief and direct". Juan's decision reverses the
# first sentence and replaces the second: "I want this to be a source of
# knowledge indeed, one that verifies against the archives for additional
# expansion as needed." The trigger was plain: answers on the surface felt short
# and less capable than the same models asked directly, and they were - Qwen
# stopped at about 683 tokens on rule 6, not on its budget, and the cited answer
# could only say what five passages say.
#
# WHAT CHANGED AND WHAT DID NOT. The model is now told it is a knowledgeable
# expert and asked for complete answers in both parts. What did NOT change is
# the structure that keeps it honest, and that is deliberate: the cited part
# still uses only the passages (rule 1), still carries every figure verbatim
# (rule 4), still says NOT IN ARCHIVE when the passages do not answer (rule 3),
# and the model's own knowledge still arrives only inside the marked block, after
# the cited part, with no citations (rule 3b). Every parser, grounding state,
# safety trigger and screen label downstream depends on that shape, so this is
# step A of DESIGN-knowledge-first.md: the voice and length change now; the order
# (knowledge first, archive as verifier) is step B and needs a verifier that has
# been measured first.
#
# The old comment here said a persona "invites the model to be helpful when the
# honest answer is 'the archive does not say'". That risk is real and is why
# rule 3 survives unchanged: the admission is still required, it is just no
# longer the whole answer.
#
# THE READER PROMPT IS KEPT, NOT DELETED, so the two can be measured against
# each other on the same node: ARK_ANSWER_PROMPT=reader restores it without an
# edit. It is also the rollback.
# ---------------------------------------------------------------------------

SYSTEM_READER = """You answer strictly from the numbered passages provided. You are a reader of an offline archive, not a source of knowledge.

RULES
1. For any claim you present as fact, use ONLY the passages. Do not mix your own knowledge into a cited answer. This rule governs the CITED answer only. The rule 3b block is not a cited answer, rule 1 does not apply inside it, and rule 4 does not either.
2. Cite every claim inline as [1], [2], matching the passage numbers. A sentence with no citation must not contain a fact.
3. If the passages do not answer the question, say exactly: NOT IN ARCHIVE
   followed by one sentence saying what is missing.
3b. ALWAYS end your answer with a block that begins on its own line with exactly:
   UNSOURCED - NOT FROM THE ARCHIVE
   It comes last, after the cited answer or after the rule 3 admission, so the
   reader has seen what the archive says before reading anything unverified.
   In it, answer the question AGAIN from your own training alone, as if no
   passage had been given to you. Be specific. Give the figures you would give
   if asked with no archive at all.
   IF IT CONTRADICTS THE CITED ANSWER, SAY SO IN THE FIRST SENTENCE and name the
   passage. Disagreement between the archive and your training is the most
   important thing you can tell the reader; do not soften it or bury it.
   If it agrees, say that it agrees. If you do not know, say you do not know -
   that is also information. Do not omit the block.
   Put no [1] style citations inside it - there is nothing to cite.
4. Inside a cited answer, never state a number, dose, voltage, pressure or measurement that is not written in a passage. Copy figures exactly, with their units. A figure in the unsourced block is allowed and is understood to be unverified.
5. Answer in the same language as the question.
6. Be brief and direct. No preamble, no summary of the question, no offer to help further."""

SYSTEM_KNOWLEDGE = """You are a knowledgeable expert on an offline node that also holds a large reference archive. Answer the question the way a capable expert would: completely, clearly and usefully. The numbered passages come from the archive. Use them to verify your answer, to extend it with what they contain, and to show the reader where each fact can be checked.

Your answer has two parts, always in this order: first what the archive supports, with citations; then your own full answer from your knowledge.

RULES
1. The first part is the cited answer. In it, use ONLY the passages for any claim you present as fact, and do not mix your own knowledge in. Use them fully: when they give steps, figures, conditions, warnings or context that bear on the question, include them rather than summarising in one line. This rule governs the cited answer only. The rule 3b block is not a cited answer, and rules 1 and 4 do not apply inside it.
2. Cite every claim in the cited answer inline as [1], [2], matching the passage numbers. A sentence with no citation must not contain a fact.
3. If the passages do not answer the question, the cited answer is exactly: NOT IN ARCHIVE
   followed by one sentence saying what is missing. Your full answer then comes in the rule 3b block.
3b. ALWAYS end your answer with a block that begins on its own line with exactly:
   UNSOURCED - NOT FROM THE ARCHIVE
   It comes last, after the cited answer or after the rule 3 admission, so the
   reader has seen what the archive says before reading anything unverified.
   Its FIRST SENTENCE says how your own knowledge relates to the cited answer:
   that it agrees, what it adds, or that it CONTRADICTS it. If it contradicts,
   say so plainly in that first sentence and name the passage. Disagreement
   between the archive and your training is the most important thing you can
   tell the reader; do not soften it or bury it.
   Then answer the question fully from your own knowledge, as you would if no
   passage had been given to you: the explanation, the steps in order, the
   figures with their units, the practical detail and the cautions an expert
   would give. Say where you are unsure or where practice varies. If you do not
   know, say so - that is also information. Do not omit the block.
   Put no [1] style citations inside it - there is nothing to cite.
4. Inside the cited answer, never state a number, dose, voltage, pressure or measurement that is not written in a passage. Copy figures exactly, with their units. A figure in the rule 3b block is allowed and is understood to be unverified.
5. Answer in the same language as the question.
6. Write for someone who has to act on this with no other reference. Match the length to the question: a simple fact gets a short answer; a procedure, a diagnosis, a repair or an explanation gets the full treatment, organised in short paragraphs or numbered steps. No preamble, no restating the question, no offer to help further."""

PROMPT_NAME = os.environ.get("ARK_ANSWER_PROMPT", "knowledge").strip().lower()
if PROMPT_NAME not in ("knowledge", "reader"):
    PROMPT_NAME = "knowledge"
SYSTEM = SYSTEM_READER if PROMPT_NAME == "reader" else SYSTEM_KNOWLEDGE

NOT_IN_ARCHIVE = "NOT IN ARCHIVE"

# THE MARKER IS STRUCTURAL, NOT DECORATIVE.
#
# WHY UNSOURCED ANSWERS EXIST AT ALL. Until 2026-09-09 rule 1 forbade them
# outright and rule 3 required NOT IN ARCHIVE and nothing more. Juan's decision
# reversed that: this is a civilization bootstrap and an emergency node, and
# refusing to say anything the archive does not hold "is limiting a universe of
# potential directionally correct answers". The arguments that position
# overrides stay true - §11.2 names the confident, well-formed, wrongly-detailed
# answer as this system's characteristic failure - so the point is not that
# unsourced content is safe. It is that A REFUSAL IS NOT FREE EITHER, and an
# operator with no other reference gets nothing.
#
# SO THE ADMISSION COMES FIRST, ALWAYS. The block cannot be reached without the
# model having said NOT IN ARCHIVE, which means the operator reads "the archive
# does not cover this" BEFORE reading anything unverified. That ordering is the
# safety property; the label is only how it is shown. An answer that opens
# straight into its own knowledge with no admission is still `model_only` and
# still a fault, because sanctioning the announced case must not sanction the
# unannounced one.
UNSOURCED_MARK = "UNSOURCED - NOT FROM THE ARCHIVE"


def build_messages(question, results):
    """The passages, numbered, and the question. Numbering is 1-based because the
    citations are read by a person as well as by the parser below."""
    parts = []
    for i, r in enumerate(results[:MAX_PASSAGES], 1):
        c = r.get("citation") or {}
        head = "[%d] %s" % (i, c.get("title") or "untitled")
        src = c.get("artifact") or c.get("shelf") or ""
        if src:
            head += "  (%s)" % src
        full = " ".join((r.get("text") or "").split())
        txt = full[:PASSAGE_CHARS]
        # AND IF IT EVER DOES FIRE, IT SAYS SO. A passage that ends mid-sentence
        # with no mark is indistinguishable from a passage that simply does not
        # cover the question, which is exactly the confusion that cost this
        # afternoon.
        if len(full) > PASSAGE_CHARS:
            txt += " ... [passage truncated at %d characters]" % PASSAGE_CHARS
        parts.append("%s\n%s" % (head, txt))
    body = "\n\n".join(parts) if parts else "(no passages were retrieved)"
    user = "PASSAGES\n\n%s\n\nQUESTION\n%s" % (body, question)
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# 2. READING THE ANSWER BACK
# ---------------------------------------------------------------------------

CITE_RE = re.compile(r"\[(\d{1,2})\]")


def citations_used(text, n_passages):
    """Which passages the answer actually cited, in order of first appearance.

    OUT-OF-RANGE CITATIONS ARE DROPPED AND COUNTED, NOT SILENTLY IGNORED. A model
    citing [7] when five passages were supplied has invented a source, and an
    interface that quietly rendered that as a valid chip would be manufacturing
    provenance - the exact opposite of what the citation path is for."""
    seen, order, bogus = set(), [], []
    for m in CITE_RE.finditer(text or ""):
        k = int(m.group(1))
        if 1 <= k <= n_passages:
            if k not in seen:
                seen.add(k)
                order.append(k)
        else:
            bogus.append(k)
    return order, bogus


def split_unsourced(text):
    """(the part that must be grounded, the unsourced part or None)."""
    t = text or ""
    i = t.upper().find(UNSOURCED_MARK)
    if i < 0:
        return t, None
    return t[:i], t[i + len(UNSOURCED_MARK):]


def grounding(text, n_passages):
    """grounded / uncovered / unsourced / model_only, plus the citation list.

    FOUR STATES, AND `model_only` IS STILL A FAULT. `unsourced` is the announced
    case: the model said NOT IN ARCHIVE and then offered its own knowledge in the
    marked block. `model_only` is the unannounced one - no citations, no
    admission - and it stays a violation with its own warning on the surface.
    The difference between them is entirely whether the operator was told.

    CITATIONS ARE COUNTED ONLY IN THE GROUNDED PART. A [1] inside the unsourced
    block is a rule 3b violation: it borrows the authority of a passage for a
    claim the passage does not make, which is worse than an uncited sentence.
    Counted and returned rather than stripped, because a violation that is
    silently cleaned up is a violation nobody fixes."""
    t = (text or "").strip()
    head, unsrc = split_unsourced(t)
    said_nia = NOT_IN_ARCHIVE in head.upper()[:200]
    used, bogus = citations_used(head, n_passages)

    if unsrc is not None:
        if said_nia:
            return "unsourced", used, bogus
        if used:
            # GROUNDED, AND THE MODEL ANSWERED AGAIN FROM ITS OWN TRAINING.
            # 2026-09-10, Juan's decision, second pass: the block is not
            # reserved for answers the archive cannot cover AND IT IS NOT
            # CONDITIONAL. Rule 3b asks for it every time, so this is the
            # ORDINARY state for a grounded answer and bare `grounded` is the
            # exception - a model that produced no block at all.
            #
            # The first version gated it on the model judging that it "adds or
            # contradicts". Measured the same evening: it fired on `who is
            # Albert Einstein` and refused twice on `dose of advil for 5 years
            # old kid`, where the cited answer was a formula the reader could
            # not apply. Two rounds of prompt loosening did not move it. A
            # discretionary block is one the model declines exactly where it
            # would matter most, so the discretion is removed rather than
            # argued with. The test that made the original design safe was
            # never "was there an admission" - it was WHETHER THE BLOCK STANDS
            # ALONE. A block after citations is exactly as accountable as a
            # block after an admission, because in both cases the reader has
            # already been shown what the archive says before reading anything
            # unverified. A block after NEITHER is still a fault, below.
            return "grounded_plus", used, bogus
        # the block with no admission and nothing cited: it stands alone
        return "model_only", used, bogus
    if said_nia:
        return "uncovered", used, bogus
    if used:
        return "grounded", used, bogus
    return "model_only", used, bogus


def unsourced_citations(text, n_passages):
    """How many [n] markers appear inside the unsourced block. Should be zero."""
    _, unsrc = split_unsourced(text or "")
    if not unsrc:
        return 0
    return len(CITE_RE.findall(unsrc))


# MAX_TOKENS COUNTS THINKING AS WELL AS THE ANSWER.
# The primary runs with --reasoning-budget 512, and llama-server bills reasoning
# against the same max_tokens as the content. At 600 the chlorine answer was cut
# off at "50 gallons" - the model had spent roughly 350 tokens thinking and had
# 250 left for a four-row table. The budget here is therefore the reasoning
# allowance PLUS the answer, not the answer alone.
#
# 1024 at 22 t/s is about 46 seconds worst case, and most answers finish far
# short of it because generation stops at the end of the answer, not at the cap.
# The cost of setting this too low is an answer that stops in the middle of a
# dose, which is worse than a slow one.
REASONING_ALLOWANCE = 512


# BUDGET 2048, AND IT IS HEADROOM RATHER THAN AN INSTRUCTION.
# Raised 2026-09-11 on Juan's call: "just in case a larger response is needed".
# It is NOT expected to change answer length and the system prompt is unchanged
# on purpose - measured the same day, Qwen finished a five-passage answer at 683
# tokens of its previous 1,024, so it stops on rule 6's brevity, not on the cap.
# What this buys is the one case that was silently losing content: a long answer
# plus a reasoning trace, where the trace counts against n_predict and the reader
# never sees it. Gemma hitting its limit mid-dosing-table is what that failure
# looks like, and the primary had no guard against it either.
#
# COSTS, MEASURED RATHER THAN ASSUMED. No memory: n_predict does not touch the
# KV cache, which is fixed at launch by `-c 8192`. Time: at ~22 t/s the extra
# 1,024 tokens is about 46s worst case against a 240s timeout. Context: five
# passages at PASSAGE_CHARS is roughly 5,000 tokens of 8,192, so 2,048 of output
# fits with room - and if that ever stops being true the server truncates and
# `truncated` says so on screen, which is the guard that matters.
# THE BUDGET IS NOW SIZED FROM THE PROMPT, 2026-09-27. Longer answers are the
# point of the knowledge-first prompt, and a fixed 2,048 was chosen when answers
# stopped at ~683. The ceiling is the context, not the wish: the primary runs
# with `-c 8192`, and VRAM headroom (245 MiB) does not allow a larger context -
# Qwen's KV costs ~512 MiB per 8,192 cells. So the output gets what the prompt
# leaves, capped at ANSWER_BUDGET_MAX and never below ANSWER_BUDGET_MIN, which
# is the fixed 2,048 this replaced: the knowledge prompt must never leave an
# answer less room than the reader prompt had.
#
# THE ESTIMATE LEANS PESSIMISTIC: 3.3 characters per token, where English runs
# nearer 4 and Spanish nearer 3.3. Over-estimating the prompt costs a few
# hundred tokens of headroom; under-estimating it overflows the context,
# which llama-server reports as an error or a cut answer. `prompt_tokens` in the
# result is the measured figure, and `truncated` says so on screen if the cap is
# ever reached - the guard that matters.
CONTEXT_TOKENS = 8192
ANSWER_BUDGET_MAX = 3072
ANSWER_BUDGET_MIN = 2048
CONTEXT_MARGIN = 256


def output_budget(msgs):
    chars = sum(len(m.get("content") or "") for m in msgs)
    est = int(chars / 3.3) + 64
    room = CONTEXT_TOKENS - est - CONTEXT_MARGIN
    return max(ANSWER_BUDGET_MIN, min(ANSWER_BUDGET_MAX, room))


def answer(question, results, n_predict=None, temperature=0.2):
    """One grounded answer from the primary model.

    Raises llm.LLMUnavailable if the model is not running. The caller must NOT
    swallow that: a node whose answer pane silently goes quiet when the model is
    down, while the source pane keeps working, teaches the operator that the
    absence of an answer means the archive has nothing - which is a different and
    much worse statement."""
    msgs = build_messages(question, results)
    if n_predict is None:
        n_predict = output_budget(msgs)
    r = llm.chat("primary", msgs, n_predict=n_predict, temperature=temperature)
    n = min(len(results), MAX_PASSAGES)
    state, used, bogus = grounding(r["text"], n)
    # BOTH BLOCK-BEARING STATES. `cross_check` is unconditional now, so this
    # flag only shapes the verify_prompt wording - but "verify against the
    # archive" is still the wrong sentence next to a figure the archive does
    # not contain, whichever state produced it.
    sf = safety.assess(r["text"], query=question,
                       unsourced=(state in ("unsourced", "grounded_plus")))
    return {
        "text": r["text"],
        "reasoning": r["reasoning"],
        "grounding": state,
        "cited": used,
        "invented_citations": bogus,
        # RULE 3b SAYS THE BLOCK CARRIES NO CITATIONS, SO A COUNT ABOVE ZERO IS
        # A VIOLATION AND IS REPORTED RATHER THAN CLEANED UP. Borrowing a
        # passage's number for a claim the passage does not make is worse than
        # an uncited sentence: it manufactures provenance, which is the one
        # thing this layer exists to prevent.
        "unsourced_citations": unsourced_citations(r["text"], n),
        # NAMED, so the surface can say "Qwen's own answer" rather than "the
        # unsourced answer". Two models write into this pane and an operator
        # reading a block should know which one wrote it.
        "model": r.get("model"),
        "passages_offered": n,
        "truncated": r["truncated"],
        "seconds": r["seconds"],
        "tokens_per_second": r["tokens_per_second"],
        "completion_tokens": r["completion_tokens"],
        "prompt_tokens": r["prompt_tokens"],
        # WHICH PROMPT AND WHAT BUDGET, so a measurement of answer length can
        # say what it measured (ARK_ANSWER_PROMPT, output_budget).
        "prompt": PROMPT_NAME,
        "n_predict": n_predict,
        "safety": {"level": sf["level"], "categories": sf["categories"],
                   "findings": sf["findings"],
                   "render_text_only": sf["render_text_only"],
                   "cross_check": sf["cross_check"],
                   "verify_prompt": sf["verify_prompt"]},
    }


# ---------------------------------------------------------------------------
# 3. THE CROSS-CHECK
# ---------------------------------------------------------------------------

def _fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("""the a an and or of to in on for with is are was were be been it its
this that these those as at by from not no if then than there their they you your
we our i he she his her which what when where how why can could should would may
might will shall do does did done have has had el la los las un una y o de del que
en con por para es son ser sido su sus lo al se""".split())


def _content_words(text):
    return {w for w in _WORD.findall(_fold(text)) if w not in _STOP and len(w) > 2}


_NUMPART = re.compile(r"^([\d.,\-\s]*\d)\s*(.*)$")


# TWO MODELS SAYING THE SAME THING IN DIFFERENT WORDS MUST NOT READ AS SILENCE.
# Measured on the node 2026-09-10, both answers to a child's ibuprofen dose:
#     Qwen   "commonly around 10 mg/kg per dose"   -> ('dose','mg/kg'): {'10'}
#     Gemma  "10 mg per kilogram (kg) of body weight" -> ('dose','mg'): {'10'}
# No shared key, so `compare()` reported "no figures in common" about two models
# that agreed exactly. AND THE FAILURE IS SYMMETRIC: had Qwen said 10 mg/kg and
# Gemma a flat 10 mg - clinically a different instruction entirely - the output
# would have been identical. A comparison defeated by spelling is not a
# comparison, and this one is the only check an unsourced figure ever gets.
#
# Applied to the COMPARISON's copy of the text only. safety.scan() itself is
# untouched, because its spans drive the surface's highlighting and shifting
# them would move the marks off the words they mark.
_UNIT_SPELLINGS = [
    (r"\bper\s+kilogram(?:\s+of\s+body\s+weight)?\b", "/kg"),
    (r"\bper\s+kilo\b", "/kg"),
    (r"\bper\s+kg\b", "/kg"),
    (r"\bper\s+day\b", "/day"),
    (r"\bdaily\b", "/day"),
    (r"\bper\s+dose\b", ""),
    (r"\bper\s+litre\b|\bper\s+liter\b", "/l"),
    (r"\bper\s+millilitre\b|\bper\s+milliliter\b|\bper\s+ml\b", "/ml"),
    (r"\bmilligrams?\b", "mg"),
    (r"\bgrams?\b", "g"),
    (r"\bmicrograms?\b", "mcg"),
]


def _spell_units(text):
    """`10 mg per kilogram` -> `10 mg/kg`, so the same figure keys the same way."""
    t = text or ""
    for pat, rep in _UNIT_SPELLINGS:
        t = re.sub(pat, rep, t, flags=re.I)
    # "10 mg /kg" and "10 mg/ kg" both become "10 mg/kg"
    return re.sub(r"\s*/\s*", "/", t)


def _figures(text):
    """The safety-relevant figures an answer states, keyed by CATEGORY AND UNIT.

    Only hard and lifted findings count. A soft hint is not a claim, and treating
    one as a disagreement would make the cross-check fire on prose.

    KEYED BY UNIT, NOT BY CATEGORY, AND THE SELFTEST IS WHY.
    The first version bucketed by category alone and compared the sets. Given
    "500 mg every 8 hours" against "250 mg every 8 hours" both buckets held
    {"500mg","every8hours"} and {"250mg","every8hours"}; the intersection was
    non-empty because the FREQUENCIES matched, so the function reported AGREE on
    two answers that differ by a factor of two in the dose.

    That is the worst possible direction for this function to fail in. A
    cross-check that misses a disagreement is worse than no cross-check, because
    the operator has been told the models agree. Comparing within (category, unit)
    means the milligrams are compared against the milligrams and the frequency
    against the frequency, and a matching frequency can no longer mask a differing
    dose."""
    out = {}
    for f in safety.scan(_spell_units(text or "")):
        if f["tier"] not in ("hard", "lifted"):
            continue
        t = _fold(f["text"]).strip()
        m = _NUMPART.match(t)
        if m:
            value = re.sub(r"[\s,]", "", m.group(1))
            unit = m.group(2).strip() or "-"
        else:
            value, unit = "", t.replace(" ", "")
        out.setdefault((f["category"], unit), set()).add(value)
    return out


def compare(primary_text, other_text):
    """agree / disagree / both_unsupported / no_basis, with the evidence.

    NOT A VERDICT. NODE-ARCHITECTURE §2 is explicit: present the state, do not
    average the answers, and do not say which model is right. This function has no
    way to know that and neither does any other part of this node."""
    # THE UNSOURCED CASE IS TESTED FIRST, BECAUSE THE OLD TEST SWALLOWED IT.
    # An unsourced answer must contain NOT IN ARCHIVE - the structural rule
    # requires the admission - so the two clauses below saw both answers
    # declining and returned `both_unsupported`, "the most reassuring result the
    # node can give", while comparing nothing about the claims. So: if either
    # side carries an unsourced block, compare THE BLOCKS.
    p_head, p_blk = split_unsourced(primary_text or "")
    o_head, o_blk = split_unsourced(other_text or "")
    p_unc0 = NOT_IN_ARCHIVE in p_head.upper()
    o_unc0 = NOT_IN_ARCHIVE in o_head.upper()
    # THE BLOCK BRANCH IS FOR ANSWERS BUILT ON A BLOCK, not for every answer
    # that has one. After 2026-09-10 a `grounded_plus` answer carries a block on
    # top of a cited claim, and comparing the blocks there would compare the two
    # footnotes and ignore the two answers - the same inversion this branch was
    # written to fix, arriving from the other side.
    #
    # AND "BUILT ON A BLOCK" MEANS THE SAME SIDE HAS BOTH, SINCE 2026-09-27. The
    # test was "either side has a block AND either side admitted", so a
    # `grounded_plus` primary (block, no admission) against a checker that read
    # the same passages and said NOT IN ARCHIVE (admission, no block) entered
    # this branch and came out `one_declined`, "one model answered from its own
    # training" - when the primary had answered from the archive, with
    # citations. Measured by `answer-ab.py` on the fuse question. That pair is
    # the plain "one found it in the passages, the other did not" below.
    if (p_blk is not None and p_unc0) or (o_blk is not None and o_unc0):
        if p_blk is None or o_blk is None:
            # One model was willing to answer from its own training and the
            # other was not. That is not agreement and it is not nothing: a
            # family declining to guess where the other guessed is a caution.
            return {"state": "one_declined", "why":
                    ("one model answered from its own training and the other "
                     "would not - nothing here is verified, and the two "
                     "families do not even agree that the question can be "
                     "answered without a source"), "figures": {}}
        pf, of = _figures(p_blk), _figures(o_blk)
        shared = set(pf) & set(of)
        detail, conflict = {}, False
        for key in sorted(shared):
            cat, unit = key
            a, b = pf[key], of[key]
            detail["%s (%s)" % (cat, unit)] = {
                "primary": sorted(a), "crosscheck": sorted(b),
                "match": bool(a & b)}
            if not (a & b):
                conflict = True
        if conflict:
            return {"state": "disagree_unsourced", "why":
                    ("both answers are unsourced AND they state different "
                     "figures. Neither is backed by a passage and they do not "
                     "agree with each other - treat both as wrong until an "
                     "outside source settles it"), "figures": detail}
        if shared:
            # AGREEMENT BETWEEN TWO RECALLS IS WEAK EVIDENCE AND MUST SAY SO.
            # Two model families can share a training corpus and therefore share
            # a misconception. This is the one place in the node where "the two
            # models agree" does NOT mean what it means everywhere else, and
            # wording it like the grounded case would be the most consequential
            # dishonesty this file could commit.
            return {"state": "agree_unsourced", "why":
                    ("two model families recall the same figures - but neither "
                     "is from the archive, and models trained on overlapping "
                     "text can share the same error. This is weaker than one "
                     "cited passage"), "figures": detail}
        return {"state": "no_basis_unsourced", "why":
                ("both answers are unsourced and state no figures in common, so "
                 "there is nothing to compare"), "figures": {}}

    p_unc = NOT_IN_ARCHIVE in p_head.upper()
    o_unc = NOT_IN_ARCHIVE in o_head.upper()
    if p_unc and o_unc:
        # §11.1's most dangerous case is two fluent models confidently wrong
        # together. This is its opposite and is the most reassuring result the
        # node can give: two families independently declining to invent one.
        return {"state": "both_unsupported", "why":
                "both models said the archive does not cover this", "figures": {}}
    if p_unc != o_unc:
        return {"state": "disagree", "why":
                "one model answered from the passages and the other said the "
                "archive does not cover this", "figures": {}}

    # HEADS, NOT WHOLE TEXTS. A figure inside an unsourced block must never be
    # compared against a cited figure as though the two were the same kind of
    # claim - that would let a `grounded_plus` block manufacture a disagreement,
    # or hide one.
    pf, of = _figures(p_head), _figures(o_head)
    shared = set(pf) & set(of)
    detail = {}
    conflict = False
    for key in sorted(shared):
        cat, unit = key
        a, b = pf[key], of[key]
        label = "%s (%s)" % (cat, unit)
        detail[label] = {"primary": sorted(a), "crosscheck": sorted(b),
                         "match": bool(a & b)}
        if not (a & b):
            conflict = True
    if conflict:
        return {"state": "disagree", "why":
                "the two models state different figures in the same category",
                "figures": detail}
    if shared:
        return {"state": "agree", "why":
                "the figures both models state match", "figures": detail}

    # No figures in common. Fall back to whether they are even about the same
    # thing. This is a WEAK signal and is labelled as one rather than dressed up.
    #
    # CONTAINMENT OF THE SHORTER CITED ANSWER, NOT JACCARD OVER THE WHOLE TEXT,
    # SINCE 2026-09-27. Jaccard divides by the union, so it falls as the primary
    # gets longer whatever the two say. The knowledge-first prompt tripled the
    # primary's length and `answer-ab.py` measured the result: five of six
    # answered cross-checks read DISAGREE at 8 to 15% overlap, on answers about
    # the same thing. Recomputed on those same answers, the share of the
    # checker's content words found in the primary's CITED part was 64 to 96%.
    # The rule-3b block is left out on both sides because the checker is given
    # the passages and never writes one: comparing it would compare the
    # primary's own recall against nothing.
    pw = _content_words(p_head.strip() or primary_text)
    ow = _content_words(o_head.strip() or other_text)
    if not pw or not ow:
        return {"state": "no_basis", "why": "one answer was empty", "figures": {}}
    small, big = (pw, ow) if len(pw) <= len(ow) else (ow, pw)
    j = len(small & big) / float(len(small))
    if j >= 0.50:
        return {"state": "agree", "why":
                "no figures to compare; %.0f%% of the shorter answer's words are "
                "in the other. This is a weak signal - it compares words, not meaning"
                % (j * 100), "figures": {}}
    return {"state": "disagree", "why":
            "no figures to compare; only %.0f%% of the shorter answer's words "
            "are in the other. This is a weak signal - read both" % (j * 100),
            "figures": {}}


RECALL_SYSTEM = """Answer from your own knowledge. There are no source documents.

RULES
1. Be specific. Give the figures, units and steps you believe are correct.
2. Say plainly where you are unsure or where practice varies.
3. Do not refuse for lack of a source - the absence of one is understood.
4. Answer in the same language as the question.
5. Be brief. No preamble."""


def build_recall_messages(question):
    """The second model asked for its OWN recall, with no passages at all.

    WHY THE UNSOURCED CROSS-CHECK CANNOT USE THE PASSAGES. `crosscheck` normally
    hands the second family the same passages on purpose, so a disagreement is
    about the models and not about retrieval. That reasoning inverts here: the
    unsourced path is reached precisely BECAUSE the passages do not answer the
    question, so handing them over guarantees the second model also finds nothing
    and says NOT IN ARCHIVE.

    And `compare` tests for that string. So the second model declining, for the
    same reason the first one did, would have produced `both_unsupported` - the
    state this file describes as "the most reassuring result the node can give" -
    while NOTHING had compared the actual claim. A checker that agrees about the
    admission and ignores the assertion is worse than no checker, because it
    reports confidence it has not earned. §9.4 wants the models compared; on this
    path that means comparing their RECALL."""
    return [{"role": "system", "content": RECALL_SYSTEM},
            {"role": "user", "content": question}]


# WHY THE SECOND MODEL GETS A DIFFERENT SYSTEM PROMPT, AND A BIGGER BUDGET.
#
# Measured on the node 2026-09-10: Gemma returned NO ANSWER AT ALL - 1,618
# characters of reasoning, then its 600-token limit, 241.61 seconds spent, empty
# text. It never began writing. Two things caused it and both are fixed here.
#
# FIRST, IT WAS BEING ASKED TO DO THE WRONG JOB. `build_messages` carries the
# full SYSTEM prompt, and rule 3b now requires EVERY answer to end with a second
# answer from the model's own training. The cross-check does not need that: its
# job is to check the CITED claim, and a block from Gemma is one more unsourced
# paragraph nothing can verify. So it gets a prompt with no rule 3b, and spends
# its budget on the thing being compared.
#
# SECOND, THE BUDGET WAS SIZED BEFORE ANY OF THIS. 600 tokens at the MEASURED
# 2.48 t/s - 600 tokens in 241.61s, not the 3 t/s this file used to claim - is
# four minutes, and a thinking model can spend all of it before the answer
# starts. 1,000 leaves room for a reasoning trace AND an answer.
#
# IF IT IS STILL TIGHT, the next lever is not a bigger number: it is stopping
# Gemma from thinking at all for this task, which is a llama-server launch flag
# and belongs in bin/ark.py rather than here.
CROSSCHECK_SYSTEM = """You answer strictly from the numbered passages provided. You are a reader of an offline archive, not a source of knowledge. You are checking an answer someone else gave, so be brief.

RULES
1. For any claim you present as fact, use ONLY the passages. Do not use your own knowledge at all.
2. Cite every claim inline as [1], [2], matching the passage numbers. A sentence with no citation must not contain a fact.
3. If the passages do not answer the question, say exactly: NOT IN ARCHIVE
   followed by one sentence saying what is missing. Then stop.
4. Never state a number, dose, voltage, pressure or measurement that is not written in a passage. Copy figures exactly, with their units.
5. Answer in the same language as the question.
6. Be brief and direct. No preamble, no summary of the question, no offer to help further. A few sentences is enough."""

# WRITTEN OUT, NOT DERIVED. The first version built this by filtering rule 3b's
# lines out of SYSTEM, which left three orphan fragments of it stranded between
# rules 3 and 4 - a prompt that was worse than either whole one, produced by a
# rule that read as reasonable and broke on the first edit it met. Two prompts
# that must stay in step are a real cost; a prompt that silently assembles
# itself wrong is a larger one. The selftest below asserts they cannot drift
# into each other.


def build_check_messages(question, results):
    """The passages and the question, WITHOUT rule 3b's block requirement."""
    m = build_messages(question, results)
    m[0] = {"role": "system", "content": CROSSCHECK_SYSTEM}
    return m


# BUDGET: 700, AND THE HISTORY IS THE POINT.
# It was 600 when Gemma returned 1,618 characters of reasoning and no answer at
# all. The first fix raised it to 1,000 - and the node's next answer timed out
# at 240s with `ark.py status` showing all 16 GB of VRAM free while both models
# reported UP, which is the signature of Windows evicting the primary's weights
# under memory pressure on a machine that had 1.6 GB of RAM to spare. A probe a
# minute later pulled Qwen back and nvidia-smi then read 15,437 MiB used.
#
# WHETHER THE LARGER BUDGET CAUSED THAT WAS NEVER ESTABLISHED, and it is written
# down as unestablished rather than assumed either way. What IS established is
# that this machine has no headroom to spend on a guess. The real lever is
# CROSSCHECK_SYSTEM above: a checker asked for a few sentences needs far less
# room than one asked, as it was until today, to write a second full answer.
# THE CHECKER DOES NOT THINK ALOUD, 2026-09-27. `bin/answer-ab.py` on the node:
# 10 of 12 cross-checks hit the 700-token limit and 5 returned no answer at all,
# each after 2,100 to 2,800 characters of reasoning and about 285 seconds. The
# comment above named this lever on 2026-09-10 - "stopping Gemma from thinking
# at all for this task" - and a bigger budget is not one: at 2.5 t/s every 100
# tokens is 40 more seconds. ARK_CROSSCHECK_THINKING=on restores the old
# behaviour for comparison.
CROSSCHECK_THINKING = (os.environ.get("ARK_CROSSCHECK_THINKING", "off")
                       .strip().lower() in ("on", "1", "true", "yes")) or False


def crosscheck(question, results, primary_text, n_predict=700, cancel=None):
    """The second opinion. Different model family, same question.

    SAME PASSAGES ON PURPOSE, EXCEPT WHEN THERE ARE NONE THAT HELP. Retrieving
    separately would let the two models disagree because they read different
    things, which is a fact about the retrieval layer and not about the models.
    §9.4 wants the models compared. But if the primary went unsourced, the
    passages are what failed, so the second model is asked for its own recall
    instead - see `build_recall_messages`."""
    p_head_x, p_unsourced = split_unsourced(primary_text)
    # RECALL-ONLY IS FOR ANSWERS WHERE THE PASSAGES FAILED, NOT FOR EVERY BLOCK.
    # A `grounded_plus` answer cited the passages and then added something; the
    # passages did NOT fail, so asking the second model with no passages would
    # throw away the check that matters - the cited claim - to check the
    # footnote. Only an answer whose head is the admission gets the plain
    # question.
    recall = (p_unsourced is not None
              and NOT_IN_ARCHIVE in p_head_x.upper())
    msgs = (build_recall_messages(question) if recall
            else build_check_messages(question, results))
    # `cancel` is a llm.CancelToken or None. It is threaded through rather than
    # held here because THE DECISION TO STOP IS NOT THIS FUNCTION'S: serve.py
    # owns it, and cancels when a new question arrives. See llm.CancelToken for
    # why a closed socket and not a killed process.
    r = llm.chat("crosscheck", msgs, n_predict=n_predict,
                 temperature=temperature_for_check(), cancel=cancel,
                 thinking=CROSSCHECK_THINKING)

    text = r["text"]
    if recall:
        # WRAPPED HERE RATHER THAN ASKED FOR. Gemma is a different family and
        # may not honour a format instruction; constructing the block makes the
        # comparison independent of whether it complied. The text is unsourced
        # by construction because of what it was asked, not by what it wrote.
        text = ("NOT IN ARCHIVE\nAsked for recall only; no passage was supplied.\n\n"
                + UNSOURCED_MARK + "\n" + (text or "").strip())

    # AN EMPTY SECOND ANSWER IS A FAILURE, NOT A COMPARISON RESULT.
    # Seen on the node 2026-09-10: Gemma returned no text and `compare()` fell
    # through to `no_basis`, whose wording is "one answer was empty" - which
    # renders as *No basis for comparison*, a sentence that reads like a finding
    # about two answers rather than the absence of one. Same shape as
    # `both_unsupported` swallowing the unsourced case: a calm state covering a
    # breakdown. The likely cause is the model spending its whole budget on a
    # reasoning trace, so the diagnosis is carried rather than guessed at.
    body = (r["text"] or "").strip()
    if not body:
        why = ("the second model returned no answer at all - "
               + ("its output was %d characters of reasoning and no answer, "
                  % len(r["reasoning"] or "") if r.get("reasoning") else "")
               + ("and it hit its %d token limit" % n_predict if r.get("truncated")
                  else "and it stopped on its own"))
        return {
            "text": "",
            "recall_only": recall,
            "grounding": "empty",
            "cited": [],
            "invented_citations": [],
            "reasoning_chars": len(r["reasoning"] or ""),
            "thinking": bool(CROSSCHECK_THINKING),
            "truncated": r["truncated"],
            "seconds": r["seconds"],
            "tokens_per_second": r["tokens_per_second"],
            "comparison": {"state": "no_answer", "why": why, "figures": {}},
        }

    n = min(len(results), MAX_PASSAGES)
    state, used, bogus = grounding(text, n)
    cmp = compare(primary_text, text)
    return {
        "text": text,
        "recall_only": recall,
        "grounding": state,
        "cited": used,
        "invented_citations": bogus,
        "reasoning_chars": len(r["reasoning"] or ""),
        "thinking": bool(CROSSCHECK_THINKING),
        "truncated": r["truncated"],
        "seconds": r["seconds"],
        "tokens_per_second": r["tokens_per_second"],
        "comparison": cmp,
    }


def temperature_for_check():
    # THE SAME TEMPERATURE AS THE PRIMARY, DELIBERATELY.
    # A hotter cross-check would disagree more often by sampling alone, and a
    # cooler one would agree more often. Either turns the disagreement rate into
    # a property of this constant instead of a property of the models.
    return 0.2


# ---------------------------------------------------------------------------
# 4. SELFTEST - the parsing half, which needs no model
# ---------------------------------------------------------------------------

CASES = [
    ("Give 500 mg every 8 hours [1]. Not more than 4 g per day [2].", 5,
     "grounded", [1, 2]),
    ("NOT IN ARCHIVE. The passages describe symptoms but give no dosage.", 5,
     "uncovered", []),
    ("Amoxicillin is usually 500 mg three times a day.", 5, "model_only", []),
    ("See [1] and [7] for details.", 5, "grounded", [1]),

    # ---- the unsourced path, added 2026-09-09 -----------------------------
    # THE ADMISSION IS WHAT MAKES IT LEGITIMATE, so the case that proves it is
    # the one WITHOUT the admission. Both texts carry the same marker and the
    # same unverified sentence; only one of them told the operator first, and
    # only that one is `unsourced`. The other stays a fault.
    ("NOT IN ARCHIVE. No passage covers this fibre.\n\n"
     + UNSOURCED_MARK + "\nManila of that diameter is usually taken near 12 kN.",
     5, "unsourced", []),
    # GROUNDED_PLUS: cited, and then the model added something. The block no
    # longer needs the admission in front of it - it needs to not stand alone.
    ("Rated at 12 kN [1].\n" + UNSOURCED_MARK
     + "\nOlder manila can be well below that; passage [1] does not say how it "
       "was aged.", 5, "grounded_plus", [1]),
    # and the case that keeps the fault a fault: block, no admission, NO
    # CITATION. Nothing accountable in front of it.
    ("Manila is usually near 12 kN.\n" + UNSOURCED_MARK
     + "\nOlder rope may be weaker.", 5, "model_only", []),
    (UNSOURCED_MARK + "\nManila of that diameter is usually taken near 12 kN.",
     5, "model_only", []),
    # A cited answer that happens to contain the words is still grounded: the
    # marker is only structural at the start of its own line.
    #
    # NOTE, 2026-09-10: the sentence that used to end this comment - "a
    # passage-backed answer never reaches the block" - was true for one day.
    # Rule 3b now asks for the block on every answer. Corrected rather than
    # left standing.
    ("NOT IN ARCHIVE. Nothing on this.\n\n" + UNSOURCED_MARK
     + "\nRoughly 12 kN [1], though I am not certain.", 5, "unsourced", []),
]

# Rule 3b says the block carries no citations. Text, and how many it smuggled in.
UNSOURCED_CITE_CASES = [
    ("NOT IN ARCHIVE. Nothing.\n\n" + UNSOURCED_MARK + "\nAbout 12 kN.", 0),
    ("NOT IN ARCHIVE. Nothing.\n\n" + UNSOURCED_MARK + "\nAbout 12 kN [1].", 1),
    ("NOT IN ARCHIVE. Nothing.\n\n" + UNSOURCED_MARK
     + "\nSee [1] and [2] and [3].", 3),
    ("Give 500 mg [1].", 0),
    # GROUNDED_PLUS: the head is full of citations and the block must still have
    # none. Rule 3b's prohibition is about the BLOCK, not about the answer, and
    # this is the case where the two live in one string.
    ("Give 500 mg every 8 hours [1]. No more than 4 g a day [2].\n"
     + UNSOURCED_MARK + "\nMost guidance now says every 6 hours.", 0),
    ("Give 500 mg [1].\n" + UNSOURCED_MARK
     + "\nThis contradicts [1], which I believe is out of date.", 1),
]

COMPARE_CASES = [
    # 2026-09-27: A CITED ANSWER WITH ITS BLOCK, AGAINST A CHECKER THAT FOUND
    # NOTHING IN THE SAME PASSAGES, IS A DISAGREEMENT ABOUT THE PASSAGES - not
    # "one_declined", which is about two models' own recall.
    ("Use about a 10A fuse [2].\nUNSOURCED - NOT FROM THE ARCHIVE\nAgrees. "
     "For 100W at 12V the current is about 8.3A, so 10A.",
     "NOT IN ARCHIVE. The passages give fuse sizes for other panels.",
     "disagree"),
    # 2026-09-27: A LONG CITED ANSWER AND A SHORT ONE ABOUT THE SAME THING AGREE.
    # Jaccard over the whole text called this disagree once the knowledge-first
    # prompt tripled the primary's length; containment of the shorter does not.
    ("Disconnect the negative battery terminal first [1]. Loosen the tensioner "
     "and slip the serpentine belt off the alternator pulley [1]. Unplug the "
     "wiring harness and remove the mounting bolts [2]. Fit the new alternator, "
     "refit the bolts, the harness and the belt, then reconnect the battery [2]. "
     "Check the belt routing against the diagram under the hood [3].\n"
     "UNSOURCED - NOT FROM THE ARCHIVE\nAgrees. A long block of the model's own "
     "recall about charging systems, diodes, regulators and belt wear.",
     "Disconnect the battery, remove the belt from the alternator pulley, unplug "
     "the harness, remove the mounting bolts and fit the new alternator [1].",
     "agree"),
    ("Give 500 mg every 8 hours [1].", "Administer 500 mg three times daily [1].",
     "agree"),
    ("Give 500 mg every 8 hours [1].", "Give 250 mg every 8 hours [1].", "disagree"),
    ("NOT IN ARCHIVE. No dosage is given.", "NOT IN ARCHIVE. The passages omit it.",
     "both_unsupported"),
    ("The mains supply is 240 V [1].", "NOT IN ARCHIVE.", "disagree"),
    # the case that caught the category-bucket bug: same frequency, half the dose
    ("Give 500 mg every 8 hours [1].", "Give 250 mg every 8 hours [1].", "disagree"),
    ("Inflate to 120 psi [1].", "Inflate to 120 psi and check the valve [2].", "agree"),
    ("Torque to 90 Nm [1].", "Torque to 40 Nm [1].", "disagree"),

    # GROUNDED_PLUS MUST NOT ROUTE INTO THE BLOCK BRANCH. Both answers cite the
    # passages and one adds a block; what is worth comparing is the two cited
    # claims, not the two footnotes. Without the routing gate this returned
    # `one_declined` - "one model answered from its own training and the other
    # would not" - about two models that had both answered from the archive.
    ("Give 500 mg every 8 hours [1].\n" + UNSOURCED_MARK
     + "\nMost guidance now says every 6 hours.",
     "Administer 500 mg three times daily [1].", "agree"),
    ("Give 500 mg every 8 hours [1].\n" + UNSOURCED_MARK
     + "\nI recall 250 mg, which contradicts passage [1].",
     "Give 250 mg every 8 hours [1].", "disagree"),

    # THE UNSOURCED STATES, WHICH WERE THE HOLE. Every one of these four
    # answers contains NOT IN ARCHIVE - the structural rule requires the
    # admission before the block - so before the unsourced branch existed all
    # four returned `both_unsupported`, described in this file as "the most
    # reassuring result the node can give". Two models guessing different doses
    # were reported to the operator as two models agreeing that the archive is
    # silent. These cases exist so that regression fails here rather than on
    # screen.
    (NOT_IN_ARCHIVE + ". Nothing on this.\n" + UNSOURCED_MARK
     + "\nAbout 500 mg every 8 hours.",
     NOT_IN_ARCHIVE + ". The passages omit it.",
     "one_declined"),
    (NOT_IN_ARCHIVE + ". Nothing.\n" + UNSOURCED_MARK
     + "\nAbout 500 mg every 8 hours.",
     NOT_IN_ARCHIVE + ". Nothing.\n" + UNSOURCED_MARK
     + "\nAbout 250 mg every 8 hours.",
     "disagree_unsourced"),
    (NOT_IN_ARCHIVE + ". Nothing.\n" + UNSOURCED_MARK
     + "\nAbout 500 mg every 8 hours.",
     NOT_IN_ARCHIVE + ". Nothing.\n" + UNSOURCED_MARK
     + "\nUsually given as 500 mg three times daily.",
     "agree_unsourced"),
    (NOT_IN_ARCHIVE + ". Nothing.\n" + UNSOURCED_MARK
     + "\nKeep the wound clean and watch for spreading redness.",
     NOT_IN_ARCHIVE + ". Nothing.\n" + UNSOURCED_MARK
     + "\nIrrigate well and leave it open.",
     "no_basis_unsourced"),
]


def _check_prompts():
    """The two system prompts must not drift into each other.

    THE CHECK EXISTS BECAUSE THE FIRST VERSION FAILED THIS WAY. CROSSCHECK_SYSTEM
    was derived from SYSTEM by dropping rule 3b's lines, and left three orphan
    fragments of it stranded between rules 3 and 4. Nothing would have noticed:
    a malformed system prompt produces a plausible answer, which is this build's
    named failure shape. So the properties are asserted rather than trusted."""
    bad = 0
    lines = CROSSCHECK_SYSTEM.splitlines()
    checks = [
        ("crosscheck has no rule 3b", "3b." not in CROSSCHECK_SYSTEM),
        ("crosscheck never names the block", UNSOURCED_MARK not in CROSSCHECK_SYSTEM),
        ("crosscheck rules run 1..6 with no 7",
         all(("\n%d." % i) in ("\n" + CROSSCHECK_SYSTEM) for i in range(1, 7))
         and "\n7." not in CROSSCHECK_SYSTEM),
        ("no orphan fragments",
         all((not ln.strip()) or ln.startswith((" ", "\t"))
             or ln[0].isdigit() or ln[0].isupper() for ln in lines)),
        ("primary still requires the block",
         "3b." in SYSTEM and UNSOURCED_MARK in SYSTEM),
        ("both still demand citations", "[1]" in SYSTEM and "[1]" in CROSSCHECK_SYSTEM),
        # 2026-09-27: THE KNOWLEDGE PROMPT MUST KEEP THE READER PROMPT'S
        # STRUCTURE. Voice and length changed; the shape the parsers read did
        # not, and these are the pieces they read.
        ("both answer prompts keep rules 1-6, 3b, the block and NOT IN ARCHIVE",
         all(all(("\n%s." % k) in ("\n" + s) for k in ("1", "2", "3", "3b", "4", "5", "6"))
             and UNSOURCED_MARK in s and NOT_IN_ARCHIVE in s and "[1]" in s
             for s in (SYSTEM_READER, SYSTEM_KNOWLEDGE))),
        ("knowledge prompt no longer asks for brevity",
         "Be brief" not in SYSTEM_KNOWLEDGE and "not a source of knowledge" not in SYSTEM_KNOWLEDGE),
        ("the selected prompt is one of the two", SYSTEM in (SYSTEM_READER, SYSTEM_KNOWLEDGE)),
    ]
    for name, ok in checks:
        if ok:
            print("  ok    prompt %s" % name)
        else:
            bad += 1
            print("  FAIL  prompt %s" % name)
    return bad


def selftest():
    bad = _check_prompts()
    for text, n, want_state, want_cited in CASES:
        state, used, bogus = grounding(text, n)
        ok = (state == want_state and used == want_cited)
        if not ok:
            bad += 1
            print("  FAIL  want %s/%s  got %s/%s\n        %s"
                  % (want_state, want_cited, state, used, text[:60]))
        else:
            print("  ok    %-10s cited=%-8s %s%s"
                  % (state, used, text[:44],
                     "  (dropped invented %s)" % bogus if bogus else ""))
    for text, want_n in UNSOURCED_CITE_CASES:
        got = unsourced_citations(text, 5)
        if got != want_n:
            bad += 1
            print("  FAIL  block citations want %d got %d\n        %s"
                  % (want_n, got, text[:56]))
        else:
            print("  ok    block citations %d  %s" % (got, text[-40:]))
    for a, b, want in COMPARE_CASES:
        got = compare(a, b)["state"]
        if got != want:
            bad += 1
            print("  FAIL  compare want %s got %s\n        %r vs %r"
                  % (want, got, a[:34], b[:34]))
        else:
            print("  ok    compare %-16s %r vs %r" % (got, a[:26], b[:26]))
    total = (6 + len(CASES) + len(COMPARE_CASES)
             + len(UNSOURCED_CITE_CASES))   # 6 = _check_prompts
    print("\n%d/%d" % (total - bad, total))
    return bad


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if selftest() else 0)
    q = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    if not q:
        print("usage: answer.py \"a question\"   |   answer.py --selftest")
        sys.exit(2)
    import store, json
    S = store.Store()
    ids = S.keyword(q, 20)
    results = []
    for cid in ids:
        m = S.meta(cid)
        if not m:
            continue
        txt = " ".join(S.text(m).split())
        if S.is_navigation(m, txt):
            continue
        results.append({"cid": cid, "text": txt, "citation": S.citation(m)})
        if len(results) >= MAX_PASSAGES:
            break
    print("%d passages\n" % len(results))
    # A FORTY-LINE TRACEBACK IS NOT AN HONEST FAILURE, IT IS A WALL.
    # The library raises on purpose - see answer() - because a caller that
    # swallowed it would let the answer pane go quiet while the source pane kept
    # working, teaching the operator that silence means the archive has nothing.
    # But the CLI is the operator, and it should say which model is down and what
    # still works.
    try:
        a = answer(q, results)
    except llm.LLMUnavailable as e:
        print("The %s model is not answering.\n  %s\n" % (e.role, e.detail))
        print("  Start it, then re-run:")
        print("    python bin/ark.py up %s" % e.role)
        print("  `python bin/ark.py commands` prints the llama-server line it runs.")
        print("\n  Retrieval itself is unaffected. The %d passages above were "
              "found without any model, and\n  bin/index-query.py and the source "
              "pane both still work." % len(results))
        sys.exit(3)
    print(a["text"])
    print("\n[%s | cited %s | %s tok, %ss, %s t/s%s]"
          % (a["grounding"], a["cited"], a["completion_tokens"], a["seconds"],
             a["tokens_per_second"], " | TRUNCATED" if a["truncated"] else ""))
    if a["invented_citations"]:
        print("INVENTED CITATIONS DROPPED: %s" % a["invented_citations"])
    if a["safety"]["level"] != "none":
        print("safety: %s %s" % (a["safety"]["level"], a["safety"]["categories"]))
    for i, r in enumerate(results, 1):
        c = r["citation"]
        print("  [%d] %s\n      %s" % (i, c.get("title", "")[:70], c.get("url", "")))
