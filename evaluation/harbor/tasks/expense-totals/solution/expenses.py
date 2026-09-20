import json
import re
import sys
from decimal import Decimal, localcontext


def summarize(request):
    if not isinstance(request, dict) or not isinstance(request.get("expenses"), list):
        raise ValueError("Provide a JSON object with an expenses list")
    totals = {}
    # Enough decimal precision for every digit in this bounded input line.
    with localcontext() as context:
        context.prec = max(28, len(json.dumps(request)))
        for expense in request["expenses"]:
            if not isinstance(expense, dict):
                raise ValueError("Each expense must be an object")
            category = expense.get("category")
            amount = expense.get("amount")
            if not isinstance(category, str) or not category.strip():
                raise ValueError("Provide a nonempty category string")
            if not isinstance(amount, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", amount):
                raise ValueError("Provide a nonnegative decimal string with at most two places")
            category = category.strip()
            totals[category] = totals.get(category, Decimal("0")) + Decimal(amount)
        return {
            "total": f"{sum(totals.values(), Decimal('0')):.2f}",
            "by_category": {key: f"{value:.2f}" for key, value in totals.items()},
        }


def main():
    for line in sys.stdin:
        try:
            result = summarize(json.loads(line))
        except (ValueError, TypeError) as error:
            result = {"error": str(error)}
        print(json.dumps(result))


if __name__ == "__main__":
    main()
