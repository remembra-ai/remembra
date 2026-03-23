# Remembra Browser Extension

Give your AI conversations persistent memory. Save context from ChatGPT, Claude, and Perplexity.

## Features

- **Save to Memory**: Select any text and right-click "Save to Remembra" or click the floating button
- **Recall Memories**: Search your memories from the popup or in-page sidebar
- **Auto-Context**: Automatically recall relevant memories based on current conversation
- **Cross-Platform**: Works on ChatGPT, Claude, and Perplexity

## Installation

### Chrome / Chromium-based browsers

1. Open `chrome://extensions` (or `edge://extensions` for Edge)
2. Enable "Developer mode" (toggle in top-right)
3. Click "Load unpacked"
4. Select the `extension/` folder
5. Click the extension icon and enter your Remembra API key

### Get an API Key

1. Sign up at [remembra.dev](https://remembra.dev)
2. Go to Dashboard → API Keys
3. Copy your key and paste it in the extension settings

## Usage

### Saving Memories

**Option 1: Context Menu**
1. Select text on any supported AI chat page
2. Right-click → "Save to Remembra"

**Option 2: Floating Button**
1. Select text (10+ characters)
2. Click the bookmark icon that appears

**Option 3: Extension Popup**
1. Click extension icon
2. Type or paste text in "Quick Save"
3. Click "Save to Memory"

### Recalling Memories

**From Popup:**
1. Click extension icon
2. Search in "Search Memories"
3. Click "Copy" to copy to clipboard

**From Sidebar (on AI chat pages):**
1. Press `Ctrl+Shift+R` to open sidebar
2. Search your memories
3. Click "Insert" to paste into the chat input

**Auto-Context:**
1. Open sidebar (`Ctrl+Shift+R`)
2. Click "⚡ Auto-Context"
3. Extension extracts recent conversation and searches for relevant memories

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+Shift+R` | Toggle sidebar on AI chat pages |

## Supported Sites

- [x] chat.openai.com / chatgpt.com
- [x] claude.ai
- [x] perplexity.ai

## Technical Details

- **Manifest Version:** 3 (latest Chrome standard)
- **Permissions:** `storage`, `contextMenus`, `activeTab`
- **API:** Connects to https://api.remembra.dev

## File Structure

```
extension/
├── manifest.json          # Extension configuration
├── background/
│   └── background.js      # Service worker (API calls)
├── content/
│   ├── content.js         # Injected into AI chat pages
│   └── content.css        # Injected styles
├── popup/
│   ├── popup.html         # Extension popup UI
│   ├── popup.js           # Popup logic
│   └── popup.css          # Popup styles
└── icons/
    ├── icon16.png
    ├── icon32.png
    ├── icon48.png
    └── icon128.png
```

## Development

### Local Testing

1. Make changes to files
2. Go to `chrome://extensions`
3. Click the refresh icon on the Remembra extension
4. Test changes

### Debug

- Background script logs: Extension page → "service worker" link
- Content script logs: Regular browser DevTools console
- Popup logs: Right-click extension icon → Inspect popup

## Privacy

- API key stored in Chrome sync storage (encrypted by Chrome)
- Memories sent only to Remembra API (your configured server)
- No analytics or tracking in extension

## Version History

### 0.12.0 (MVP)
- Initial release
- Save selected text via context menu or floating button
- Search memories via popup and sidebar
- Auto-context feature for AI conversations
- Support for ChatGPT, Claude, Perplexity

## License

MIT License - Copyright 2024 DolphyTech
