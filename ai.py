"""AI layer: Gemini LLM for summaries + chat, with a rule-based fallback.

Only test values are ever sent to the LLM - never names, emails or other personal data.
"""
import os
import re
import time
import logging
import requests

log = logging.getLogger("uroscan.ai")
DISCLAIMER = "UroScan is a screening aid, not a diagnosis. Please confirm any abnormal result with a clinician."
API = "https://generativelanguage.googleapis.com/v1beta"

SYSTEM = ("You are UroScan's assistant inside a urine test-strip screening app. Explain dipstick results in simple, "
          "calm, plain English. Never diagnose, never prescribe, never give doses. Mention common causes only as "
          "possibilities and suggest seeing a clinician for anything abnormal. Use only the data you are given. "
          "Answer in 2-5 short sentences or a few short bullet points, no headings, no bold text.")

_models = {"list": None, "at": 0}


def _available_models(key):
    """Ask Google which 'flash' models this key can use (cached for 6 hours), newest first, lite models last."""
    if _models["list"] and time.time() - _models["at"] < 6 * 3600:
        return _models["list"]
    names = []
    try:
        r = requests.get(f"{API}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=10)
        r.raise_for_status()
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in name \
                    and not any(x in name for x in ("image", "tts", "audio", "live", "exp", "preview", "thinking")):
                names.append(name)
    except Exception as e:
        log.warning("Could not list Gemini models: %s", e)

    def rank(n):
        version = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", n)[:1]] or [0]
        return ("lite" in n, -version[0], "latest" not in n)
    names = sorted(set(names), key=rank)
    preferred = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest"] if m]
    ordered = list(dict.fromkeys(preferred + names + ["gemini-flash-lite-latest"]))
    _models.update(list=ordered, at=time.time())
    log.info("Gemini models to try: %s", ", ".join(ordered[:5]))
    return ordered


def _gemini(prompt, turns=None):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    contents = []
    for t in turns or []:                               # earlier chat turns, so follow-up questions make sense
        role = "model" if t.get("role") == "bot" else "user"
        text = str(t.get("text", ""))[:600]
        if text:
            contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": prompt}]})
    body = {"system_instruction": {"parts": [{"text": SYSTEM}]}, "contents": contents,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 600}}
    for model in _available_models(key)[:4]:
        for attempt in range(2):                         # one quick retry if Google says it's busy (503 / 429)
            try:
                r = requests.post(f"{API}/models/{model}:generateContent", json=body,
                                  headers={"x-goog-api-key": key}, timeout=25)
                if r.status_code in (429, 500, 503) and attempt == 0:
                    time.sleep(1.2)
                    continue
                if r.status_code != 200:
                    log.warning("Gemini %s: %s %s", model, r.status_code, r.text[:160])
                    break
                parts = r.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return re.sub(r"\*\*(.+?)\*\*", r"\1", text).replace("### ", "").replace("## ", "")
                break
            except Exception as e:
                log.warning("Gemini call failed (%s): %s", model, e)
                break
    return None


def ai_available():
    return bool(os.environ.get("GEMINI_API_KEY"))


# ---------------------------------------------------------------- rule-based fallback
HINTS = {
    "Glucose": "sugar in the urine, which can be linked to high blood sugar",
    "Protein": "protein in the urine, which can happen with dehydration, exercise or kidney stress",
    "Leukocytes": "white blood cells, a common sign of infection or inflammation",
    "Nitrite": "nitrite, often produced by bacteria in a urinary tract infection",
    "Blood": "blood, which can come from infection, stones or menstruation",
    "Ketones": "ketones, seen with fasting, low-carb diets or uncontrolled diabetes",
    "Bilirubin": "bilirubin, which can point to a liver issue",
    "Urobilinogen": "raised urobilinogen, sometimes seen with liver conditions",
    "pH": "an unusual pH (acidity)",
    "Specific Gravity": "unusual urine concentration (hydration level)",
}
ALIASES = {"sugar": "Glucose", "sg": "Specific Gravity", "gravity": "Specific Gravity", "ph": "pH",
           "wbc": "Leukocytes", "white": "Leukocytes", "rbc": "Blood", "nitrites": "Nitrite"}


def rule_summary(results):
    abnormal = [r for r in results if r["status"] != "Normal"]
    if not abnormal:
        return "All 10 readings are within the normal range. Nothing stands out in this screening. " + DISCLAIMER
    bits = [f"{r['analyte']} is {r['status'].lower()} ({r['level_label']}), meaning {HINTS.get(r['analyte'], 'an unusual value')}"
            for r in abnormal]
    extra = ""
    names = {r["analyte"] for r in abnormal}
    if {"Leukocytes", "Nitrite"} <= names:
        extra = " Leukocytes and nitrite together are a common pattern in urinary tract infections."
    elif {"Glucose", "Ketones"} <= names:
        extra = " Glucose with ketones together should be checked for blood-sugar control."
    return ("; ".join(bits) + "." + extra + " The other readings look normal. " + DISCLAIMER)


def summarise(results):
    """results: list of dicts with analyte, level_label, status."""
    lines = "\n".join(f"- {r['analyte']}: {r['level_label']} ({r['status']})" for r in results)
    text = _gemini("Explain these urine test strip results to the patient in 3-5 sentences:\n" + lines)
    if text:
        return text + ("" if ("doctor" in text.lower() or "clinician" in text.lower()) else "\n\n" + DISCLAIMER), "gemini"
    return rule_summary(results), "rules"


def _mentioned(question):
    q = question.lower()
    found = [a for a in HINTS if a.lower() in q]
    found += [a for k, a in ALIASES.items() if re.search(rf"\b{k}\b", q)]
    return list(dict.fromkeys(found))


def _fallback(question, history_rows, latest):
    """A useful answer without the AI: talks about the tests the question mentions, or the latest scan."""
    if not latest:
        return "There are no scans yet. Go to 'New scan' to read a strip, then ask me about it."
    q = question.lower()
    by_test = {}
    for d, a, l, s in history_rows:
        by_test.setdefault(a, []).append((d, l, s))
    tests = _mentioned(question)
    if tests:
        out = []
        for t in tests:
            seen = by_test.get(t, [])
            if not seen:
                continue
            d, l, s = seen[-1]
            line = f"{t}: {l} ({s}) on {d}"
            if len(seen) > 1:
                pd, pl, ps = seen[-2]
                line += f" - before that {pl} ({ps}) on {pd}"
            if s != "Normal":
                line += f". This can mean {HINTS[t]}"
            out.append(line + ".")
        if out:
            return "\n".join(out) + "\n" + DISCLAIMER
    abn = [r for r in latest if r["status"] != "Normal"]
    if any(w in q for w in ("worse", "trend", "change", "compare", "better", "improv")):
        changes = []
        for t, seen in by_test.items():
            if len(seen) > 1 and seen[-1][1] != seen[-2][1]:
                changes.append(f"{t} went from {seen[-2][1]} to {seen[-1][1]}")
        if changes:
            return "Since your previous scan: " + "; ".join(changes) + ". The trend graph shows each test over time."
        return "Nothing changed between your last two scans." if len(set(d for d, *_ in history_rows)) > 1 \
            else "You have only one scan so far - scan again later to see a trend."
    if any(w in q for w in ("next", "do", "should", "advice")):
        if abn:
            return ("Some results are outside the normal range (" + ", ".join(r["analyte"] for r in abn) +
                    "). Drink water normally, repeat the test in a few days with a fresh strip, and show the report to a "
                    "clinician - especially if you have symptoms. " + DISCLAIMER)
        return "Everything looks normal. Keep scanning now and then to track your trend. " + DISCLAIMER
    if abn:
        return rule_summary(latest)
    return "Your latest scan has all 10 readings in the normal range. " + DISCLAIMER


def chat(question, history_rows, latest, turns=None, audience="patient"):
    """history_rows: list of (date, analyte, level, status) for this patient only."""
    context = "\n".join(f"{d} | {a} | {l} | {s}" for d, a, l, s in history_rows[-120:])
    who = "a clinician reviewing this patient" if audience == "clinician" else "the patient"
    prompt = (f"UroScan results for this patient (date | test | level | status), oldest first:\n{context or 'no scans yet'}\n\n"
              f"Question from {who}: {question}")
    return _gemini(prompt, turns) or _fallback(question, history_rows, latest)
