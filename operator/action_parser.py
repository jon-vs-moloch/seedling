import json

def parse_action(reply):
    start = reply.find('{')
    if start == -1:
        raise ValueError("No { found in reply.")
    end = reply.rfind('}')
    if end == -1:
        raise ValueError("No } found in reply.")
    
    content = reply[start:end + 1]
    
    for i in range(1, len(content) + 1):
        start = content.find('{', 0, i)
        end = content.rfind('}', 0, i)
        if start == -1 or end == -1:
            continue
        
        chunk = content[start:end + 1]
        try:
            action = json.loads(chunk)
            return action
        except json.JSONDecodeError:
            pass
        
    raise ValueError("No valid JSON object found in reply.")

# Example usage:
# action = parse_action("Some text {"{"key": "value"}", more text")
# print(action)