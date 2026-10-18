"""Turn a vision model's reply into one operator action.

Split out of main.py so it can be scored on its own (scripts/measure_cycle.py)
and improved through the Seedling loop. README.md in this directory has the
contract.
"""
import json
import re


def parse_action(reply):
    """Return the action in the model's reply. Raises ValueError if there is none."""
    match = re.search(r'\{.*\}', reply, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    return json.loads(reply.strip('`').strip('json').strip())
