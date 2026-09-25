/**
 * Remembra Browser Extension - Popup Script
 * Settings, quick save, and memory search
 */

document.addEventListener('DOMContentLoaded', init);

async function init() {
  // Check for existing API key
  const result = await chrome.runtime.sendMessage({ type: 'GET_API_KEY' });
  
  if (result.success && result.apiKey) {
    showMainSection();
    checkConnection();
  } else {
    showSettingsSection();
  }
  
  // Setup event listeners
  setupEventListeners();
}

function setupEventListeners() {
  // Save API key
  document.getElementById('save-key-btn').addEventListener('click', saveApiKey);
  document.getElementById('api-key-input').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') saveApiKey();
  });
  
  // Quick save
  document.getElementById('quick-save-btn').addEventListener('click', quickSave);
  
  // Search
  document.getElementById('search-btn').addEventListener('click', searchMemories);
  document.getElementById('search-input').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') searchMemories();
  });
  
  // Dashboard
  document.getElementById('open-dashboard-btn').addEventListener('click', () => {
    chrome.tabs.create({ url: 'https://remembra.dev/dashboard' });
  });
  
  // Disconnect
  document.getElementById('disconnect-btn').addEventListener('click', disconnect);
}

async function saveApiKey() {
  const input = document.getElementById('api-key-input');
  const apiKey = input.value.trim();
  
  if (!apiKey) {
    showError('Please enter an API key');
    return;
  }
  
  const btn = document.getElementById('save-key-btn');
  btn.disabled = true;
  btn.textContent = 'Connecting...';
  
  const result = await chrome.runtime.sendMessage({
    type: 'SET_API_KEY',
    apiKey: apiKey
  });
  
  if (result.success) {
    // Verify connection
    const check = await chrome.runtime.sendMessage({ type: 'CHECK_CONNECTION' });
    
    if (check.connected) {
      showMainSection();
      updateStatus(true);
    } else {
      showError('Invalid API key or server unreachable');
      btn.disabled = false;
      btn.textContent = 'Connect';
    }
  } else {
    showError('Failed to save API key');
    btn.disabled = false;
    btn.textContent = 'Connect';
  }
}

async function quickSave() {
  const input = document.getElementById('quick-save-input');
  const content = input.value.trim();
  
  if (!content) {
    showError('Please enter something to save');
    return;
  }
  
  const btn = document.getElementById('quick-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';
  
  const result = await chrome.runtime.sendMessage({
    type: 'STORE_MEMORY',
    content: content,
    metadata: { source: 'extension-popup' }
  });
  
  if (result.success) {
    input.value = '';
    btn.textContent = 'Saved!';
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = 'Save to Memory';
    }, 1500);
  } else {
    showError(result.error || 'Failed to save');
    btn.disabled = false;
    btn.textContent = 'Save to Memory';
  }
}

async function searchMemories() {
  const input = document.getElementById('search-input');
  const query = input.value.trim();
  
  if (!query) return;
  
  const resultsEl = document.getElementById('search-results');
  resultsEl.innerHTML = '<div class="loading">Searching...</div>';
  
  const result = await chrome.runtime.sendMessage({
    type: 'RECALL_MEMORIES',
    query: query,
    limit: 5
  });
  
  if (result.success && result.memories) {
    renderResults(result.memories);
  } else {
    const errorEl = document.createElement('div');
    errorEl.className = 'error';
    errorEl.textContent = result.error || 'No results found';
    resultsEl.replaceChildren(errorEl);
  }
}

function renderResults(memories) {
  const resultsEl = document.getElementById('search-results');
  
  if (!memories || memories.length === 0) {
    resultsEl.innerHTML = '<div class="no-results">No memories found</div>';
    return;
  }
  
  // Untrusted memory text is only ever assigned via textContent/dataset.
  resultsEl.replaceChildren(...memories.map(mem => {
    const text = mem.content || mem.text || '';
    const item = document.createElement('div');
    item.className = 'result-item';
    const content = document.createElement('div');
    content.className = 'result-content';
    content.textContent = truncate(text, 120);
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.dataset.content = text;
    btn.textContent = 'Copy';
    item.append(content, btn);
    return item;
  }));
  
  // Add copy handlers
  resultsEl.querySelectorAll('.copy-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      navigator.clipboard.writeText(btn.dataset.content);
      btn.textContent = 'Copied!';
      setTimeout(() => btn.textContent = 'Copy', 1000);
    });
  });
}

async function checkConnection() {
  const result = await chrome.runtime.sendMessage({ type: 'CHECK_CONNECTION' });
  updateStatus(result.connected);
}

function updateStatus(connected) {
  const badge = document.getElementById('status-badge');
  badge.className = `status-badge ${connected ? 'connected' : 'disconnected'}`;
  badge.querySelector('.status-text').textContent = connected ? 'Connected' : 'Disconnected';
}

async function disconnect() {
  await chrome.runtime.sendMessage({ type: 'SET_API_KEY', apiKey: '' });
  showSettingsSection();
  updateStatus(false);
  document.getElementById('api-key-input').value = '';
}

function showSettingsSection() {
  document.getElementById('settings-section').classList.remove('hidden');
  document.getElementById('main-section').classList.add('hidden');
}

function showMainSection() {
  document.getElementById('settings-section').classList.add('hidden');
  document.getElementById('main-section').classList.remove('hidden');
}

function showError(message) {
  // Simple alert for now - could be improved with toast
  alert(message);
}


function truncate(text, maxLength) {
  if (!text) return '';
  return text.length > maxLength ? text.substring(0, maxLength) + '...' : text;
}
