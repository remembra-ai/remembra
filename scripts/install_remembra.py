#!/usr/bin/env python3
"""
Remembra Universal Agent Installer
Detects macOS/Linux, finds ALL installed AI agents, and injects MCP configuration.
"""

import os
import json
import argparse
from pathlib import Path

# Centralized paths
REMEMBRA_HOME = Path.home() / ".remembra"
CONFIG_FILE = REMEMBRA_HOME / "config.toml"

# Agent Config Paths
AGENTS = {
    "claude_desktop": Path.home() / "Library/Application Support/Claude/claude_desktop_config.json",
    "claude_code": Path.home() / ".claude/settings.json",
    "gemini": Path.home() / ".gemini/settings.json",
    "codex": Path.home() / ".codex/config.toml",  # Note: TOML format
    "cursor": Path.home() / ".cursor/mcp.json",
    "windsurf": Path.home() / ".windsurf/mcp_config.json",
}

def generate_mcp_config(api_key: str, project_id: str, user_id: str = "default"):
    """Generates the universal MCP block."""
    return {
        "mcpServers": {
            "remembra": {
                "command": "remembra-mcp",
                "env": {
                    "REMEMBRA_URL": "https://api.remembra.dev",
                    "REMEMBRA_API_KEY": api_key,
                    "REMEMBRA_PROJECT": project_id,
                    "REMEMBRA_USER_ID": user_id
                }
            }
        }
    }

def install_to_json_agent(agent_name: str, config_path: Path, mcp_config: dict):
    """Merges MCP config safely into JSON-based agents."""
    if not config_path.parent.exists():
        print(f"[-] {agent_name} not detected (directory missing). Skipping.")
        return False
    
    # Load existing or create new
    current_config = {}
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                current_config = json.load(f)
        except json.JSONDecodeError:
            print(f"[!] Warning: {config_path} was corrupted. Backing up.")
            config_path.rename(config_path.with_suffix(".json.bak"))
    
    # Deep merge - preserve existing mcpServers
    if "mcpServers" not in current_config:
        current_config["mcpServers"] = {}
    current_config["mcpServers"]["remembra"] = mcp_config["mcpServers"]["remembra"]
    
    with open(config_path, "w") as f:
        json.dump(current_config, f, indent=2)
    
    print(f"[+] ✅ {agent_name} configured!")
    return True

def install_to_codex(config_path: Path, api_key: str, project_id: str, user_id: str):
    """Special handler for Codex TOML format."""
    if not config_path.exists():
        print(f"[-] Codex not detected. Skipping.")
        return False
    
    # Read existing TOML
    with open(config_path, "r") as f:
        content = f.read()
    
    # Check if remembra already configured
    if "[mcp_servers.remembra]" in content:
        print(f"[*] Codex already has Remembra configured. Updating...")
        # Remove old config (simple approach)
        lines = content.split("\n")
        new_lines = []
        skip_until_next_section = False
        for line in lines:
            if line.strip().startswith("[mcp_servers.remembra]"):
                skip_until_next_section = True
                continue
            if skip_until_next_section and line.strip().startswith("["):
                skip_until_next_section = False
            if not skip_until_next_section:
                new_lines.append(line)
        content = "\n".join(new_lines)
    
    # Append new config
    toml_block = f'''
# MCP Servers - Remembra Shared Memory
[mcp_servers.remembra]
command = "remembra-mcp"

[mcp_servers.remembra.env]
REMEMBRA_URL = "https://api.remembra.dev"
REMEMBRA_API_KEY = "{api_key}"
REMEMBRA_PROJECT = "{project_id}"
REMEMBRA_USER_ID = "{user_id}"
'''
    
    with open(config_path, "a") as f:
        f.write(toml_block)
    
    print(f"[+] ✅ Codex configured!")
    return True

def create_credentials_file(api_key: str, project_id: str, user_id: str):
    """Create centralized credentials file."""
    REMEMBRA_HOME.mkdir(exist_ok=True)
    
    creds_content = f'''# Remembra Credentials
# Created by install_remembra.py

[credentials]
api_key = "{api_key}"
url = "https://api.remembra.dev"

[defaults]
project_id = "{project_id}"
user_id = "{user_id}"
'''
    
    creds_file = REMEMBRA_HOME / "credentials"
    with open(creds_file, "w") as f:
        f.write(creds_content)
    
    # Secure the file
    os.chmod(creds_file, 0o600)
    print(f"[+] ✅ Credentials saved to {creds_file}")

def main():
    parser = argparse.ArgumentParser(
        description="🧠 Remembra Universal Agent Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python install_remembra.py --api-key rem_xxx --project myproject
  python install_remembra.py --api-key rem_xxx --project default --user-id user_123
        """
    )
    parser.add_argument("--api-key", required=True, help="Your Remembra API Key")
    parser.add_argument("--project", default="default", help="Project ID for memory isolation")
    parser.add_argument("--user-id", default="default", help="User ID for memory isolation")
    args = parser.parse_args()
    
    print("🧠 Remembra Universal Agent Installer")
    print("=" * 50)
    
    # Create centralized credentials
    create_credentials_file(args.api_key, args.project, args.user_id)
    
    # Generate MCP config
    mcp_block = generate_mcp_config(args.api_key, args.project, args.user_id)
    
    # Install to each agent
    configured = 0
    
    # JSON-based agents
    for agent_name in ["claude_desktop", "claude_code", "gemini", "cursor", "windsurf"]:
        if install_to_json_agent(agent_name, AGENTS[agent_name], mcp_block):
            configured += 1
    
    # TOML-based agents (Codex)
    if install_to_codex(AGENTS["codex"], args.api_key, args.project, args.user_id):
        configured += 1
    
    print("=" * 50)
    print(f"🎉 Done! {configured} agents now share a single brain.")
    print("")
    print("Next steps:")
    print("  1. Restart any open AI apps (Claude Desktop, etc.)")
    print("  2. Test: Ask any agent 'What do you know about me?'")
    print("  3. Store: Tell any agent to remember something")
    print("  4. Verify: Ask a DIFFERENT agent to recall it")

if __name__ == "__main__":
    main()
