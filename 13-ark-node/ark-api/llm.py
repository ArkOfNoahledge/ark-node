#!/usr/bin/env python3
"""
llm.py - transport to the two llama-server instances. Standard library only.

    python 13-ark-node/ark-api/llm.py --probe
    python 13-ark-node/ark-api/llm.py "what is a safe chlorine dose for drinking water"

TWO SERVERS, TWO MEMORIES, ONE CODE PATH.

    primary      Qwen3.8-27B IQ4_XS   GPU, ngl 64    ~25 t/s   127.0.0.1:8091
    crosscheck   Gemma 4 31B IQ4_XS   CPU, 64GB RAM  ~3 t/s    127.0.0.1:8092

Measured 2026-09-04, BUILD-LOG Phase 2G. They are on different memory, so both are
resident at once with no model swap, and - the reason it matters - both run at
IQ4_XS. Spec §9.4 warns that where the two families run at different quantization
levels some disagreement is compression noise rather than the models disagreeing.
That caveat is removed here rather than managed, and it was removed by the hardware
constraint that looked like the end of the idea: Gemma 4 31B is 14.82 GiB at its
smallest quantization against 14.69 GiB of free VRAM, so it could never have shared
the GPU. Being forced into system RAM is what let it keep its quantization.

WHY THE CHAT ENDPOINT AND NOT /completion. Two model families, two different chat
templates. /v1/chat/completions makes the server apply each model's own template;
/completion would make this file carry a copy of both, and a chat template that
drifts from its model produces a fluent answer to a subtly different question -
the worst failure shape available, because nothing looks wrong.

QWEN THINKS OUT LOUD. Its template emits reasoning before the answer, which the
server separates into `reasoning_content`. It is returned here rather than
discarded, so the surface can show the working when asked - but it is NEVER part
of the answer text, because reasoning that reads like a conclusion is exactly what
§11.1 warns about.

THIS FILE HAS NO OPINIONS ABOUT PROMPTS. Grounding, citations and the cross-check
comparison live in answer.py. This is transport, health and honest failure.
"""

import http.client
import io as _io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINTS = {
    "primary": os.environ.get("ARK_PRIMARY_URL", "http://127.0.0.1:8091"),
    "crosscheck": os.environ.get("ARK_CROSSCHECK_URL", "http://127.0.0.1:8092"),
}

# TIMEOUTS ARE PER ROLE AND THEY ARE NOT THE SAME NUMBER.
# The cross-check runs at about 3 t/s. A 500-token second opinion is nearly three
# minutes of legitimate work, and a timeout tuned to the GPU would kill it every
# time and report the model as down. A wrong timeout is indistinguishable from a
# broken service from the outside, which is how a working component gets removed.
TIMEOUT = {"primary": 240, "crosscheck": 900}

# ---------------------------------------------------------------------------
# CANCELLATION, AND WHY IT IS A SOCKET AND NOT A KILL
# ---------------------------------------------------------------------------
#
# A cross-check the operator walked away from used to run to completion: about
# 2.5 minutes of 16 CPU threads producing a second opinion nobody would read,
# and - because Gemma is launched with no `-np` and llama-server defaults to one
# slot - the NEXT cross-check queued behind it rather than running. Three
# safety-relevant questions in a row put the third check five minutes out while
# the surface kept promising 2.5.
#
# THE FIX IS NOT KILLING THE PROCESS. `llama-server` holds a 31B model in 21 GB
# of system RAM; killing it makes the next cross-check pay minutes to reload, so
# cancelling one check would slow down the one after it. What has to be aborted
# is the REQUEST, which frees the slot: llama-server stops generating when the
# client disconnects, and a disconnect is a closed socket.
#
# `urllib` gives no handle on its socket, which is why this file grew a second
# transport rather than a flag. The primary's path is untouched and still goes
# through urllib - it answers in ~20s and has four slots, so there is nothing to
# cancel and no reason to carry the risk.
#
# WHETHER IT WORKS IS OBSERVABLE, NOT ASSUMED: Gemma's CPU should fall within a
# second or two of a cancel. If it does not, the disconnect is not reaching the
# server and this design is wrong.


class Cancelled(Exception):
    """The request was aborted on purpose. NOT a failure of the model.

    Kept distinct from LLMUnavailable all the way to the surface: an operator who
    reads "the second model is not available" beside a dose will go looking for a
    broken service, and an operator who reads nothing at all will assume the
    answer was checked. Cancelled is neither, and it says so."""


class CancelToken:
    """Held by the caller, attached to the live connection by the transport.

    THE RACE IS THE POINT. Cancel can arrive before the connection exists, while
    it is being made, or after the response is already read. `attach` returns
    False if cancellation got there first, so a request that was cancelled before
    it was sent is never sent at all rather than being sent and abandoned."""

    def __init__(self):
        self._lock = threading.Lock()
        self._conn = None
        self.cancelled = False

    def attach(self, conn):
        with self._lock:
            if self.cancelled:
                _shut(conn)
                return False
            self._conn = conn
            return True

    def detach(self):
        with self._lock:
            self._conn = None

    def cancel(self):
        """True if there was a live connection to close."""
        with self._lock:
            self.cancelled = True
            c, self._conn = self._conn, None
        if c is None:
            return False
        _shut(c)
        return True


def _shut(conn):
    # close() alone can block behind a socket that is mid-read, so the socket is
    # shut down first and the close is best effort. Either way this must never
    # raise into the thread doing the cancelling.
    try:
        sock = getattr(conn, "sock", None)
        if sock is not None:
            import socket as _socket
            try:
                sock.shutdown(_socket.SHUT_RDWR)
            except OSError:
                pass
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


HEALTH_TTL = 3.0            # seconds; /api/health must not stall on a busy model
_health_cache = {}
_lock = threading.Lock()


class LLMUnavailable(Exception):
    """The server did not answer. Carries the role so the surface can say WHICH."""

    def __init__(self, role, detail):
        self.role = role
        self.detail = detail
        Exception.__init__(self, "%s: %s" % (role, detail))


def _post(url, payload, timeout, cancel=None):
    body = json.dumps(payload).encode("utf-8")
    if cancel is None:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    return _post_cancellable(url, body, timeout, cancel)


def _post_cancellable(url, body, timeout, cancel):
    """The same POST over a connection the caller can close from another thread.

    RAISES THE SAME EXCEPTIONS AS THE URLLIB PATH, deliberately: a non-200 comes
    back as urllib.error.HTTPError carrying the body, so `chat` below needs no
    second error branch and the two transports cannot drift in how they report a
    failure. The one new exception is Cancelled, and it is raised only when this
    token was actually cancelled - a socket error on a request nobody cancelled
    is still a real error and must not be disguised as an intention."""
    u = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
    if not cancel.attach(conn):
        raise Cancelled("cancelled before the request was sent")
    try:
        conn.request("POST", (u.path or "/") + (("?" + u.query) if u.query else ""),
                     body=body, headers={"Content-Type": "application/json",
                                         "Content-Length": str(len(body))})
        r = conn.getresponse()
        raw = r.read()
        if r.status != 200:
            raise urllib.error.HTTPError(url, r.status, r.reason,
                                         r.getheaders(), _io.BytesIO(raw))
        return json.loads(raw.decode("utf-8"))
    except Cancelled:
        raise
    except urllib.error.HTTPError:
        raise
    except Exception:
        if cancel.cancelled:
            raise Cancelled("connection closed by cancel")
        raise
    finally:
        cancel.detach()
        _shut(conn)


def _get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def health(role, force=False):
    """Cheap, cached, and never raises. Returns a dict the surface can render.

    A health probe that blocks is worse than one that is a few seconds stale: the
    cross-check is a single-slot server that is busy for minutes at a time, and a
    status page that hangs while it works reads to the operator as a dead node."""
    base = ENDPOINTS.get(role)
    if not base:
        return {"role": role, "ok": False, "error": "no endpoint configured"}
    now = time.time()
    with _lock:
        hit = _health_cache.get(role)
        if hit and not force and now - hit[0] < HEALTH_TTL:
            return hit[1]
    out = {"role": role, "url": base}
    try:
        h = _get(base + "/health", 2.5)
        out["ok"] = (h.get("status") == "ok")
        out["status"] = h.get("status")
    except urllib.error.URLError as e:
        out["ok"] = False
        out["error"] = "not reachable (%s)" % (getattr(e, "reason", e),)
    except Exception as e:                                  # pragma: no cover
        out["ok"] = False
        out["error"] = "%s: %s" % (type(e).__name__, e)
    if out.get("ok"):
        try:
            m = _get(base + "/v1/models", 2.5)
            data = m.get("data") or []
            if data:
                out["model"] = data[0].get("id")
        except Exception:
            pass        # the alias is a nicety; its absence is not a fault
    with _lock:
        _health_cache[role] = (now, out)
    return out


def available(role):
    return bool(health(role).get("ok"))


def chat(role, messages, n_predict=512, temperature=0.2, top_p=0.9,
         stop=None, timeout=None, cancel=None, thinking=None):
    """One completion. Returns text, reasoning, token counts and wall time.

    temperature defaults LOW and deliberately. This is a reference machine, not a
    writing assistant: §9.2's whole posture is that specifics get verified, and
    sampling variance in a dose is a defect, not a feature."""
    base = ENDPOINTS.get(role)
    if not base:
        raise LLMUnavailable(role, "no endpoint configured")
    payload = {
        "messages": messages,
        "max_tokens": n_predict,
        "temperature": temperature,
        "top_p": top_p,
        "stream": False,
    }
    if stop:
        payload["stop"] = stop
    # thinking=False ASKS THE CHAT TEMPLATE TO SKIP THE REASONING TRACE, per
    # request, 2026-09-27. Measured that day: Gemma, as the cross-check, spent
    # its whole 700-token budget thinking on 10 of 12 questions and returned NO
    # ANSWER on 5 - 2,100 to 2,800 characters of reasoning each time. The flag
    # travels as chat_template_kwargs.enable_thinking, which templates that do
    # not know the variable ignore; whether Gemma's honours it is a MEASUREMENT,
    # read from `reasoning_chars` in the cross-check result, not an assumption.
    if thinking is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    t0 = time.time()
    try:
        d = _post(base + "/v1/chat/completions", payload,
                  timeout or TIMEOUT.get(role, 240), cancel=cancel)
    except Cancelled:
        # NOT wrapped in LLMUnavailable. The model is fine; we stopped asking.
        raise
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise LLMUnavailable(role, "HTTP %s: %s" % (e.code, detail))
    except urllib.error.URLError as e:
        raise LLMUnavailable(role, "not reachable (%s)" % (getattr(e, "reason", e),))
    except Exception as e:
        raise LLMUnavailable(role, "%s: %s" % (type(e).__name__, e))
    secs = time.time() - t0

    choices = d.get("choices") or []
    msg = (choices[0].get("message") if choices else {}) or {}
    text = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()

    # THE SERVER'S REASONING PARSER IS NOT LOAD-BEARING. THIS IS.
    # Observed in the browser 2026-09-05: with a system message and a capped
    # reasoning budget, `--reasoning-format deepseek` stopped separating the
    # thought trace and shipped it inside `content`, ending with a bare
    # `</think>`. The whole scratchpad rendered in the answer pane and printed
    # onto the card - "This answers. No numbers. Need same language English." -
    # above the actual answer, with no visual distinction between them.
    #
    # That is precisely §11.1: reasoning that reads like a conclusion. It is not
    # acceptable for it to depend on a server flag behaving, so the split is done
    # here as well. Whatever precedes a closing think tag is reasoning, always.
    for tag in ("</think>", "</thinking>", "<|end_of_thought|>"):
        if tag in text:
            head, _, tail = text.partition(tag)
            reasoning = (reasoning + "\n" + head).strip() if reasoning else head.strip()
            text = tail.strip()
            break
    usage = d.get("usage") or {}
    out_tok = usage.get("completion_tokens") or 0

    # WHY A FINISH REASON IS CARRIED RATHER THAN CHECKED HERE.
    # "length" means the model was cut off mid-sentence. That is not an error and
    # must not be raised as one - but an answer that stops in the middle of a dose
    # is worse than no answer, so the caller is told and the SURFACE says so.
    return {
        "role": role,
        "text": text,
        "reasoning": reasoning,
        "finish": (choices[0].get("finish_reason") if choices else None),
        "truncated": (choices[0].get("finish_reason") == "length") if choices else False,
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": out_tok,
        "seconds": round(secs, 2),
        "tokens_per_second": round(out_tok / secs, 2) if secs > 0 and out_tok else None,
        "model": d.get("model"),
    }


def probe(role, n_predict=96):
    """Does it answer AT ALL, right now. A liveness check, not a benchmark.

    THE TOKENS-PER-SECOND FROM THIS FUNCTION IS NOT A SPEED. 24 tokens is a window
    too short to escape the model's own start-up, and on 2026-09-04 a window that
    short reported this GPU at a fifth of its real throughput and sent an evening
    into diagnosing hardware that was fine (BUILD-LOG Phase 2G). It is reported
    here only so a probe that takes 90 seconds is visibly different from one that
    takes 2, and it is labelled at every place it is printed."""
    h = health(role, force=True)
    if not h.get("ok"):
        return h
    try:
        r = chat(role, [{"role": "user", "content": "Reply with the single word: ready"}],
                 n_predict=n_predict, temperature=0.0, timeout=TIMEOUT.get(role))
    except LLMUnavailable as e:
        h["ok"] = False
        h["error"] = e.detail
        return h
    # A THINKING MODEL SPENDS ITS BUDGET THINKING FIRST.
    # With 24 tokens allowed and reasoning enabled, every token went into
    # reasoning_content and `content` came back empty - so a healthy model
    # reported `replied ''` and looked broken. The budget is now large enough to
    # reach an answer, and reasoning counts as a sign of life either way, because
    # the question this function asks is "did it respond", not "what did it say".
    said = r["text"] or (("(thinking only) " + r["reasoning"]) if r["reasoning"] else "")
    h.update({"replied": said[:40], "seconds": r["seconds"],
              # deliberately NOT called tokens_per_second - see the docstring
              "liveness_tok_s": r["tokens_per_second"], "model": r.get("model")})
    return h


def status():
    """Both roles, for /api/health. Never raises, never blocks for long."""
    out = {}
    for role in ENDPOINTS:
        out[role] = health(role)
    return out


def selftest():
    """Cancellation, against a real socket and no model.

    IT MUST NOT NEED GEMMA. A cancellation path that can only be exercised when
    a 31B model is resident is a path nobody runs, and this build already has
    one lesson about a check that quietly does not look. So the test stands up a
    throwaway HTTP server that sleeps, and asserts against IT - the thing being
    tested is this file's socket handling, which is identical whatever is on the
    far end.

    WHAT IS AND IS NOT PROVEN HERE. That the request aborts promptly, that
    Cancelled is raised rather than a generic failure, that a cancel arriving
    first stops the request being sent at all, and that the server observes the
    disconnect. NOT proven: that llama-server reacts to that disconnect by
    freeing its slot. Only the node can show that, by Gemma's CPU falling within
    a second or two of a cancel. Said here so the green line below is not read
    as more than it is."""
    import http.server
    import select
    import socket as _sock
    import socketserver

    bad = 0
    seen = {"disconnected": False}

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            # HOW A CLOSED PEER IS DETECTED, AND HOW IT IS NOT.
            # The first version of this probe wrote b"" and watched for an
            # exception. A zero-byte write is a no-op and raises nothing on a
            # closed socket, so it detected the disconnect never - a check that
            # looked right, ran green in the harness and observed nothing, which
            # is the same defect this file's own cancellation exists to expose.
            # EOF on the READ side is the actual signal: the body is fully read,
            # so anything readable afterwards is either a pipelined request
            # (impossible here) or the peer closing.
            for _ in range(100):            # 10s in 0.1s steps
                time.sleep(0.1)
                r, _w, _x = select.select([self.connection], [], [], 0)
                if r:
                    try:
                        if self.connection.recv(1, _sock.MSG_PEEK) == b"":
                            seen["disconnected"] = True
                            return
                    except OSError:
                        seen["disconnected"] = True
                        return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"choices":[{"message":{"content":"late"}}]}')

        def log_message(self, *a):
            pass

    class Quiet(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            # A broken pipe here is the EXPECTED outcome of the thing under
            # test. Printing its traceback would make a passing run look like a
            # failing one, which is how a green check gets ignored.
            pass

    srv = Quiet(("127.0.0.1", 0), Slow)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1]

    def check(name, ok, detail=""):
        nonlocal bad
        if ok:
            print("  ok    cancel %-34s %s" % (name, detail))
        else:
            bad += 1
            print("  FAIL  cancel %-34s %s" % (name, detail))

    # 1. Cancel while the request is in flight.
    tok = CancelToken()
    box = {}

    def run():
        t0 = time.time()
        try:
            _post(url, {"messages": []}, 30, cancel=tok)
            box["outcome"] = "returned"
        except Cancelled:
            box["outcome"] = "cancelled"
        except Exception as e:
            box["outcome"] = "%s: %s" % (type(e).__name__, e)
        box["seconds"] = time.time() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(1.0)                      # let it get onto the wire
    closed = tok.cancel()
    th.join(timeout=5)

    check("aborts in flight", box.get("outcome") == "cancelled",
          "outcome=%s" % box.get("outcome"))
    check("had a live connection to close", closed)
    check("returns promptly", (box.get("seconds") or 99) < 3.0,
          "%.2fs" % (box.get("seconds") or -1))
    check("thread ended", not th.is_alive())
    time.sleep(0.5)
    check("server saw the disconnect", seen["disconnected"])

    # 2. Cancel that arrives BEFORE the request is sent. The request must never
    #    go out - otherwise a cancel racing a start leaves the slot busy, which
    #    is the whole failure this exists to prevent.
    tok2 = CancelToken()
    tok2.cancel()
    try:
        _post(url, {"messages": []}, 5, cancel=tok2)
        check("pre-cancel is not sent", False, "the POST was sent anyway")
    except Cancelled:
        check("pre-cancel is not sent", True)
    except Exception as e:
        check("pre-cancel is not sent", False, "%s: %s" % (type(e).__name__, e))

    # 3. An uncancelled failure must stay a failure. Disguising a real socket
    #    error as an intentional stop would hide a dead second model behind a
    #    message saying it was deliberately stopped.
    tok3 = CancelToken()
    try:
        _post("http://127.0.0.1:1/v1/chat/completions", {"m": 1}, 2, cancel=tok3)
        check("real error stays an error", False, "no exception")
    except Cancelled:
        check("real error stays an error", False, "raised Cancelled")
    except Exception:
        check("real error stays an error", True)

    srv.shutdown()
    print("\n%d/9" % (9 - bad))
    return bad


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(1 if selftest() else 0)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--probe" in sys.argv or not args:
        bad = 0
        for role in ("primary", "crosscheck"):
            p = probe(role)
            if p.get("ok"):
                print("  %-11s OK   %-22s replied %-8r in %ss"
                      % (role, p.get("model", "?"), p.get("replied", ""),
                         p.get("seconds")))
            else:
                bad += 1
                print("  %-11s DOWN %s\n               %s"
                      % (role, p.get("url"), p.get("error")))
        print("\n%s" % ("both models answering" if not bad
                        else "%d of 2 not answering - the answer pane will say so "
                             "and the source pane still works" % bad))
        print("  (liveness only. For throughput use a real prompt, or "
              "bin/bench-inference.sh - a 24-token window measures start-up.)")
        sys.exit(1 if bad else 0)
    q = " ".join(args)
    r = chat("primary", [{"role": "user", "content": q}], n_predict=200)
    if r["reasoning"]:
        print("--- reasoning (%d chars, NOT the answer) ---\n%s\n"
              % (len(r["reasoning"]), r["reasoning"][:400]))
    print(r["text"])
    print("\n[%s tok in, %s out, %ss, %s t/s%s]"
          % (r["prompt_tokens"], r["completion_tokens"], r["seconds"],
             r["tokens_per_second"], ", TRUNCATED" if r["truncated"] else ""))
