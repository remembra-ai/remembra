# Bash parser contract

Consumer: gatecore (WP-3), for PreToolUse on `Bash`, the pre-commit/pre-push identity and the
post-tool check. Vectors: `tests/crew/vectors/bash/corpus.json` (321 commands). Runner:
`run_bash_corpus(parse_fn)`. Vocabulary constants: `remembra.crew.schemas.BASH_*`.

## Output

`parse_fn(command) -> dict` with exactly these keys (the corpus lists only keys that differ from
the defaults):

| Key | Default | Meaning |
|---|---|---|
| `read_only` | `false` | every segment is on the read-only allow-list → fast exit (no snapshot read) |
| `writes` | `[]` | write targets (sorted, unique) |
| `tree_writer` | `false` | a formatter/fixer/codegen with no explicit file target |
| `tree_scope` | `[]` | directories the tree writer covers; `"."` = the whole checkout |
| `tamper` | `[]` | `TAMPER_KINDS` found (sorted, unique) |
| `git_tree_op` | `null` | first tree-wide git op: `checkout_branch, switch, reset_hard, stash, clean, rebase, merge, pull, cherry_pick` |
| `opaque` | `false` | some segment could not be understood → allow it and run the post-tool check |

Paths: relative to the command's starting cwd, POSIX-normalised (`./` dropped, no trailing slash,
`..` kept), absolute paths stay absolute, `~/…` stays literal, `cd` with no argument means `~`.

## Rules

1. **Segments.** Split on `&&`, `||`, `;`, `|`, `&` and newlines, respecting quotes. Heredoc
   bodies are data, not commands. `cd X`/`pushd X`/`popd` change the cwd for the following
   segments (`cd src || exit 1; rm b.ts` → `src/b.ts`). `cd` with a variable or `cd -` makes later
   relative targets unknown → `opaque`. `git -C X` sets the cwd for that git command.
2. **Unwrapping.** Leading `NAME=value` assignments are recorded (tamper check) and removed;
   wrappers `sudo, env, command, builtin, exec, nohup, time, nice, timeout N` and runners
   `npx, bunx, pnpm exec, pnpm dlx, yarn dlx, bun x, npm exec` are stripped (`@biomejs/biome` →
   `biome`).
3. **Redirections** `>`, `>>`, `>|`, `&>`, `&>>`, `1>`, `2>`, `N>>` write their target;
   `/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/tty` and `N>&M` do not. Input `<` and `<<<`
   do not write. `cat > f <<EOF` writes `f`.
4. **Writers.** `tee [-a] F…`; `sed -i`/`-i ''`/`-i.bak`/`--in-place` files after the script;
   `perl -i`/`-pi` files after the script; `truncate`, `dd of=`, `touch`, `mkdir [-p]`, `rm`,
   `rmdir`, `unlink`, `chmod`/`chown`/`chgrp` targets; `cp` and `rsync` destination (or `-t DIR`);
   `mv` sources and destination; `install` destination (`-d` directories); `ln`: link and source
   (a symlink source is resolved relative to the link's directory: `ln -s ../shared/m.ts
   src/app/pos/m.ts` → `src/app/pos/m.ts`, `src/app/shared/m.ts`); `patch F` (explicit file);
   `sort -o F`; `curl -o F`, `wget -O F`; `git checkout [<ref>] -- P…`, `git checkout .`,
   `git checkout <file-with-extension>`; `git restore [--source X] [--staged] P…`; `git rm P…`;
   `git mv A B`; `prisma migrate dev` → `prisma/migrations`.
5. **Tree writers.** `prettier --write|-w`, `eslint --fix`, `biome format|check|lint
   --write|--apply|--fix`, `ruff format`, `ruff check --fix`, `black` (not `--check`/`--diff`),
   `gofmt -w`, `go fmt`, `cargo fmt`, `prisma generate`, `supabase gen types`,
   `openapi-generator[-cli] generate` (scope = `-o DIR`), `graphql-codegen`, and package-manager
   scripts (`npm run X`, `pnpm [run] X`, `yarn [run] X`, `bun run X`) whose name contains `format`,
   `lint`, `fix`, `gen` or `codegen`. Scope: directory arguments (a last component without an
   extension, or `.`); none → `["."]`; a glob or variable argument → `["."]`. A file argument (has
   an extension) is a plain write, not a tree write.
6. **Git tree ops.** `git checkout <branch>` (no `--`, not `.`, last component without an
   extension), `checkout -b`, `switch [-c]`, `reset --hard`, `stash [push|pop|apply|drop|save|clear]`
   (`stash list|show` are read-only), `clean -f…` (`clean -n` is read-only), `rebase`, `merge`,
   `pull`, `cherry-pick`. `git reset` without `--hard` is opaque.
7. **Read-only.** `BASH_READ_ONLY_COMMANDS` (without their write flags: `find` without
   `-exec/-execdir/-ok/-delete/-fprint*`, `sort` without `-o`, `sed` without `-i`, linters without
   fix flags), git subcommands in `BASH_READ_ONLY_GIT` (including `config --get`/`config <key>`
   with no value), and the test runners in `BASH_TEST_RUNNERS`. A whole command is `read_only` only
   when every segment is and nothing is redirected to a file.
8. **Opaque.** `eval`, `bash|sh|zsh -c` (not recursively parsed), `python -c`, `node -e`,
   scripts (`./x.sh`, `bash x.sh`, `node x.js`, `python x.py`), `make`, `xargs`, `find -exec`,
   `find -delete`, subshells `( … )`, groups `{ …; }`, `$(…)`/backticks, and any writer target
   containing a variable, a glob (`* ? [`) or braces. Everything not otherwise classified (e.g.
   `npm install`, `git commit`, `git push`, `git add`, `curl` without `-o`, `awk`, `kill 123`) is
   opaque too. Known builtins with no file effect (`export`, `unset`, `set`, `alias`, `cd`,
   `pushd`, `popd`, `exit`) are neither read-only nor opaque.
9. **Tamper** (row 2), structured detection:
   * `env_crew_var`: assigning (`X=…` prefix, `export`, `env X=`) or `unset`-ting
     `REMEMBRA_CREW`, `REMEMBRA_CREW_SESSION`, `REMEMBRA_BYPASS` — except `REMEMBRA_BYPASS=<code>`
     in the literal form `RCB-XXXXX-XXXXX` as an inline prefix of a `git` command;
   * `no_verify`: `--no-verify` on `git commit|push|merge|rebase|am`, `-n` on `git commit`
     including combined short flags (`-nm`, `-anm`); a flag that takes a value ends the cluster
     (`-mn` is the message "n"), and `git push -n` is a dry run;
   * `hooks_path`: `git -c core.hooksPath=…` (key case-insensitive), `git config [scope]
     core.hooksPath <value>`, `--unset`, `--unset-all`, `--add`, `--replace-all`;
   * `husky_off`: `HUSKY=0`, `HUSKY_SKIP_HOOKS=…`; `lefthook_off`: `LEFTHOOK=0`, `LEFTHOOK_EXCLUDE=…`;
   * `crewd_kill`: `kill`/`pkill`/`killall` naming crewd, `launchctl bootout|unload|remove|kill|disable`
     of the crewd label (`dev.remembra.crewd`), `systemctl --user stop|disable|kill|mask` of
     `remembra-crewd`;
   * `crew_files_removed`: `rm`, `unlink`, `mv` (as source), `truncate`, `chmod`/`chown` of
     `.remembra/…`, `~/.remembra/…` or `.git/hooks/…`.

   Plus a **raw scan of opaque segments** (including a heredoc fed to a shell) for the markers in
   `BASH_TAMPER_SCAN` (`crewd` and crew paths only together with a kill/removal verb). Read-only
   segments are not scanned, so `grep -- --no-verify docs` stays read-only, and arguments are not
   assignments, so `echo "HUSKY=0" > notes.md` is a plain write.
