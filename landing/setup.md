# Set up Remembra Relay on this machine

Ask the user before each step that installs or writes anything. Run only the step they said yes to, then show them what it printed.

This guide is for an AI agent (Claude Code, Codex or any other) helping its user. Remembra is one cloud memory for your AI agents. When a session ends, Remembra keeps the facts, not the chatter. The relay hooks read commits, changed and uncommitted files, and unpushed commits from git. For Claude Code and Codex they also read test runs and open todos from the session transcript. An agent that hands off only through the MCP tools records what it reports. The next agent, in another tool or on another machine, starts with a short brief.

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

Tell the user which agents you found. Claude Code's and Codex's session hooks are verified. So are Gemini CLI's, Qwen Code's and Kimi Code's: each was run against the real tool. The hooks for Cursor are unverified: cursor-agent's own hook runner ran them, but no logged-in Cursor session has yet.

## 2. pipx

When `command -v pipx` printed nothing, install it, with the user's yes:

- macOS: `brew install pipx && pipx ensurepath`
- Debian or Ubuntu: `sudo apt install pipx && pipx ensurepath`
- Fedora: `sudo dnf install pipx && pipx ensurepath`
- Anything else: `python3 -m pip install --user pipx && python3 -m pipx ensurepath`

When pipx is already there, still run `pipx ensurepath`, with the user's yes. pipx puts the commands it installs in its own directory (`~/.local/bin` unless `PIPX_BIN_DIR` says otherwise), and a pipx that came from Homebrew or a package manager may never have put that directory on the PATH. `pipx ensurepath` adds it, or says it is there already and changes nothing.

`pipx ensurepath` changes the PATH for new shells only. Ask the user to open a new terminal before step 5. A shell that started earlier, your own included, still won't find the commands: run `pipx environment --value PIPX_BIN_DIR` and put that directory in front of each command, or ask the user to restart you.

## 3. Stop: the user gets a key

Ask the user to create a free key at https://app.remembra.dev/signup (after signing in: "Create a relay key" on Home, or API keys). Wait until they say they have it. Do not ask them to paste it here.

## 4. Install

```bash
pipx install --force 'remembra[mcp]>=0.16'
```

Keep the quotes: zsh reads the brackets as a pattern without them. `--force` also upgrades an older install.

Then check that the commands are found: `command -v remembra-install remembra-relay` prints two paths. If it prints nothing, go back to `pipx ensurepath` in step 2.

## 5. The user saves the key

Ask the user to run this in their own terminal, not through you:

```bash
remembra-install --all
```

It asks for the key at a hidden prompt (it never goes on the command line), shows what it will add to each agent it sets up and writes only after they answer y. It saves the key to `~/.remembra/credentials`, where the relay hooks read it. A first install uses https://api.remembra.dev; on a self-hosted server, the user adds `--url` and their server's URL. Exit code 3 means it showed the changes and wrote nothing.

## 6. Show the hook changes

```bash
remembra-relay connect
```

This is the dry run. Show the user its output and ask before the next step.

## 7. Write the hooks

```bash
remembra-relay connect --apply
```

It leaves the unverified hooks out. Only if the user chooses one of those agents, add its name and `--include-unverified`:

```bash
remembra-relay connect --apply --agent cursor --include-unverified
```

The name is `cursor`.

## 8. Codex: the user trusts the hooks

Codex runs a hook only after the user trusts it. Until then it skips the hook. The interactive Codex CLI asks at startup about hooks that need review; in our `codex exec` runs, Codex skips untrusted hooks without a message. Tell the user: Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust. Open Codex Settings > Hooks, or run `/hooks` in the Codex CLI, and trust SessionStart, UserPromptSubmit and SessionEnd. When a hook's command changes, Codex marks it modified and skips it until you trust it again. `remembra-relay doctor` shows which hooks need it.

## 9. Restart the agents

`remembra-install` added the Remembra MCP server to each verified agent it found: Claude Desktop (on macOS), Claude Code, Codex, Cursor and Gemini CLI. Windsurf is unverified, so `--all` skips it; only `remembra-install --agent windsurf` writes it. Qwen Code and Kimi get their hooks in step 7 but not the MCP server: the block to paste is at https://docs.remembra.dev/guides/relay/#mcp-by-hand. Ask the user to restart the agents it set up so they load it.

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
remembra-install --remove --all --apply --delete-backups
pipx uninstall remembra
rm -r ~/.remembra
```

The second line also deletes the backups of agent configs that still hold the key, and the last line deletes the saved key, the unsent-handoff queue and the log. Then ask the user to revoke the key in the dashboard (Settings > API keys): a copy made anywhere else still works until it is revoked.

## More

- Relay guide: https://docs.remembra.dev/guides/relay/
- Plans and credits: https://docs.remembra.dev/reference/plans-and-credits/
- Help from a person: https://remembra.dev/contact
