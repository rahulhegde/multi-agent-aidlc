import json
import sys


def main():
    tasks = []
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Expected a JSON object with an action")
            action = request.get("action")
            if action == "add":
                title = request.get("title")
                if not isinstance(title, str) or not title.strip():
                    raise ValueError("Provide a nonempty title string")
                task = {"id": len(tasks) + 1, "title": title.strip(), "completed": False}
                tasks.append(task)
                result = task
            elif action == "list":
                result = tasks
            elif action == "complete":
                task_id = request.get("id")
                if type(task_id) is not int or task_id <= 0:
                    raise ValueError("Provide a positive integer task id")
                task = next((item for item in tasks if item["id"] == task_id), None)
                if task is None:
                    raise ValueError("Task id does not exist; list tasks first")
                task["completed"] = True
                result = task
            else:
                raise ValueError("Use action add, list, or complete")
        except (ValueError, TypeError) as error:
            result = {"error": str(error)}
        print(json.dumps(result))


if __name__ == "__main__":
    main()
