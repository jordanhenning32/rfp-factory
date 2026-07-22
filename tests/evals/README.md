# Compliance review gold set

`fixtures/compliance_review_gold_v1.json` is synthetic and contains no client
or solicitation data. It covers classification errors, headers/truncation,
duplicates, inherited parent context, submission rules, forms, evaluation
weights, pricing, and intentional source omissions.

Normal regression tests validate the corpus and scorer without network calls.
Live comparisons are opt-in:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_compliance_models.py --live `
  --models gemini-2.5-pro claude-haiku-4-5-20251001 --runs 3
```

Do not replace the configured Gemini reviewer merely because Haiku is cheaper.
The candidate gate requires no critical misses, 100% structured-call success,
no unsafe HIGH corrections or omission auto-adds, and classification, omission,
and omission-metadata metrics within five percentage points of the primary over
repeated runs.
