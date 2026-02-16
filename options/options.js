// DOM Elements
const apiTokenInput = document.getElementById('apiToken');
const toggleTokenBtn = document.getElementById('toggleToken');
const saveTokenBtn = document.getElementById('saveToken');
const connectionStatus = document.getElementById('connectionStatus');
const budgetSection = document.getElementById('budgetSection');
const budgetSelect = document.getElementById('budgetSelect');
const amazonAccountSelect = document.getElementById('amazonAccountSelect');
const venmoAccountSelect = document.getElementById('venmoAccountSelect');
const saveSettingsBtn = document.getElementById('saveSettings');
const saveStatus = document.getElementById('saveStatus');
const currentSettings = document.getElementById('currentSettings');
const currentBudget = document.getElementById('currentBudget');
const currentAmazonAccount = document.getElementById('currentAmazonAccount');
const currentVenmoAccount = document.getElementById('currentVenmoAccount');
const clearSettingsBtn = document.getElementById('clearSettings');

// State
let budgets = [];
let accounts = [];

// Event Listeners
toggleTokenBtn.addEventListener('click', toggleTokenVisibility);
saveTokenBtn.addEventListener('click', handleSaveToken);
budgetSelect.addEventListener('change', handleBudgetChange);
saveSettingsBtn.addEventListener('click', handleSaveSettings);
clearSettingsBtn.addEventListener('click', handleClearSettings);

// Load saved settings on page load
document.addEventListener('DOMContentLoaded', loadSavedSettings);

async function loadSavedSettings() {
  const settings = await chrome.storage.local.get([
    'ynabToken',
    'ynabBudgetId',
    'ynabAmazonAccountId',
    'ynabVenmoAccountId',
    'ynabBudgetName',
    'ynabAmazonAccountName',
    'ynabVenmoAccountName'
  ]);

  if (settings.ynabToken) {
    apiTokenInput.value = settings.ynabToken;
    currentSettings.style.display = 'block';

    if (settings.ynabBudgetName) {
      currentBudget.textContent = settings.ynabBudgetName;
    }

    if (settings.ynabAmazonAccountName) {
      currentAmazonAccount.textContent = settings.ynabAmazonAccountName;
    }

    if (settings.ynabVenmoAccountName) {
      currentVenmoAccount.textContent = settings.ynabVenmoAccountName;
    }

    // Auto-load budgets if token exists
    await loadBudgets(settings.ynabToken);
  }
}

function toggleTokenVisibility() {
  if (apiTokenInput.type === 'password') {
    apiTokenInput.type = 'text';
    toggleTokenBtn.textContent = 'Hide token';
  } else {
    apiTokenInput.type = 'password';
    toggleTokenBtn.textContent = 'Show token';
  }
}

async function handleSaveToken() {
  const token = apiTokenInput.value.trim();

  if (!token) {
    showStatus(connectionStatus, 'error', 'Please enter your YNAB API token');
    return;
  }

  saveTokenBtn.disabled = true;
  saveTokenBtn.textContent = 'Testing...';
  showStatus(connectionStatus, 'info', 'Testing connection to YNAB...');

  try {
    // Test the token by fetching user info
    const response = await fetch('https://api.ynab.com/v1/user', {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    });

    if (!response.ok) {
      throw new Error('Invalid API token or connection failed');
    }

    // Save token
    await chrome.storage.local.set({ ynabToken: token });

    showStatus(connectionStatus, 'success', '✓ Connected successfully! Loading budgets...');
    currentSettings.style.display = 'block';

    // Load budgets
    await loadBudgets(token);

  } catch (error) {
    showStatus(connectionStatus, 'error', `Error: ${error.message}`);
  } finally {
    saveTokenBtn.disabled = false;
    saveTokenBtn.textContent = 'Save & Test Connection';
  }
}

async function loadBudgets(token) {
  try {
    const response = await fetch('https://api.ynab.com/v1/budgets', {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    });

    if (!response.ok) {
      throw new Error('Failed to load budgets');
    }

    const data = await response.json();
    budgets = data.data.budgets;

    // Populate budget dropdown
    budgetSelect.innerHTML = '<option value="">-- Select a budget --</option>';
    budgets.forEach(budget => {
      const option = document.createElement('option');
      option.value = budget.id;
      option.textContent = budget.name;
      budgetSelect.appendChild(option);
    });

    budgetSection.style.display = 'block';

    // Load saved budget selection
    const settings = await chrome.storage.local.get(['ynabBudgetId']);
    if (settings.ynabBudgetId) {
      budgetSelect.value = settings.ynabBudgetId;
      await handleBudgetChange();
    }

  } catch (error) {
    console.error('Error loading budgets:', error);
    showStatus(connectionStatus, 'error', `Error loading budgets: ${error.message}`);
  }
}

async function handleBudgetChange() {
  const budgetId = budgetSelect.value;

  if (!budgetId) {
    amazonAccountSelect.innerHTML = '<option value="">Select a budget first...</option>';
    venmoAccountSelect.innerHTML = '<option value="">Select a budget first...</option>';
    return;
  }

  amazonAccountSelect.innerHTML = '<option value="">Loading accounts...</option>';
  venmoAccountSelect.innerHTML = '<option value="">Loading accounts...</option>';

  try {
    const token = apiTokenInput.value.trim();
    const response = await fetch(`https://api.ynab.com/v1/budgets/${budgetId}/accounts`, {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    });

    if (!response.ok) {
      throw new Error('Failed to load accounts');
    }

    const data = await response.json();
    accounts = data.data.accounts.filter(account => !account.closed && !account.deleted);

    // Populate BOTH account dropdowns with the same accounts
    const populateDropdown = (dropdown, savedId) => {
      dropdown.innerHTML = '<option value="">-- Select an account --</option>';
      accounts.forEach(account => {
        const option = document.createElement('option');
        option.value = account.id;
        option.textContent = account.name;
        dropdown.appendChild(option);
      });
      if (savedId) {
        dropdown.value = savedId;
      }
    };

    // Load saved account selections
    const settings = await chrome.storage.local.get(['ynabAmazonAccountId', 'ynabVenmoAccountId']);
    populateDropdown(amazonAccountSelect, settings.ynabAmazonAccountId);
    populateDropdown(venmoAccountSelect, settings.ynabVenmoAccountId);

  } catch (error) {
    console.error('Error loading accounts:', error);
    showStatus(saveStatus, 'error', `Error loading accounts: ${error.message}`);
  }
}

async function handleSaveSettings() {
  const budgetId = budgetSelect.value;
  const amazonAccountId = amazonAccountSelect.value;
  const venmoAccountId = venmoAccountSelect.value;

  if (!budgetId || !amazonAccountId || !venmoAccountId) {
    showStatus(saveStatus, 'error', 'Please select a budget and both accounts');
    return;
  }

  const selectedBudget = budgets.find(b => b.id === budgetId);
  const selectedAmazonAccount = accounts.find(a => a.id === amazonAccountId);
  const selectedVenmoAccount = accounts.find(a => a.id === venmoAccountId);

  await chrome.storage.local.set({
    ynabBudgetId: budgetId,
    ynabAmazonAccountId: amazonAccountId,
    ynabVenmoAccountId: venmoAccountId,
    ynabBudgetName: selectedBudget.name,
    ynabAmazonAccountName: selectedAmazonAccount.name,
    ynabVenmoAccountName: selectedVenmoAccount.name
  });

  currentBudget.textContent = selectedBudget.name;
  currentAmazonAccount.textContent = selectedAmazonAccount.name;
  currentVenmoAccount.textContent = selectedVenmoAccount.name;

  showStatus(saveStatus, 'success', `✓ Settings saved! Amazon → ${selectedAmazonAccount.name}, Venmo → ${selectedVenmoAccount.name}`);
}

async function handleClearSettings() {
  if (!confirm('Are you sure you want to clear all YNAB settings?')) {
    return;
  }

  await chrome.storage.local.remove([
    'ynabToken',
    'ynabBudgetId',
    'ynabAmazonAccountId',
    'ynabVenmoAccountId',
    'ynabBudgetName',
    'ynabAmazonAccountName',
    'ynabVenmoAccountName'
  ]);

  apiTokenInput.value = '';
  budgetSelect.innerHTML = '<option value="">-- Select a budget --</option>';
  amazonAccountSelect.innerHTML = '<option value="">Select a budget first...</option>';
  venmoAccountSelect.innerHTML = '<option value="">Select a budget first...</option>';
  budgetSection.style.display = 'none';
  currentSettings.style.display = 'none';

  showStatus(connectionStatus, 'success', 'Settings cleared');
}

function showStatus(element, type, message) {
  element.className = `status-message ${type}`;
  element.textContent = message;
  element.classList.remove('hidden');
}
