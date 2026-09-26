# Zone command grammar (D38)

Zone `commands` are **argv-prefix token patterns**, never regexes. The gate never runs a user
regex; crewd compiles patterns into a token trie (WP-3), the server validates them (WP-5).
Vectors: `tests/crew/vectors/grammar/command_patterns.json`. Reference:
`validate_command_pattern`, `validate_command_patterns`, `command_pattern_matches`.

## Syntax

* Tokens separated by single spaces; no leading/trailing space, tabs or newlines.
* A token is a literal (`[A-Za-z0-9._:/@=+,%-]{1,64}`) or exactly `*`. `*` inside a token
  (`vercel*`, `deploy:*`) is rejected, as is anything regex-like (`^ $ ( ) [ ] { } | ? \ ' "`).
* The first token must be a literal (the command name).
* ≤16 tokens, ≤256 characters, ≤20 patterns per zone.

## Matching

Match against the **normalised argv** of one segment (env assignments, wrappers and package
runners stripped, as in [bash-parser.md](bash-parser.md)). The pattern must be a prefix of argv:
a literal token equals the argv token (case-sensitive); a non-final `*` matches exactly one token;
a final `*` matches zero or more tokens (so `supabase db push *` ≡ `supabase db push`). Options
are ordinary tokens: `supabase db push` does not match `supabase --debug db push`.
Matching is linear in the pattern length.
