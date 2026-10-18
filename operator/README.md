# Operator

The operator is Seedling's vision-model user. Each step it takes a screenshot of its ZenCode instance, asks a vision-language model for one action, and performs that action with Playwright. It has no shell and no API into ZenCode.

| File | What it is |
|---|---|
| `main.py` | The loop, the control page's API and the request guard |
| `action_parser.py` | `parse_action(reply)`: turns the model's reply into one action |
| `static/` | The control page |

## Actions

The model is asked to answer with one JSON object:

```
{"action": "click", "selector": "button:has-text('Connect')"}
{"action": "fill", "selector": "input[type='url']", "value": "text to type"}
{"action": "wait", "ms": 2000}
{"action": "done"}
```

## The parser contract

`parse_action(reply: str) -> dict` returns one action. The action must have these keys with these types; other keys the model sent may stay.

| `action` | Required keys |
|---|---|
| `"click"` | `selector`: a non-empty string |
| `"fill"` | `selector`: a non-empty string; `value`: a string |
| `"wait"` | `ms`: an integer (`2000` when the model leaves it out) |
| `"done"` | none |

`action` is always lower case. If the reply holds no usable action, `parse_action` raises `ValueError`, and the loop logs the reply and waits three seconds (see `main.py`). It must never return an action the loop cannot perform, and a bad reply must never raise anything except `ValueError`. When a reply holds several candidate objects, the first valid action wins.

## Known parser failures (the improvement target)

Small local vision models rarely answer with clean JSON. The current parser takes everything from the first `{` to the last `}` and calls `json.loads`, so it fails or misbehaves when:

- prose after the JSON contains a brace, or the reply holds a second JSON object (an alternative, or a repeat);
- a `<think>...</think>` block before the answer contains braces;
- the object uses single quotes, curly quotes or a trailing comma;
- `action` is capitalised (`"Click"`), or `ms` is a string (`"2000"`) or missing;
- the reply is valid JSON but not a usable action: a `click` with no selector, an unknown action such as `"scroll"`, an object with no `action`, or a bare string. Today these are returned as they are and the loop silently does nothing, instead of `ValueError` being raised.

## How it is scored

`scripts/measure_cycle.py` runs `parse_action` over a fixed corpus of model replies kept in `tests/fixtures/`, and runs the smoke tests. The corpus is not given to ZenCode, and nothing under `operator/` can change it.

## Rules for changes

- Standard library only: `action_parser.py` must import on Python 3.12 with nothing installed.
- Keep the name and signature `parse_action(reply)`.
- The parser reads no files, opens no network connections and starts no processes.
