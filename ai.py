"""AI layer: Gemini LLM for summaries + chat, with a rule-based fallback.

Only test values are ever sent to the LLM - never names, emails or other personal data.
"""
import os
import logging
import requests

log = logging.getLogger("uroscan.ai")
DISCLAIMER = "UroScan is a screening aid, not a diagnosis. Please confirm any abnormal result with a clinician."

SYSTEM = ("You are UroScan's assistant. You explain urine dipstick screening results in simple, calm, "
          "plain English for a patient. Never diagnose, never prescribe, never give doses. Mention possible "
          "common causes only as possibilities and recommend seeing a clinician (doctor) for anything abnormal. "
          "Keep answers short (under 120 words), no markdown headings.")


def _gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    models = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-latest", "gemini-2.5-flash"] if m]
    for model in dict.fromkeys(models):                 # try each model name once, in order
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        body = {"system_instruction": {"parts": [{"text": SYSTEM}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1024,
                                     "thinkingConfig": {"thinkingBudget": 0}}}
        try:
            r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=25)
            if r.status_code in (400, 404):              # unknown model / option -> try the next one
                log.warning("Gemini %s: %s %s", model, r.status_code, r.text[:200])
                body["generationConfig"].pop("thinkingConfig", None)
                r = requests.post(url, json=body, headers={"x-goog-api-key": key}, timeout=25)
                if r.status_code in (400, 404):
                    continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
            if text:
                return text
        except Exception as e:
            log.warning("Gemini call failed (%s): %s", model, e)
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


def chat(question, history_rows, latest):
    """history_rows: list of (date, analyte, level, status) for this patient only."""
    context = "\n".join(f"{d} | {a} | {l} | {s}" for d, a, l, s in history_rows[-120:])
    prompt = (f"The patient's recent UroScan results (date | test | level | status):\n{context}\n\n"
              f"Patient question: {question}\nAnswer using only this data.")
    text = _gemini(prompt)
    if text:
        return text
    # simple fallback
    q = question.lower()
    if latest:
        abn = [r for r in latest if r["status"] != "Normal"]
        if "compare" in q or "trend" in q or "change" in q:
            return ("Use the graph on your dashboard and pick a test to see how it changed over time. "
                    "AI chat is offline right now, so I can only give a basic answer.")
        if abn:
            return ("In your latest scan: " + ", ".join(f"{r['analyte']} {r['level_label']}" for r in abn)
                    + ". " + DISCLAIMER)
        return "Your latest scan has all readings in the normal range. " + DISCLAIMER
    return "You have no scans yet. Go to 'New scan' to upload a strip photo."
