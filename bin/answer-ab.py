#!/usr/bin/env python3
"""answer-ab.py - measure the answer pane on a fixed question set, one prompt at a time.

Written 2026-09-27 for step A of 00-docs/DESIGN-knowledge-first.md. The answer
prompt changed from "a reader of the archive, not a source of knowledge ... be
brief" to a knowledge-first prompt that asks for complete answers. Whether that
made answers better is a question about many answers, not one screenshot, so
this asks the RUNNING node the same questions and writes down what came back.

    python bin/answer-ab.py                      ask the default 12 questions
    python bin/answer-ab.py --file q.txt         one question per line
    python bin/answer-ab.py --no-crosscheck      do not wait for the second model
    python bin/answer-ab.py --node http://localhost:8090

RUN IT ONCE PER PROMPT. The node chooses the prompt at start-up from
ARK_ANSWER_PROMPT (`knowledge`, the default, or `reader`, the old one). Each run
records which prompt answered, so two runs can be compared line by line:

    python bin/answer-ab.py --compare A.jsonl B.jsonl

THE CROSS-CHECK IS WAITED FOR BY DEFAULT, and that is most of the time: a new
question cancels a running cross-check (serve.py), so asking the next one early
would measure nothing about the second model. With Gemma on the CPU that is about
four minutes a question. --no-crosscheck measures the primary alone in minutes.

Standard library only. Writes to _incoming/answer-ab/ under the archive root,
which is outside every checksummed shelf. Starts nothing, changes nothing.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

QUESTIONS = [
    "how much chlorine to disinfect drinking water",
    "paracetamol dose for a 5 year old child who weighs 20 kg",
    "what do I do for someone in shock",
    "my well water tastes metallic, is it safe to drink",
    "how do I replace a car alternator",
    "how to splice a broken 12V wire so it lasts outdoors",
    "when do I plant potatoes and how do I know they are ready to harvest",
    "how does a solar charge controller work",
    "what size fuse for a 100W 12V solar panel",
    "how to make soap from wood ashes and animal fat",
    "¿cómo desinfectar agua con la luz del sol?",
    "¿cómo tratar una quemadura de segundo grado?",
]

FIELDS = ["prompt", "grounding", "completion_tokens", "prompt_tokens", "n_predict",
          "seconds", "truncated", "cited", "invented_citations",
          "unsourced_citations", "safety_level", "chars_cited", "chars_block",
          "crosscheck"]


def get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def split_block(text):
    mark = "UNSOURCED - NOT FROM THE ARCHIVE"
    i = (text or "").upper().find(mark)
    if i < 0:
        return text or "", ""
    return text[:i], text[i + len(mark):]


def ask(node, q, mode, wait_check):
    url = "%s/api/answer?%s" % (node, urllib.parse.urlencode(
        {"q": q, "n": 5, "mode": mode}))
    t0 = time.time()
    out = get(url, 600)
    a = out.get("answer") or {}
    head, blk = split_block(a.get("text") or "")
    row = {"q": q, "wall_seconds": round(time.time() - t0, 1),
           "model_down": out.get("model_down"),
           "prompt": a.get("prompt"), "grounding": a.get("grounding"),
           "completion_tokens": a.get("completion_tokens"),
           "prompt_tokens": a.get("prompt_tokens"), "n_predict": a.get("n_predict"),
           "seconds": a.get("seconds"), "truncated": a.get("truncated"),
           "cited": a.get("cited"), "invented_citations": a.get("invented_citations"),
           "unsourced_citations": a.get("unsourced_citations"),
           "safety_level": (a.get("safety") or {}).get("level"),
           "chars_cited": len(head.strip()), "chars_block": len(blk.strip()),
           "text": a.get("text"), "crosscheck": None}
    job = out.get("crosscheck_job")
    if job and wait_check:
        while True:
            time.sleep(5)
            try:
                j = get("%s/api/crosscheck?job=%s" % (node, job), 30)
            except Exception as e:                           # noqa: BLE001
                row["crosscheck"] = "poll failed: %s" % e
                break
            if j.get("state") != "running":
                cmp = j.get("comparison") or {}
                row["crosscheck"] = cmp.get("state") or j.get("state")
                row["crosscheck_detail"] = j
                break
    elif job:
        row["crosscheck"] = "not waited for"
    return row


def summary(rows):
    ok = [r for r in rows if r.get("grounding")]
    if not ok:
        return "no answers - is the primary model running?"
    med = lambda xs: sorted(xs)[len(xs) // 2] if xs else None     # noqa: E731
    toks = [r["completion_tokens"] or 0 for r in ok]
    secs = [r["seconds"] or 0 for r in ok]
    lines = [
        "prompt            %s" % ", ".join(sorted({str(r['prompt']) for r in ok})),
        "answers           %d of %d" % (len(ok), len(rows)),
        "tokens median     %s   (max %s)" % (med(toks), max(toks)),
        "seconds median    %s   (max %s)" % (med(secs), max(secs)),
        "cited part chars  median %s" % med([r["chars_cited"] for r in ok]),
        "block chars       median %s" % med([r["chars_block"] for r in ok]),
        "truncated         %d" % sum(1 for r in ok if r["truncated"]),
        "invented cites    %d" % sum(len(r["invented_citations"] or []) for r in ok),
        "cites in block    %d" % sum(r["unsourced_citations"] or 0 for r in ok),
        "grounding         %s" % ", ".join("%s %d" % (g, sum(1 for r in ok if r["grounding"] == g))
                                           for g in sorted({r["grounding"] for r in ok})),
        "cross-check       %s" % ", ".join("%s %d" % (c, sum(1 for r in ok if r["crosscheck"] == c))
                                           for c in sorted({str(r["crosscheck"]) for r in ok})),
    ]
    return "\n".join(lines)


def compare(a_path, b_path):
    A = {r["q"]: r for r in map(json.loads, open(a_path, encoding="utf-8"))}
    B = {r["q"]: r for r in map(json.loads, open(b_path, encoding="utf-8"))}
    print("%-44s %18s %18s %14s" % ("question", "tokens A -> B", "seconds A -> B", "check A|B"))
    for q in A:
        if q not in B:
            continue
        a, b = A[q], B[q]
        print("%-44s %8s -> %-7s %8s -> %-7s %6s|%s" % (
            q[:44], a.get("completion_tokens"), b.get("completion_tokens"),
            a.get("seconds"), b.get("seconds"),
            str(a.get("crosscheck"))[:6], str(b.get("crosscheck"))[:6]))
    print("\nA: %s\n\nB: %s" % (summary(list(A.values())), summary(list(B.values()))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", default="http://localhost:8090")
    ap.add_argument("--file")
    ap.add_argument("--mode", default="hybrid")
    ap.add_argument("--no-crosscheck", action="store_true")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    a = ap.parse_args()
    if a.compare:
        compare(*a.compare)
        return 0
    qs = QUESTIONS
    if a.file:
        qs = [ln.strip() for ln in open(a.file, encoding="utf-8") if ln.strip()]
    outdir = os.path.join(ROOT, "_incoming", "answer-ab")
    os.makedirs(outdir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(outdir, "run-%s.jsonl" % stamp)
    rows = []
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for i, q in enumerate(qs, 1):
            print("[%d/%d] %s" % (i, len(qs), q), flush=True)
            try:
                r = ask(a.node, q, a.mode, not a.no_crosscheck)
            except Exception as e:                           # noqa: BLE001
                r = {"q": q, "error": "%s: %s" % (type(e).__name__, e)}
            rows.append(r)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()
            print("      %s · %s tokens · %ss · check %s" % (
                r.get("grounding") or r.get("error") or r.get("model_down"),
                r.get("completion_tokens"), r.get("seconds"), r.get("crosscheck")),
                flush=True)
    print("\n" + summary(rows))
    print("\nwrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
