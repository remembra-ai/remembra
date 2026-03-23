/**
 * Remembra Browser Extension - Background Service Worker
 * Handles API communication, context menus, and message routing
 */

const API_BASE = 'https://api.remembra.dev';

// Initialize context menu on install
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'remembra-save',
    title: 'Save to Remembra',
    contexts: ['selection']
  });
  
  chrome.contextMenus.create({
    id: 'remembra-save-page',
    title: 'Save page context to Remembra',
    contexts: ['page']
  });
  
  console.log('[Remembra] Extension installed, context menus created');
});

// Handle context menu clicks
chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId === 'remembra-save' && info.selectionText) {
    const result = await storeMemory(info.selectionText, {
      source: 'context-menu',
      url: tab.url,
      title: tab.title
    });
    
    // Notify content script
    chrome.tabs.sendMessage(tab.id, {
      type: 'REMEMBRA_NOTIFICATION',
      success: result.success,
      message: result.success ? 'Saved to memory!' : result.error
    });
  }
  
  if (info.menuItemId === 'remembra-save-page') {
    // Request page content from content script
    chrome.tabs.sendMessage(tab.id, { type: 'GET_PAGE_CONTEXT' });
  }
});

// Message handler for content scripts and popup
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  handleMessage(message, sender).then(sendResponse);
  return true; // Keep channel open for async response
});

async function handleMessage(message, sender) {
  switch (message.type) {
    case 'STORE_MEMORY':
      return await storeMemory(message.content, message.metadata);
    
    case 'RECALL_MEMORIES':
      return await recallMemories(message.query, message.limit);
    
    case 'GET_API_KEY':
      return await getApiKey();
    
    case 'SET_API_KEY':
      return await setApiKey(message.apiKey);
    
    case 'CHECK_CONNECTION':
      return await checkConnection();
    
    case 'PAGE_CONTEXT':
      // Save page context received from content script
      return await storeMemory(message.content, {
        source: 'page-context',
        url: message.url,
        title: message.title
      });
    
    default:
      return { success: false, error: 'Unknown message type' };
  }
}

/**
 * Store a memory via Remembra API
 */
async function storeMemory(content, metadata = {}) {
  const apiKey = await getApiKey();
  if (!apiKey.success || !apiKey.apiKey) {
    return { success: false, error: 'API key not configured. Click the extension icon to set up.' };
  }
  
  try {
    const response = await fetch(`${API_BASE}/api/v1/memories`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${apiKey.apiKey}`
      },
      body: JSON.stringify({
        content: content,
        metadata: {
          ...metadata,
          source: metadata.source || 'browser-extension',
          timestamp: new Date().toISOString()
        }
      })
    });
    
    if (!response.ok) {
      const error = await response.text();
      console.error('[Remembra] API error:', response.status, error);
      return { success: false, error: `API error: ${response.status}` };
    }
    
    const data = await response.json();
    console.log('[Remembra] Memory stored:', data);
    return { success: true, data };
    
  } catch (error) {
    console.error('[Remembra] Store error:', error);
    return { success: false, error: error.message };
  }
}

/**
 * Recall memories via Remembra API
 */
async function recallMemories(query, limit = 5) {
  const apiKey = await getApiKey();
  if (!apiKey.success || !apiKey.apiKey) {
    return { success: false, error: 'API key not configured' };
  }
  
  try {
    const params = new URLSearchParams({ query, limit: String(limit) });
    const response = await fetch(`${API_BASE}/api/v1/memories/search?${params}`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${apiKey.apiKey}`
      }
    });
    
    if (!response.ok) {
      return { success: false, error: `API error: ${response.status}` };
    }
    
    const data = await response.json();
    return { success: true, memories: data.memories || data.results || data };
    
  } catch (error) {
    console.error('[Remembra] Recall error:', error);
    return { success: false, error: error.message };
  }
}

/**
 * Get stored API key
 */
async function getApiKey() {
  try {
    const result = await chrome.storage.sync.get(['remembra_api_key']);
    return { success: true, apiKey: result.remembra_api_key || null };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

/**
 * Store API key
 */
async function setApiKey(apiKey) {
  try {
    await chrome.storage.sync.set({ remembra_api_key: apiKey });
    return { success: true };
  } catch (error) {
    return { success: false, error: error.message };
  }
}

/**
 * Check API connection
 */
async function checkConnection() {
  const apiKey = await getApiKey();
  if (!apiKey.success || !apiKey.apiKey) {
    return { success: false, connected: false, error: 'No API key' };
  }
  
  try {
    const response = await fetch(`${API_BASE}/health`, {
      method: 'GET',
      headers: {
        'Authorization': `Bearer ${apiKey.apiKey}`
      }
    });
    
    return { 
      success: true, 
      connected: response.ok,
      status: response.status 
    };
  } catch (error) {
    return { success: false, connected: false, error: error.message };
  }
}

console.log('[Remembra] Background service worker loaded');
