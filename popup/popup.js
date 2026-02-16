// DOM Elements
const fetchStevenBtn = document.getElementById('fetchStevenBtn');
const fetchAllisonBtn = document.getElementById('fetchAllisonBtn');
const uploadVenmoFamilyBtn = document.getElementById('uploadVenmoFamilyBtn');
const uploadVenmoAllisonBtn = document.getElementById('uploadVenmoAllisonBtn');
const venmoFamilyFileInput = document.getElementById('venmoFamilyFile');
const venmoAllisonFileInput = document.getElementById('venmoAllisonFile');
const clearStevenBtn = document.getElementById('clearStevenBtn');
const clearAllisonBtn = document.getElementById('clearAllisonBtn');
const clearVenmoFamilyBtn = document.getElementById('clearVenmoFamilyBtn');
const clearVenmoAllisonBtn = document.getElementById('clearVenmoAllisonBtn');
const syncYnabBtn = document.getElementById('syncYnabBtn');
const settingsBtn = document.getElementById('settingsBtn');
const stevenOrderCount = document.getElementById('stevenOrderCount');
const stevenPaymentCount = document.getElementById('stevenPaymentCount');
const allisonOrderCount = document.getElementById('allisonOrderCount');
const allisonPaymentCount = document.getElementById('allisonPaymentCount');
const venmoFamilyCount = document.getElementById('venmoFamilyCount');
const venmoAllisonCount = document.getElementById('venmoAllisonCount');
const statusArea = document.getElementById('statusArea');
const statusIcon = document.getElementById('statusIcon');
const statusText = document.getElementById('statusText');
const progressBar = document.getElementById('progressBar');
const progressFill = document.getElementById('progressFill');
const resultsSection = document.getElementById('resultsSection');
const resultsBody = document.getElementById('resultsBody');
const orderCount = document.getElementById('orderCount');
const errorArea = document.getElementById('errorArea');
const errorText = document.getElementById('errorText');

// State - Separate data for each account
let stevenOrders = [];
let stevenPayments = [];
let allisonOrders = [];
let allisonPayments = [];
let venmoFamilyTransactions = [];
let venmoAllisonTransactions = [];

// Event Listeners
fetchStevenBtn.addEventListener('click', () => handleFetchOrders('Steven'));
fetchAllisonBtn.addEventListener('click', () => handleFetchOrders('Allison'));
uploadVenmoFamilyBtn.addEventListener('click', () => venmoFamilyFileInput.click());
uploadVenmoAllisonBtn.addEventListener('click', () => venmoAllisonFileInput.click());
venmoFamilyFileInput.addEventListener('change', (e) => handleVenmoCSVUpload(e, 'Family'));
venmoAllisonFileInput.addEventListener('change', (e) => handleVenmoCSVUpload(e, 'AllisonVenmo'));
clearStevenBtn.addEventListener('click', () => handleClearCache('Steven'));
clearAllisonBtn.addEventListener('click', () => handleClearCache('Allison'));
clearVenmoFamilyBtn.addEventListener('click', () => handleClearCache('VenmoFamily'));
clearVenmoAllisonBtn.addEventListener('click', () => handleClearCache('VenmoAllison'));
syncYnabBtn.addEventListener('click', handleSyncToYnab);
settingsBtn.addEventListener('click', () => chrome.runtime.openOptionsPage());

// Load cached data on popup open
document.addEventListener('DOMContentLoaded', loadCachedData);

async function loadCachedData() {
  try {
    const result = await chrome.storage.local.get([
      'stevenOrders', 'stevenPayments',
      'allisonOrders', 'allisonPayments',
      'venmoFamilyTransactions', 'venmoAllisonTransactions'
    ]);

    // Load Steven's data
    if (result.stevenOrders) stevenOrders = result.stevenOrders;
    if (result.stevenPayments) stevenPayments = result.stevenPayments;

    // Load Allison's data
    if (result.allisonOrders) allisonOrders = result.allisonOrders;
    if (result.allisonPayments) allisonPayments = result.allisonPayments;

    // Load Venmo data
    if (result.venmoFamilyTransactions) venmoFamilyTransactions = result.venmoFamilyTransactions;
    if (result.venmoAllisonTransactions) venmoAllisonTransactions = result.venmoAllisonTransactions;

    // Update UI counts
    updateAccountCounts();

    // Display combined results
    if (stevenOrders.length > 0 || stevenPayments.length > 0 ||
        allisonOrders.length > 0 || allisonPayments.length > 0 ||
        venmoFamilyTransactions.length > 0 || venmoAllisonTransactions.length > 0) {
      displayMergedResults();
      showStatus('success', `Loaded cached data for all accounts`);
    }
  } catch (error) {
    console.error('Error loading cached data:', error);
  }
}

function updateAccountCounts() {
  // Amazon accounts - show separate order and payment counts
  const stevenOrdersCount = stevenOrders.length;
  const stevenPaymentsCount = stevenPayments.length;
  const allisonOrdersCount = allisonOrders.length;
  const allisonPaymentsCount = allisonPayments.length;

  // Venmo accounts - show total transaction count
  const venmoFamilyTotal = venmoFamilyTransactions.length;
  const venmoAllisonTotal = venmoAllisonTransactions.length;

  stevenOrderCount.textContent = `${stevenOrdersCount} order${stevenOrdersCount !== 1 ? 's' : ''}`;
  stevenPaymentCount.textContent = `${stevenPaymentsCount} payment${stevenPaymentsCount !== 1 ? 's' : ''}`;
  allisonOrderCount.textContent = `${allisonOrdersCount} order${allisonOrdersCount !== 1 ? 's' : ''}`;
  allisonPaymentCount.textContent = `${allisonPaymentsCount} payment${allisonPaymentsCount !== 1 ? 's' : ''}`;
  venmoFamilyCount.textContent = `${venmoFamilyTotal} item${venmoFamilyTotal !== 1 ? 's' : ''}`;
  venmoAllisonCount.textContent = `${venmoAllisonTotal} item${venmoAllisonTotal !== 1 ? 's' : ''}`;
}

function displayMergedResults() {
  // Merge data for each account
  const stevenMerged = mergeOrdersAndPayments(stevenOrders, stevenPayments);
  const allisonMerged = mergeOrdersAndPayments(allisonOrders, allisonPayments);

  // Tag each item with account name and source
  const stevenData = (stevenMerged.length > 0 ? stevenMerged : stevenOrders).map(item => ({...item, account: 'Steven', source: 'Amazon'}));
  const allisonData = (allisonMerged.length > 0 ? allisonMerged : allisonOrders).map(item => ({...item, account: 'Allison', source: 'Amazon'}));

  // Add Venmo data
  const venmoFamilyData = venmoFamilyTransactions.map(item => ({...item, account: 'Family', source: 'Venmo'}));
  const venmoAllisonData = venmoAllisonTransactions.map(item => ({...item, account: 'Allison', source: 'Venmo'}));

  // Combine all
  const allData = [...stevenData, ...allisonData, ...venmoFamilyData, ...venmoAllisonData];

  if (allData.length === 0) {
    resultsSection.classList.add('hidden');
    return;
  }

  // Sort by date (most recent first)
  allData.sort((a, b) => {
    const dateA = new Date(a.chargeDate || a.date);
    const dateB = new Date(b.chargeDate || b.date);
    return dateB - dateA;
  });

  const amazonCount = stevenData.length + allisonData.length;
  const venmoCount = venmoFamilyData.length + venmoAllisonData.length;
  orderCount.textContent = `${allData.length} total (${amazonCount} Amazon, ${venmoCount} Venmo)`;

  resultsBody.innerHTML = '';

  const previewItems = allData.slice(0, 10);
  previewItems.forEach(item => {
    const row = document.createElement('tr');

    const date = new Date(item.chargeDate || item.date);
    const formattedDate = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

    let itemsText, displayItems;
    if (item.source === 'Venmo') {
      // For Venmo: show payee and memo
      itemsText = `${item.payee}${item.memo ? ': ' + item.memo : ''}`;
      displayItems = itemsText.length > 25 ? itemsText.substring(0, 25) + '...' : itemsText;
    } else {
      // For Amazon: show items
      itemsText = item.items ? item.items.join(', ') : 'No products';
      displayItems = itemsText.length > 25 ? itemsText.substring(0, 25) + '...' : itemsText;
    }

    const amount = item.chargeAmount || item.total || item.amount;
    const totalDisplay = (amount == null || isNaN(amount) || amount === 0) ? '0.00' : amount.toFixed(2);

    const accountIcon = item.account === 'Steven' ? '👨' : (item.account === 'Family' ? '👨‍👩‍👧' : '👩');
    const sourceIcon = item.source === 'Venmo' ? '💸' : '📦';

    row.innerHTML = `
      <td>${accountIcon} ${sourceIcon}</td>
      <td>${formattedDate}${item.chargeDate ? ' 💳' : ''}</td>
      <td style="font-size: 11px;">${item.orderId || '-'}</td>
      <td title="${itemsText}">${displayItems}</td>
      <td>$${totalDisplay}</td>
    `;

    resultsBody.appendChild(row);
  });

  resultsSection.classList.remove('hidden');
}

function mergeOrdersAndPayments(orders, payments) {
  if (payments.length === 0) return [];

  const merged = [];
  const orderMap = new Map(orders.map(o => [o.orderId, o]));

  for (const payment of payments) {
    const order = orderMap.get(payment.orderId);

    merged.push({
      orderId: payment.orderId,
      chargeDate: payment.date, // Actual charge date from payments
      chargeAmount: payment.amount, // Actual amount charged
      payee: payment.payee,
      items: order ? order.items : [], // Product names from orders
      date: order ? order.date : payment.date, // Order date as fallback
      total: payment.amount
    });
  }

  return merged;
}

async function handleVenmoCSVUpload(event, accountType) {
  const file = event.target.files[0];
  if (!file) return;

  console.log('=== VENMO CSV UPLOAD STARTED ===');
  console.log(`File: ${file.name}, Account: ${accountType}`);

  try {
    hideError();
    showStatus('loading', `Parsing ${accountType === 'Family' ? 'Family' : "Allison's"} Venmo CSV...`);
    showProgress(0);

    // Read file as text
    const text = await file.text();
    console.log(`Read ${text.length} characters from CSV`);

    // Parse CSV
    const transactions = parseVenmoCSV(text, accountType);

    console.log(`Parsed ${transactions.length} bank transactions from Venmo CSV`);
    console.log('Sample transactions:', transactions.slice(0, 3));

    // Get existing transactions for this account
    let accountTransactions = accountType === 'Family' ? venmoFamilyTransactions : venmoAllisonTransactions;

    // Create unique key using date + payee + amount
    const existingKeys = new Set(accountTransactions.map(t =>
      `${t.date}-${t.payee}-${t.amount}`
    ));

    const uniqueNewTransactions = transactions.filter(t =>
      !existingKeys.has(`${t.date}-${t.payee}-${t.amount}`)
    );

    accountTransactions = [...accountTransactions, ...uniqueNewTransactions];

    // Update state and storage
    if (accountType === 'Family') {
      venmoFamilyTransactions = accountTransactions;
      await chrome.storage.local.set({ venmoFamilyTransactions });
      console.log(`Updated venmoFamilyTransactions: ${venmoFamilyTransactions.length} total`);
    } else {
      venmoAllisonTransactions = accountTransactions;
      await chrome.storage.local.set({ venmoAllisonTransactions });
      console.log(`Updated venmoAllisonTransactions: ${venmoAllisonTransactions.length} total`);
    }

    const skipped = transactions.length - uniqueNewTransactions.length;
    showProgress(100);
    console.log(`Added ${uniqueNewTransactions.length} new, skipped ${skipped} duplicates`);

    let message = `Added ${uniqueNewTransactions.length} new transaction${uniqueNewTransactions.length !== 1 ? 's' : ''} for ${accountType === 'Family' ? 'Family' : 'Allison'}`;
    if (skipped > 0) {
      message += ` (skipped ${skipped} duplicate${skipped !== 1 ? 's' : ''})`;
    }
    message += ` - ${accountTransactions.length} total`;

    showStatus('success', message);
    updateAccountCounts();
    displayMergedResults();

    // Reset file input
    event.target.value = '';

  } catch (error) {
    console.error('Error parsing Venmo CSV:', error);
    showError(`Error: ${error.message}`);
    showStatus('error', 'Failed to parse CSV');
    event.target.value = '';
  }
}

function parseVenmoCSV(csvText, accountType) {
  console.log('=== PARSING VENMO CSV ===');
  const transactions = [];
  const lines = csvText.split('\n');
  console.log(`Total lines in CSV: ${lines.length}`);

  // Find the header row (starts with ",ID,Datetime,Type...")
  let headerIndex = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes(',ID,Datetime,Type,')) {
      headerIndex = i;
      console.log(`Found header at line ${i}`);
      break;
    }
  }

  if (headerIndex === -1) {
    console.error('Could not find header row in CSV');
    throw new Error('Invalid Venmo CSV format - could not find header row');
  }

  // Parse header to get column indices
  const headers = lines[headerIndex].split(',');
  const idxID = headers.indexOf('ID');
  const idxDatetime = headers.indexOf('Datetime');
  const idxType = headers.indexOf('Type');
  const idxNote = headers.indexOf('Note');
  const idxFrom = headers.indexOf('From');
  const idxTo = headers.indexOf('To');
  const idxAmount = headers.indexOf('Amount (total)');
  const idxFundingSource = headers.indexOf('Funding Source');
  const idxDestination = headers.indexOf('Destination');

  // Parse transaction rows
  let totalRows = 0;
  let skippedBalance = 0;
  let skippedNoID = 0;

  for (let i = headerIndex + 1; i < lines.length; i++) {
    const line = lines[i].trim();

    // Stop at the cryptocurrency summary section or end of file
    if (!line || line.includes('Cryptocurrency summary')) break;

    const cols = parseCSVLine(line);

    // Debug: Log first few rows
    if (i < headerIndex + 5) {
      console.log(`Row ${i}: cols.length=${cols.length}, ID="${cols[idxID]}", Datetime="${cols[idxDatetime]}"`);
    }

    const id = cols[idxID];
    if (!id) {
      skippedNoID++;
      continue; // Skip rows without ID (like balance rows)
    }

    totalRows++;

    const fundingSource = cols[idxFundingSource];
    const destination = cols[idxDestination];
    const amount = cols[idxAmount];

    // **FILTER: Only include transactions that hit the bank account**
    // Skip if both funding source AND destination are "Venmo balance"
    const isVenmoBalanceOnly =
      fundingSource === 'Venmo balance' &&
      (destination === '' || destination === 'Venmo balance');

    if (isVenmoBalanceOnly) {
      skippedBalance++;
      continue;
    }

    // Determine payee (who you paid or who paid you)
    const from = cols[idxFrom];
    const to = cols[idxTo];
    const accountName = accountType === 'Family' ? 'Harris Family' : cols[idxFrom]; // Adjust based on your username

    let payee;
    if (from === 'Harris Family' || from === accountName) {
      // You paid someone
      payee = to;
    } else {
      // Someone paid you
      payee = from;
    }

    // Parse date safely
    const datetime = cols[idxDatetime];
    if (!datetime) {
      console.warn(`Row ${i}: Missing datetime, skipping`);
      continue;
    }

    const date = new Date(datetime);
    if (isNaN(date.getTime())) {
      console.warn(`Row ${i}: Invalid datetime "${datetime}", skipping`);
      continue;
    }

    // Parse amount and preserve sign for YNAB matching
    // Negative = payment/outflow, Positive = received/inflow
    let parsedAmount = parseFloat(amount.replace(/[+\-$\s,]/g, ''));
    if (amount.startsWith('-')) {
      parsedAmount = -parsedAmount;
    }

    transactions.push({
      date: date.toISOString(),
      payee: payee,
      memo: cols[idxNote],
      amount: parsedAmount,
      direction: amount.startsWith('-') ? 'paid' : 'received',
      type: cols[idxType],
      fundingSource: fundingSource,
      destination: destination
    });
  }

  console.log('=== PARSING SUMMARY ===');
  console.log(`Total CSV rows: ${totalRows}`);
  console.log(`Skipped (no ID): ${skippedNoID}`);
  console.log(`Skipped (Venmo balance only): ${skippedBalance}`);
  console.log(`Bank transactions extracted: ${transactions.length}`);

  return transactions;
}

function parseCSVLine(line) {
  const result = [];
  let current = '';
  let inQuotes = false;

  for (let i = 0; i < line.length; i++) {
    const char = line[i];

    if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === ',' && !inQuotes) {
      result.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }

  result.push(current.trim());
  return result;
}

async function handleFetchOrders(accountName) {
  const button = accountName === 'Steven' ? fetchStevenBtn : fetchAllisonBtn;

  try {
    // Clear previous errors
    hideError();

    // Check if we're on Amazon page
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

    const isOrderPage = tab.url.includes('amazon.') &&
                        (tab.url.includes('order-history') || tab.url.includes('your-orders/orders'));

    const isPaymentsPage = tab.url.includes('amazon.') &&
                           tab.url.includes('/cpe/yourpayments/transactions');

    if (!isOrderPage && !isPaymentsPage) {
      showError('Please navigate to:\n- Amazon Order History (for product names)\n- Amazon Payments (for charge dates/amounts)');
      return;
    }

    // Disable button and show progress
    button.disabled = true;
    button.textContent = 'Fetching...';
    showProgress(0);

    if (isPaymentsPage) {
      // Scrape payments
      showStatus('loading', `Scraping ${accountName}'s payment transactions...`);
      const response = await chrome.tabs.sendMessage(tab.id, {
        action: 'scrapePayments'
      });

      if (response.success) {
        const newPayments = response.payments;

        // Get existing payments for this account
        let accountPayments = accountName === 'Steven' ? stevenPayments : allisonPayments;
        const existingPaymentKeys = new Set(accountPayments.map(p => `${p.orderId}-${p.date}`));

        const uniqueNewPayments = newPayments.filter(payment =>
          !existingPaymentKeys.has(`${payment.orderId}-${payment.date}`)
        );
        accountPayments = [...accountPayments, ...uniqueNewPayments];

        // Update state and storage
        if (accountName === 'Steven') {
          stevenPayments = accountPayments;
          await chrome.storage.local.set({ stevenPayments });
        } else {
          allisonPayments = accountPayments;
          await chrome.storage.local.set({ allisonPayments });
        }

        showProgress(100);
        showStatus('success', `Added ${uniqueNewPayments.length} payments for ${accountName} (${accountPayments.length} total)`);
        updateAccountCounts();
        displayMergedResults();
      } else {
        throw new Error(response.error || 'Failed to scrape payments');
      }

    } else {
      // Scrape orders
      showStatus('loading', `Scraping ${accountName}'s order history...`);

      const response = await chrome.tabs.sendMessage(tab.id, {
        action: 'scrapeOrders',
        dateRange: '365' // Always fetch last year
      });

      if (response.success) {
        const newOrders = response.orders;

        // Get existing orders for this account
        let accountOrders = accountName === 'Steven' ? stevenOrders : allisonOrders;
        const existingOrderIds = new Set(accountOrders.map(o => o.orderId));

        const uniqueNewOrders = newOrders.filter(order => !existingOrderIds.has(order.orderId));
        accountOrders = [...accountOrders, ...uniqueNewOrders];

        // Update state and storage
        if (accountName === 'Steven') {
          stevenOrders = accountOrders;
          await chrome.storage.local.set({ stevenOrders });
        } else {
          allisonOrders = accountOrders;
          await chrome.storage.local.set({ allisonOrders });
        }

        showProgress(100);
        showStatus('success', `Added ${uniqueNewOrders.length} orders for ${accountName} (${accountOrders.length} total)`);
        updateAccountCounts();
        displayMergedResults();
      } else {
        throw new Error(response.error || 'Failed to scrape orders');
      }
    }

  } catch (error) {
    console.error('Error fetching data:', error);
    showError(`Error: ${error.message}`);
    showStatus('error', 'Failed to fetch data');
  } finally {
    button.disabled = false;
    button.textContent = `Fetch ${accountName}'s Data`;
  }
}

function displayResults(orders) {
  if (orders.length === 0) {
    showError('No orders found in the selected date range.');
    resultsSection.classList.add('hidden');
    return;
  }

  // Update order count
  orderCount.textContent = `${orders.length} order${orders.length !== 1 ? 's' : ''}`;

  // Clear previous results
  resultsBody.innerHTML = '';

  // Display first 10 orders
  const previewOrders = orders.slice(0, 10);
  previewOrders.forEach(order => {
    const row = document.createElement('tr');

    // Format date
    const date = new Date(order.date);
    const formattedDate = date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });

    // Truncate items if too long
    const itemsText = order.items.join(', ');
    const displayItems = itemsText.length > 30 ? itemsText.substring(0, 30) + '...' : itemsText;

    // Handle null/NaN totals
    const totalDisplay = (order.total == null || isNaN(order.total) || order.total === 0) ? '0.00' : order.total.toFixed(2);

    row.innerHTML = `
      <td>${formattedDate}</td>
      <td>${order.orderId}</td>
      <td title="${itemsText}">${displayItems}</td>
      <td>$${totalDisplay}</td>
    `;

    resultsBody.appendChild(row);
  });

  // Show results section and enable export
  resultsSection.classList.remove('hidden');
  exportBtn.disabled = false;
}

function handleExportCSV() {
  if (scrapedOrders.length === 0) {
    showError('No orders to export');
    return;
  }

  try {
    // Create CSV content
    const headers = ['Date', 'Order ID', 'Items', 'Item Count', 'Total', 'Currency'];
    const rows = scrapedOrders.map(order => {
      const date = new Date(order.date).toLocaleDateString('en-US');
      const items = order.items.join('; ');
      const itemCount = order.items.length;
      const total = (order.total == null || isNaN(order.total) || order.total === 0) ? '0.00' : order.total.toFixed(2);

      return [
        date,
        order.orderId,
        `"${items}"`, // Quote items to handle commas
        itemCount,
        total,
        order.currency || 'USD'
      ].join(',');
    });

    const csvContent = [headers.join(','), ...rows].join('\n');

    // Create blob and download
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');

    const timestamp = new Date().toISOString().split('T')[0];
    link.setAttribute('href', url);
    link.setAttribute('download', `amazon_orders_${timestamp}.csv`);
    link.style.display = 'none';

    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    URL.revokeObjectURL(url);

    showStatus('success', `Exported ${scrapedOrders.length} orders to CSV`);
  } catch (error) {
    console.error('Error exporting CSV:', error);
    showError(`Failed to export CSV: ${error.message}`);
  }
}

function showStatus(type, message) {
  statusArea.classList.remove('hidden');
  statusIcon.className = `status-icon ${type}`;
  statusText.textContent = message;
}

function showProgress(percent) {
  progressBar.classList.remove('hidden');
  progressFill.style.width = `${percent}%`;

  if (percent >= 100) {
    setTimeout(() => {
      progressBar.classList.add('hidden');
    }, 500);
  }
}

function showError(message) {
  errorArea.classList.remove('hidden');
  errorText.textContent = message;
}

function hideError() {
  errorArea.classList.add('hidden');
  errorText.textContent = '';
}

function handleClearCache(accountName) {
  if (accountName === 'Steven') {
    stevenOrders = [];
    stevenPayments = [];
    chrome.storage.local.set({ stevenOrders: [], stevenPayments: [] });
    showStatus('success', `Cleared Steven's cache`);
  } else if (accountName === 'Allison') {
    allisonOrders = [];
    allisonPayments = [];
    chrome.storage.local.set({ allisonOrders: [], allisonPayments: [] });
    showStatus('success', `Cleared Allison's cache`);
  } else if (accountName === 'VenmoFamily') {
    venmoFamilyTransactions = [];
    chrome.storage.local.set({ venmoFamilyTransactions: [] });
    showStatus('success', `Cleared Venmo Family cache`);
  } else if (accountName === 'VenmoAllison') {
    venmoAllisonTransactions = [];
    chrome.storage.local.set({ venmoAllisonTransactions: [] });
    showStatus('success', `Cleared Allison Venmo cache`);
  }

  updateAccountCounts();
  displayMergedResults();
}

async function handleSyncToYnab() {
  console.log('=== YNAB SYNC STARTED ===');

  // Merge data for each account separately and tag with account name
  const stevenMerged = mergeOrdersAndPayments(stevenOrders, stevenPayments);
  const allisonMerged = mergeOrdersAndPayments(allisonOrders, allisonPayments);

  const stevenData = (stevenMerged.length > 0 ? stevenMerged : stevenOrders).map(item => ({...item, accountName: 'Steven', source: 'Amazon'}));
  const allisonData = (allisonMerged.length > 0 ? allisonMerged : allisonOrders).map(item => ({...item, accountName: 'Allison', source: 'Amazon'}));

  // Add Venmo data with appropriate account names
  const venmoFamilyData = venmoFamilyTransactions.map(item => ({...item, accountName: 'Family', source: 'Venmo'}));
  const venmoAllisonData = venmoAllisonTransactions.map(item => ({...item, accountName: 'Allison', source: 'Venmo'}));

  // Combine all accounts
  const dataToSync = [...stevenData, ...allisonData, ...venmoFamilyData, ...venmoAllisonData];

  console.log(`Data to sync: ${dataToSync.length} items`);
  console.log(`  - Steven Amazon: ${stevenData.length} items (${stevenOrders.length} orders, ${stevenPayments.length} payments)`);
  console.log(`  - Allison Amazon: ${allisonData.length} items (${allisonOrders.length} orders, ${allisonPayments.length} payments)`);
  console.log(`  - Family Venmo: ${venmoFamilyData.length} transactions`);
  console.log(`  - Allison Venmo: ${venmoAllisonData.length} transactions`);

  // Debug: Show sample Venmo transactions
  if (venmoFamilyData.length > 0) {
    console.log('Sample Family Venmo transaction:', venmoFamilyData[0]);
  }

  if (dataToSync.length === 0) {
    showError('No data to sync. Please fetch data for at least one account first.');
    return;
  }

  try {
    // Get YNAB settings
    const settings = await chrome.storage.local.get([
      'ynabToken',
      'ynabBudgetId',
      'ynabAmazonAccountId',
      'ynabVenmoAccountId'
    ]);

    console.log('YNAB Settings:', {
      hasToken: !!settings.ynabToken,
      budgetId: settings.ynabBudgetId,
      amazonAccountId: settings.ynabAmazonAccountId,
      venmoAccountId: settings.ynabVenmoAccountId
    });

    if (!settings.ynabToken || !settings.ynabBudgetId || !settings.ynabAmazonAccountId || !settings.ynabVenmoAccountId) {
      showError('Please configure YNAB settings first. Click "YNAB Settings" button and select both Amazon and Venmo accounts.');
      return;
    }

    syncYnabBtn.disabled = true;
    syncYnabBtn.textContent = 'Syncing...';
    showStatus('loading', 'Fetching YNAB transactions...');

    // Fetch transactions from BOTH accounts
    const amazonTransactions = await fetchYnabTransactions(
      settings.ynabToken,
      settings.ynabBudgetId,
      settings.ynabAmazonAccountId
    );
    const venmoTransactions = await fetchYnabTransactions(
      settings.ynabToken,
      settings.ynabBudgetId,
      settings.ynabVenmoAccountId
    );

    // Tag transactions with their account type for matching
    const taggedAmazonTransactions = amazonTransactions.map(t => ({...t, accountType: 'Amazon'}));
    const taggedVenmoTransactions = venmoTransactions.map(t => ({...t, accountType: 'Venmo'}));

    console.log(`Fetched ${amazonTransactions.length} Amazon transactions and ${venmoTransactions.length} Venmo transactions from YNAB`);

    // Match data with YNAB transactions
    const matches = matchOrdersToTransactions(dataToSync, [...taggedAmazonTransactions, ...taggedVenmoTransactions]);

    console.log(`Found ${matches.length} matches between Amazon and YNAB`);

    if (matches.length === 0) {
      showStatus('success', 'No matching transactions found in YNAB.');
      return;
    }

    // Show confirmation
    const stevenAmazonMatches = matches.filter(m => m.order.accountName === 'Steven' && m.order.source === 'Amazon').length;
    const allisonAmazonMatches = matches.filter(m => m.order.accountName === 'Allison' && m.order.source === 'Amazon').length;
    const familyVenmoMatches = matches.filter(m => m.order.accountName === 'Family' && m.order.source === 'Venmo').length;
    const allisonVenmoMatches = matches.filter(m => m.order.accountName === 'Allison' && m.order.source === 'Venmo').length;

    const confirmed = confirm(
      `Found ${matches.length} matching transactions in YNAB:\n` +
      `  • Steven Amazon: ${stevenAmazonMatches}\n` +
      `  • Allison Amazon: ${allisonAmazonMatches}\n` +
      `  • Family Venmo: ${familyVenmoMatches}\n` +
      `  • Allison Venmo: ${allisonVenmoMatches}\n\n` +
      `This will update memo fields with product/transaction details.\n\n` +
      `Continue?`
    );

    if (!confirmed) {
      console.log('User cancelled sync');
      showStatus('success', 'Sync cancelled.');
      return;
    }

    // Update transactions
    let updated = 0;
    let skipped = 0;
    for (const match of matches) {
      // Use the account name from the tagged order
      const accountOwnerName = match.order.accountName;

      // Calculate what the new memo will be based on source
      console.log(`Building memo for ${accountOwnerName}, source: "${match.order.source}", payee: "${match.order.payee}"`);

      let newMemo;
      if (match.order.source === 'Venmo') {
        // Venmo format: "Family - Venmo: Payee - Note"
        const venmoDetails = match.order.payee + (match.order.memo ? ` - ${match.order.memo}` : '');
        newMemo = `${accountOwnerName} - Venmo: ${venmoDetails}`;
        console.log(`  → Venmo memo: "${newMemo}"`);
      } else {
        // Amazon format: "Steven - Product1; Product2"
        const items = match.order.items && match.order.items.length > 0 ? match.order.items : ['Amazon purchase'];
        const productList = items.slice(0, 3).join('; ');
        newMemo = `${accountOwnerName} - ${productList}`;
        console.log(`  → Amazon memo: "${newMemo}"`);
      }

      const truncatedMemo = newMemo.length > 200 ? newMemo.substring(0, 197) + '...' : newMemo;

      // Skip if memo is already identical
      if (match.transaction.memo === truncatedMemo) {
        console.log(`Skipping transaction ${updated + skipped + 1}/${matches.length} (already up-to-date):`, {
          orderId: match.order.orderId,
          account: accountOwnerName,
          memo: truncatedMemo
        });
        skipped++;
        continue;
      }

      console.log(`Updating transaction ${updated + skipped + 1}/${matches.length}:`, {
        ynabId: match.transaction.id,
        orderId: match.order.orderId,
        account: accountOwnerName,
        currentMemo: match.transaction.memo,
        newMemo: truncatedMemo
      });

      await updateYnabTransaction(
        settings.ynabToken,
        settings.ynabBudgetId,
        match.transaction,
        truncatedMemo
      );
      updated++;
      showStatus('loading', `Updating transactions... (${updated}/${matches.length - skipped})`);

      // Add delay to avoid rate limiting (200ms between requests)
      await new Promise(resolve => setTimeout(resolve, 200));
    }

    console.log(`✓ Successfully updated ${updated} transactions (${skipped} already up-to-date)`);
    showStatus('success', `✓ Updated ${updated} transactions! (${skipped} skipped)`);

  } catch (error) {
    console.error('=== YNAB SYNC FAILED ===');
    console.error('Error:', error);
    showError(`YNAB sync failed: ${error.message}`);
  } finally {
    syncYnabBtn.disabled = false;
    syncYnabBtn.textContent = 'Sync Both to YNAB';
    console.log('=== YNAB SYNC ENDED ===');
  }
}

async function fetchYnabTransactions(token, budgetId, accountId) {
  // Fetch last 3 months of transactions
  const sinceDate = new Date();
  sinceDate.setMonth(sinceDate.getMonth() - 3);
  const sinceDateStr = sinceDate.toISOString().split('T')[0];

  const response = await fetch(
    `https://api.ynab.com/v1/budgets/${budgetId}/accounts/${accountId}/transactions?since_date=${sinceDateStr}`,
    {
      headers: {
        'Authorization': `Bearer ${token}`
      }
    }
  );

  if (!response.ok) {
    throw new Error('Failed to fetch YNAB transactions');
  }

  const data = await response.json();
  return data.data.transactions;
}

function matchOrdersToTransactions(orders, transactions) {
  console.log('=== MATCHING ORDERS TO TRANSACTIONS ===');
  console.log(`Total orders to match: ${orders.length}`);
  console.log(`Total YNAB transactions: ${transactions.length}`);

  // Debug: Show sample YNAB transaction
  if (transactions.length > 0) {
    console.log('Sample YNAB transaction:', {
      date: transactions[0].date,
      amount: transactions[0].amount,
      payee: transactions[0].payee_name
    });
  }

  const matches = [];

  for (const order of orders) {
    // Use charge date if available (from payments), otherwise use order date
    const dateToMatch = order.chargeDate || order.date;
    const orderDate = new Date(dateToMatch);
    let orderAmount = Math.round((order.chargeAmount || order.total || order.amount) * 1000); // Convert to milliunits

    // CRITICAL: For Venmo Payments/Charges, flip the sign!
    // Our matching formula expects opposite signs: transaction.amount + orderAmount ≈ 0
    // - Payments/Charges: CSV and YNAB have SAME sign → flip to make opposite
    // - Standard Transfers: CSV and YNAB already have OPPOSITE signs → don't flip
    //   (Transfer OUT: CSV negative, YNAB positive inflow)
    //   (Transfer IN: CSV positive, YNAB negative outflow)
    if (order.source === 'Venmo' && order.type !== 'Standard Transfer') {
      orderAmount = -orderAmount;
    }

    const orderId = order.orderId || `${order.payee}-${order.amount}`;
    console.log(`\nTrying to match ${order.source || 'Amazon'} ${orderId}:`, {
      date: dateToMatch,
      amount: orderAmount / 1000,
      source: order.source,
      payee: order.payee
    });

    // Find matching transaction
    let matched = false;
    let venmoChecked = 0;
    for (const transaction of transactions) {
      // Only match transactions from the correct account
      // Amazon data should only match Amazon account, Venmo data should only match Venmo account
      if (order.source === 'Venmo' && transaction.accountType !== 'Venmo') continue;
      if (order.source === 'Amazon' && transaction.accountType !== 'Amazon') continue;

      if (order.source === 'Venmo') venmoChecked++;

      const transDate = new Date(transaction.date);
      const daysDiff = Math.abs((transDate - orderDate) / (1000 * 60 * 60 * 24));

      // Different matching rules for Venmo vs Amazon
      let maxDaysDiff, maxAmountDiff, payeeMatch;

      if (order.source === 'Venmo') {
        // Venmo matching: ±2 days, exact amount
        // Note: YNAB payee will be the bank name (e.g., "Coastal Credit Union"), not "Venmo"
        // So we match on date/amount only and exclude Amazon transactions
        maxDaysDiff = 2;
        maxAmountDiff = 10; // 0.01 tolerance
        // Match if payee is NOT Amazon (to avoid false matches)
        const isNotAmazon = !transaction.payee_name || (
          !transaction.payee_name.toLowerCase().includes('amazon') &&
          !transaction.payee_name.toLowerCase().includes('amzn')
        );
        payeeMatch = isNotAmazon;
      } else {
        // Amazon matching: ±2-7 days depending on charge date, exact amount, payee contains "Amazon"
        maxDaysDiff = order.chargeDate ? 2 : 7;
        maxAmountDiff = order.chargeDate ? 10 : 50;
        payeeMatch = transaction.payee_name && (
          transaction.payee_name.toLowerCase().includes('amazon') ||
          transaction.payee_name.toLowerCase().includes('amzn')
        );
      }

      const amountDiff = Math.abs(transaction.amount + orderAmount);

      // Debug logging for Venmo transactions
      if (order.source === 'Venmo' && daysDiff <= maxDaysDiff && amountDiff < maxAmountDiff) {
        console.log(`    Venmo near-match check:`, {
          ynabPayee: transaction.payee_name,
          daysDiff: daysDiff.toFixed(2),
          amountDiff: amountDiff / 1000,
          payeeMatch: payeeMatch,
          maxAmountDiff: maxAmountDiff / 1000
        });
      }

      // Match if: within allowed days AND amount matches AND payee matches
      if (daysDiff <= maxDaysDiff && amountDiff < maxAmountDiff && payeeMatch) {
        console.log(`  ✓ MATCHED to YNAB transaction:`, {
          ynabDate: transaction.date,
          ynabAmount: transaction.amount / 1000,
          ynabPayee: transaction.payee_name,
          daysDiff: daysDiff.toFixed(2),
          amountDiff: amountDiff / 1000
        });
        matches.push({
          order: order,
          transaction: transaction
        });
        matched = true;
        break; // Only match each order once
      }
    }

    if (!matched) {
      if (order.source === 'Venmo') {
        console.log(`  ✗ No match found for ${order.source} ${orderId} (checked ${venmoChecked} YNAB Venmo transactions)`);
      } else {
        console.log(`  ✗ No match found for ${order.source || 'Amazon'} ${orderId}`);
      }
    }
  }

  console.log(`\nTotal matches: ${matches.length}`);
  return matches;
}

async function updateYnabTransaction(token, budgetId, transaction, memo) {
  console.log(`  → Sending to YNAB API: "${memo}"`);
  const response = await fetch(
    `https://api.ynab.com/v1/budgets/${budgetId}/transactions/${transaction.id}`,
    {
      method: 'PUT',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        transaction: {
          memo: memo
        }
      })
    }
  );

  if (!response.ok) {
    throw new Error(`Failed to update transaction ${transaction.id}`);
  }

  return response.json();
}
