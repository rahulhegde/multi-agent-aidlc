# Offline task list

Idea: Build a tiny offline task list that accepts JSON commands and explains invalid input.

Create `/app/todo.py` using Python's standard library. Running `python /app/todo.py`
reads one JSON object per input line and writes one JSON value per line. Keep state
in memory for this process only. No files, network calls, external dependencies,
shell commands, or dynamic code evaluation.

- `{"action":"add","title":" Homework "}` returns
  `{"id":1,"title":"Homework","completed":false}`. IDs start at 1 and increase
  only after a successful add. Titles must be nonempty strings after trimming.
- `{"action":"list"}` returns all tasks in insertion order, initially `[]`.
- `{"action":"complete","id":1}` returns the task with `completed:true`.
  Repeating completion succeeds. IDs must be positive integers; booleans are invalid.
- Malformed JSON, non-object input, unknown actions, missing or invalid fields,
  and unknown task IDs return `{"error":"<actionable nonempty explanation>"}`.
  The process continues and invalid requests leave state unchanged.

Do not write diagnostic text to stdout or require an interactive terminal.
