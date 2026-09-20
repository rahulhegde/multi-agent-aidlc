# Offline expense totals

Idea: Build an offline expense summarizer with exact decimal totals and useful validation errors.

Create `/app/expenses.py` using Python's standard library. It reads one JSON
object per stdin line and writes one JSON object per line. An input is
`{"expenses":[{"category":" Food ","amount":"0.10"},{"category":"Food","amount":"0.20"}]}`.
The output is `{"total":"0.30","by_category":{"Food":"0.30"}}`.

Categories must be nonempty strings after trimming. Amounts must be strings of
ASCII digits with an optional decimal point and one or two fractional digits.
Reject signs, exponent notation, numeric JSON amounts, and nonfinite values.
Use decimal arithmetic; all output amounts have exactly two fractional digits.
An empty expenses list returns `{"total":"0.00","by_category":{}}`.

Malformed JSON, non-object input, a missing/non-list expenses field, non-object
items, and invalid or missing categories/amounts return
`{"error":"<actionable nonempty explanation>"}`. Never emit a partial total for
an invalid request; continue processing subsequent input lines independently.

No files, network calls, external dependencies, shell commands, or dynamic code
evaluation. Do not write diagnostic text to stdout or require a terminal.
