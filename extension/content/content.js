/**
 * Remembra Browser Extension - Content Script
 * Injected into AI chat interfaces (ChatGPT, Claude, Perplexity)
 */

(function() {
  'use strict';
  
  // Prevent double injection
  if (window.__remembraInjected) return;
  window.__remembraInjected = true;
  
  console.log('[Remembra] Content script loaded on', window.location.hostname);
  
  // Platform detection
  const PLATFORM = detectPlatform();
  
  function detectPlatform() {
    const host = window.location.hostname;
    if (host.includes('chat.openai.com') || host.includes('chatgpt.com')) return 'chatgpt';
    if (host.includes('claude.ai')) return 'claude';
    if (host.includes('perplexity.ai')) return 'perplexity';
    return 'unknown';
  }
  
  // ===== FLOATING ACTION BUTTON =====
  
  let fab = null;
  let fabTimeout = null;
  
  function createFAB() {
    if (fab) return fab;
    
    fab = document.createElement('div');
    fab.id = 'remembra-fab';
    fab.innerHTML = `
      <button id="remembra-save-btn" title="Save to Remembra">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"></path>
        </svg>
      </button>
    `;
    
    document.body.appendChild(fab);
    
    fab.querySelector('#remembra-save-btn').addEventListener('click', saveSelection);
    
    return fab;
  }
  
  function showFAB(x, y) {
    if (!fab) createFAB();
    
    // Position near selection
    fab.style.left = `${Math.min(x, window.innerWidth - 60)}px`;
    fab.style.top = `${Math.max(y - 50, 10)}px`;
    fab.classList.add('visible');
    
    // Auto-hide after 5 seconds
    clearTimeout(fabTimeout);
    fabTimeout = setTimeout(hideFAB, 5000);
  }
  
  function hideFAB() {
    if (fab) fab.classList.remove('visible');
  }
  
  // ===== SAVE FUNCTIONALITY =====
  
  async function saveSelection() {
    const selection = window.getSelection();
    const text = selection.toString().trim();
    
    if (!text) {
      showNotification('No text selected', 'error');
      return;
    }
    
    showNotification('Saving...', 'loading');
    
    const result = await chrome.runtime.sendMessage({
      type: 'STORE_MEMORY',
      content: text,
      metadata: {
        platform: PLATFORM,
        url: window.location.href,
        title: document.title
      }
    });
    
    if (result.success) {
      showNotification('Saved to memory!', 'success');
      hideFAB();
    } else {
      showNotification(result.error || 'Failed to save', 'error');
    }
  }
  
  // ===== NOTIFICATION SYSTEM =====
  
  let notificationEl = null;
  
  function showNotification(message, type = 'info') {
    if (!notificationEl) {
      notificationEl = document.createElement('div');
      notificationEl.id = 'remembra-notification';
      document.body.appendChild(notificationEl);
    }
    
    notificationEl.textContent = message;
    notificationEl.className = `visible ${type}`;
    
    if (type !== 'loading') {
      setTimeout(() => {
        notificationEl.classList.remove('visible');
      }, 3000);
    }
  }
  
  // ===== SIDEBAR =====
  
  let sidebar = null;
  
  function createSidebar() {
    if (sidebar) return sidebar;
    
    sidebar = document.createElement('div');
    sidebar.id = 'remembra-sidebar';
    sidebar.innerHTML = `
      <div class="remembra-sidebar-header">
        <span class="remembra-logo">🧠 Remembra</span>
        <button id="remembra-close-sidebar" title="Close">×</button>
      </div>
      <div class="remembra-sidebar-search">
        <input type="text" id="remembra-search-input" placeholder="Search memories..." />
        <button id="remembra-search-btn">Search</button>
      </div>
      <div id="remembra-memories-list" class="remembra-memories-list">
        <p class="remembra-hint">Search your memories or use auto-context</p>
      </div>
      <div class="remembra-sidebar-footer">
        <button id="remembra-auto-context" title="Auto-recall relevant memories">
          ⚡ Auto-Context
        </button>
      </div>
    `;
    
    document.body.appendChild(sidebar);
    
    // Event listeners
    sidebar.querySelector('#remembra-close-sidebar').addEventListener('click', toggleSidebar);
    sidebar.querySelector('#remembra-search-btn').addEventListener('click', searchMemories);
    sidebar.querySelector('#remembra-search-input').addEventListener('keypress', (e) => {
      if (e.key === 'Enter') searchMemories();
    });
    sidebar.querySelector('#remembra-auto-context').addEventListener('click', autoContext);
    
    return sidebar;
  }
  
  function toggleSidebar() {
    if (!sidebar) createSidebar();
    sidebar.classList.toggle('open');
  }
  
  async function searchMemories() {
    const input = sidebar.querySelector('#remembra-search-input');
    const query = input.value.trim();
    
    if (!query) return;
    
    const listEl = sidebar.querySelector('#remembra-memories-list');
    listEl.innerHTML = '<p class="remembra-loading">Searching...</p>';
    
    const result = await chrome.runtime.sendMessage({
      type: 'RECALL_MEMORIES',
      query: query,
      limit: 10
    });
    
    if (result.success && result.memories) {
      renderMemories(result.memories);
    } else {
      listEl.innerHTML = `<p class="remembra-error">${result.error || 'No results found'}</p>`;
    }
  }
  
  function renderMemories(memories) {
    const listEl = sidebar.querySelector('#remembra-memories-list');
    
    if (!memories || memories.length === 0) {
      listEl.innerHTML = '<p class="remembra-hint">No memories found</p>';
      return;
    }
    
    listEl.innerHTML = memories.map((mem, i) => `
      <div class="remembra-memory-item" data-index="${i}">
        <div class="remembra-memory-content">${escapeHtml(truncate(mem.content || mem.text, 150))}</div>
        <div class="remembra-memory-actions">
          <button class="remembra-insert-btn" data-content="${escapeAttr(mem.content || mem.text)}">
            Insert
          </button>
          <button class="remembra-copy-btn" data-content="${escapeAttr(mem.content || mem.text)}">
            Copy
          </button>
        </div>
      </div>
    `).join('');
    
    // Add click handlers
    listEl.querySelectorAll('.remembra-insert-btn').forEach(btn => {
      btn.addEventListener('click', () => insertIntoChat(btn.dataset.content));
    });
    
    listEl.querySelectorAll('.remembra-copy-btn').forEach(btn => {
      btn.addEventListener('click', () => copyToClipboard(btn.dataset.content));
    });
  }
  
  // ===== AUTO-CONTEXT =====
  
  async function autoContext() {
    const chatContext = extractChatContext();
    
    if (!chatContext) {
      showNotification('Could not extract chat context', 'error');
      return;
    }
    
    const input = sidebar.querySelector('#remembra-search-input');
    input.value = chatContext.substring(0, 200);
    
    await searchMemories();
  }
  
  function extractChatContext() {
    // Platform-specific extraction
    let messages = [];
    
    switch (PLATFORM) {
      case 'chatgpt':
        messages = Array.from(document.querySelectorAll('[data-message-author-role]'))
          .slice(-5)
          .map(el => el.textContent);
        break;
        
      case 'claude':
        messages = Array.from(document.querySelectorAll('[data-testid="user-message"], [data-testid="assistant-message"]'))
          .slice(-5)
          .map(el => el.textContent);
        // Fallback for Claude
        if (messages.length === 0) {
          messages = Array.from(document.querySelectorAll('.font-user-message, .font-claude-message'))
            .slice(-5)
            .map(el => el.textContent);
        }
        break;
        
      case 'perplexity':
        messages = Array.from(document.querySelectorAll('[class*="prose"]'))
          .slice(-5)
          .map(el => el.textContent);
        break;
    }
    
    return messages.join('\n').substring(0, 500);
  }
  
  // ===== INSERT INTO CHAT =====
  
  function insertIntoChat(text) {
    let inputEl = null;
    
    switch (PLATFORM) {
      case 'chatgpt':
        inputEl = document.querySelector('#prompt-textarea, textarea[data-id="root"]');
        break;
        
      case 'claude':
        inputEl = document.querySelector('[contenteditable="true"], .ProseMirror');
        break;
        
      case 'perplexity':
        inputEl = document.querySelector('textarea[placeholder*="Ask"]');
        break;
    }
    
    if (!inputEl) {
      copyToClipboard(text);
      showNotification('Copied! Paste into chat.', 'success');
      return;
    }
    
    // Handle contenteditable (Claude)
    if (inputEl.getAttribute('contenteditable') === 'true' || inputEl.classList.contains('ProseMirror')) {
      inputEl.focus();
      document.execCommand('insertText', false, text);
    } else {
      // Handle textarea
      inputEl.focus();
      const start = inputEl.selectionStart;
      const end = inputEl.selectionEnd;
      const currentValue = inputEl.value;
      inputEl.value = currentValue.substring(0, start) + text + currentValue.substring(end);
      inputEl.selectionStart = inputEl.selectionEnd = start + text.length;
      
      // Trigger input event for React
      inputEl.dispatchEvent(new Event('input', { bubbles: true }));
    }
    
    showNotification('Inserted!', 'success');
  }
  
  function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
      showNotification('Copied!', 'success');
    }).catch(() => {
      showNotification('Copy failed', 'error');
    });
  }
  
  // ===== UTILITIES =====
  
  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
  
  function escapeAttr(text) {
    return text.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  
  function truncate(text, maxLength) {
    if (!text) return '';
    return text.length > maxLength ? text.substring(0, maxLength) + '...' : text;
  }
  
  // ===== EVENT LISTENERS =====
  
  // Show FAB on text selection
  document.addEventListener('mouseup', (e) => {
    setTimeout(() => {
      const selection = window.getSelection();
      const text = selection.toString().trim();
      
      if (text && text.length > 10) {
        showFAB(e.pageX, e.pageY);
      } else {
        hideFAB();
      }
    }, 10);
  });
  
  // Hide FAB on click elsewhere
  document.addEventListener('mousedown', (e) => {
    if (fab && !fab.contains(e.target)) {
      hideFAB();
    }
  });
  
  // Keyboard shortcut: Ctrl+Shift+R to toggle sidebar
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.key === 'R') {
      e.preventDefault();
      toggleSidebar();
    }
  });
  
  // Message handler from background script
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    switch (message.type) {
      case 'REMEMBRA_NOTIFICATION':
        showNotification(message.message, message.success ? 'success' : 'error');
        break;
        
      case 'GET_PAGE_CONTEXT':
        const context = extractChatContext();
        chrome.runtime.sendMessage({
          type: 'PAGE_CONTEXT',
          content: context,
          url: window.location.href,
          title: document.title
        });
        break;
        
      case 'TOGGLE_SIDEBAR':
        toggleSidebar();
        break;
    }
    sendResponse({ received: true });
    return true;
  });
  
  // Initialize
  createFAB();
  
  console.log('[Remembra] Content script initialized for', PLATFORM);
  
})();
