// Background Service Worker for YNAB Helper Chrome Extension
// Manifest V3 requires a service worker instead of background pages

console.log('YNAB Helper: Service worker initialized');

// Listen for extension installation
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === 'install') {
    console.log('YNAB Helper: Extension installed');

    // Set default storage values
    chrome.storage.local.set({
      cachedOrders: [],
      cacheTimestamp: null
    });

    // Open welcome page or instructions
    // chrome.tabs.create({ url: 'https://www.amazon.com/gp/css/order-history' });
  } else if (details.reason === 'update') {
    console.log('YNAB Helper: Extension updated to version', chrome.runtime.getManifest().version);
  }
});

// Listen for messages from content scripts or popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  console.log('YNAB Helper: Received message:', request);

  // Handle different message types
  if (request.action === 'clearCache') {
    chrome.storage.local.set({
      cachedOrders: [],
      cacheTimestamp: null
    }, () => {
      sendResponse({ success: true });
    });
    return true; // Async response
  }

  if (request.action === 'getStats') {
    chrome.storage.local.get(['cachedOrders', 'cacheTimestamp'], (result) => {
      sendResponse({
        orderCount: result.cachedOrders ? result.cachedOrders.length : 0,
        lastUpdate: result.cacheTimestamp
      });
    });
    return true; // Async response
  }
});

// Keep service worker alive (optional, for long-running tasks)
// This is a workaround for service worker timeout issues
let keepAliveInterval;

function keepAlive() {
  keepAliveInterval = setInterval(() => {
    chrome.runtime.getPlatformInfo(() => {
      // Just to keep the service worker alive
    });
  }, 20000); // Every 20 seconds
}

function stopKeepAlive() {
  if (keepAliveInterval) {
    clearInterval(keepAliveInterval);
  }
}

// Start keep-alive when service worker starts
keepAlive();
