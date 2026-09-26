# Set up Remembra Relay on this machine

Ask the user before each step that installs or writes anything. Run only the step they said yes to, then show them what it printed.

This guide is for an AI agent (Claude Code, Codex or any other) helping its user. Remembra is one cloud memory for all your AI agents. When a session ends, Remembra keeps the facts, not the chatter: commits, changed files, tests that passed or failed, what is still open. The next agent, in any tool and on any machine, starts with a short brief.

## Rules

- Never ask for the API key in this chat. Never put a key on a command line, in a file or in an environment variable you set. The user types it into remembra-install's hidden prompt, in their own terminal.
- Never edit hook files by hand. `remembra-relay connect --apply` writes them and keeps a backup of each file.
- `remembra-relay connect` without `--apply` is a dry run: it shows the changes and writes nothing.
- Windows setup has not been tested. On Windows, stop and send the user to https://docs.remembra.dev/guides/relay/.
- Quote prices only from https://remembra.dev/pricing. Never compute them.

## 1. Look at this machine

```bash
uname -s
echo "$SHELL"
command -v claude codex cursor-agent cursor gemini qwen kimi pipx
```

Tell the user which agents you found. Claude Code's and Codex's session hooks are verified. The hooks for Cursor, Gemini CLI, Qwen Code and Kimi are unverified: they follow each tool's docs but have not been run against it yet.

## 2. pipx

Skip this step when `command -v pipx` printed a path. Otherwise, with the user's yes:

- macOS: `brew install pipx && pipx ensurepath`
- Debian or Ubuntu: `sudo apt install pipx && pipx ensurepath`
- Fedora: `sudo dnf install pipx && pipx ensurepath`
- Anything else: `python3 -m pip install --user pipx && python3 -m pipx ensurepath`

`pipx ensurepath` changes the PATH for new shells only. Open a new shell before the next step.

## 3. Stop: the user gets a key

Ask the user to create a free key at https://app.remembra.dev/signup (after signing in: "Create a relay key" on Home, or API keys). Wait until they say they have it. Do not ask them to paste it here.

## 4. Install

```bash
pipx install --force 'remembra[mcp]>=0.16'
```

Keep the quotes: zsh reads the brackets as a pattern without them. `--force` also upgrades an older install.

## 5. The user saves the key

Ask the user to run this in their own terminal, not through you:

```bash
remembra-install --all
```

It asks for the key at a hidden prompt (it never goes on the command line), shows what it will add to each agent it finds and writes only after they answer y. It saves the key to `~/.remembra/credentials`, where the relay hooks read it. A first install uses https://api.remembra.dev; on a self-hosted server, the user adds `--url` and their server's URL. Exit code 3 means it showed the changes and wrote nothing.

## 6. Show the hook changes

```bash
remembra-relay connect
```

This is the dry run. Show the user its output and ask before the next step.

## 7. Write the hooks

```bash
remembra-relay connect --apply
```

It leaves the unverified hooks out. Only if the user chooses one of those agents, add its name and `--include-unverified`, for example:

```bash
remembra-relay connect --apply --agent gemini --include-unverified
```

The names are `cursor`, `gemini`, `qwen` and `kimi`.

## 8. Codex: the user trusts the hooks

Codex runs a hook only after the user trusts it. Codex skips untrusted hooks without a message. Tell the user: Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust. Open Codex Settings > Hooks, or run `/hooks` in the Codex CLI, and trust SessionStart, UserPromptSubmit and SessionEnd. Codex asks again whenever a hook's command changes.

## 9. Restart the agents

`remembra-install` added the Remembra MCP server to each agent it found. Ask the user to restart those agents so they load it.

## 10. Check

Whenever handoffs don't arrive, run `remembra-relay doctor`. Run it now.

```bash
remembra-relay doctor
```

Doctor arrived in remembra 0.16.1. When `remembra-relay --help` does not list it, run `pipx run --spec 'remembra>=0.16.1' remembra-relay doctor` instead. Doctor reads this machine's files and the trail, changes nothing and prints the one fix for each problem. It checks the key with the server, the handoffs waiting to be sent, each agent's hooks and the last result per agent. Show its output as printed. Offer its fixes one at a time, and run one only after the user says yes to it. Right after setup it names each agent that hasn't ended a session with the hooks yet: step 11 is that session.

## 11. Hand off

End this session. The next agent, even a different one, starts with the brief.

## The same steps in the user's terminal

After the key (step 3), these three lines do steps 4, 5 and 7:

```bash
pipx install --force 'remembra[mcp]>=0.16'
remembra-install --all
remembra-relay connect --apply
```

## Taking it off again

In this order:

```bash
remembra-relay disconnect --apply
remembra-install --remove --all --apply
pipx uninstall remembra
rm -r ~/.remembra
```

The last line deletes the saved key, the unsent-handoff queue and the log.

## More

- Relay guide: https://docs.remembra.dev/guides/relay/
- Plans and credits: https://docs.remembra.dev/reference/plans-and-credits/
- Help from a person: https://remembra.dev/contact
