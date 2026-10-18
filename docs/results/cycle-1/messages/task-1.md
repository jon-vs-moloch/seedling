Your sandbox holds two files copied from Seedling's operator: README.md (the operator's docs) and action_parser.py (its reply parser, the function parse_action).

Task: rewrite action_parser.py so that parse_action meets the contract in README.md (section "The parser contract") and handles every case listed under "Known parser failures".

Rules:
- Change only action_parser.py. Do not edit README.md.
- Standard library only. Keep the function name and signature parse_action(reply).
- For a reply with no usable action, raise ValueError. Never raise another exception for a bad reply, and never return an action the operator cannot perform.

Steps:
1. Read README.md and action_parser.py with read_file.
2. Write the complete new action_parser.py with write_file.
3. Check it: write a small script check_parser.py that calls parse_action on at least one example of each failure listed in README.md and prints each result, then run it with run_bash: python3 check_parser.py
4. Fix action_parser.py until every check gives the right answer.
5. Delete check_parser.py and any other scratch files (run_bash: rm -f check_parser.py), so the sandbox holds only README.md and action_parser.py. Whatever is in the sandbox when you finish is shipped.
6. Reply with a short summary of what you changed.
