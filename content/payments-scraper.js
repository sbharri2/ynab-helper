// Amazon Payments/Transactions Scraper
// Scrapes the payments page to get actual charge dates and amounts

console.log('YNAB Helper: Payments scraper loaded');

// Listen for messages from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'scrapePayments') {
    console.log('YNAB Helper: Received payments scrape request');

    scrapePayments()
      .then(payments => {
        console.log('YNAB Helper: Successfully scraped', payments.length, 'payment transactions');
        sendResponse({ success: true, payments: payments });
      })
      .catch(error => {
        console.error('YNAB Helper: Error scraping payments:', error);
        sendResponse({ success: false, error: error.message });
      });

    return true; // Async response
  }
});

async function scrapePayments() {
  const payments = [];

  // Wait for page to load
  await waitForPageLoad();

  // Find all payment transaction rows
  const paymentRows = findPaymentRows();
  console.log('YNAB Helper: Found', paymentRows.length, 'payment rows');

  if (paymentRows.length === 0) {
    throw new Error('No payment transactions found. Make sure you are on the Amazon Payments page.');
  }

  let currentDate = null;

  for (const row of paymentRows) {
    try {
      // Check if there's a date heading before this row
      const dateHeading = findDateHeadingBefore(row);
      if (dateHeading) {
        currentDate = dateHeading;
      }

      const payment = extractPaymentData(row);

      // Use the current section date
      if (!payment.date && currentDate) {
        payment.date = currentDate;
      }

      // Only add if we have all required fields
      if (payment.date && payment.amount && payment.orderId) {
        payments.push(payment);
      } else {
        console.warn('YNAB Helper: Skipping incomplete payment:', payment);
      }

    } catch (error) {
      console.warn('YNAB Helper: Error parsing payment row:', error);
    }
  }

  return payments;
}

function findDateHeadingBefore(element) {
  // Walk up and look for date headings in previous siblings or parent elements
  let current = element.previousElementSibling;

  while (current) {
    const text = current.textContent;
    const dateMatch = text.match(/([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})/);
    if (dateMatch) {
      return new Date(`${dateMatch[1]} ${dateMatch[2]}, ${dateMatch[3]}`).toISOString();
    }
    current = current.previousElementSibling;
  }

  // Also check parent's previous siblings
  if (element.parentElement) {
    return findDateHeadingBefore(element.parentElement);
  }

  return null;
}

function findPaymentRows() {
  // Use specific Amazon payments page container class
  const paymentElements = document.querySelectorAll('.apx-transactions-line-item-component-container');

  if (paymentElements.length > 0) {
    console.log('YNAB Helper: Found payment elements using .apx-transactions-line-item-component-container');
    return Array.from(paymentElements);
  }

  // Fallback: look for elements with pmts-portal-component class
  const fallbackElements = document.querySelectorAll('.pmts-portal-component');
  const uniqueTransactions = [];
  const seenOrderIds = new Set();

  fallbackElements.forEach(elem => {
    const orderIdMatch = elem.textContent.match(/Order #(\d{3}-\d{7}-\d{7})/);
    if (orderIdMatch && !seenOrderIds.has(orderIdMatch[1])) {
      seenOrderIds.add(orderIdMatch[1]);
      uniqueTransactions.push(elem);
    }
  });

  console.log('YNAB Helper: Found payment elements using fallback method');
  return uniqueTransactions;
}

function extractPaymentData(element) {
  const text = element.textContent;

  const payment = {
    date: null,
    amount: null,
    orderId: null,
    payee: null,
    cardLast4: null
  };

  // Extract amount - look for the amount in the right-aligned column
  // Structure: <div class="a-text-right"><span class="a-size-base-plus a-text-bold">-$76.39</span></div>
  const rightColumn = element.querySelector('.a-text-right');
  if (rightColumn) {
    const amountText = rightColumn.textContent.trim();
    const amountMatch = amountText.match(/-?\$?([\d,]+\.\d{2})/);
    if (amountMatch) {
      payment.amount = parseFloat(amountMatch[1].replace(/,/g, ''));
    }
  }

  // Fallback: search all text for amount pattern
  if (!payment.amount) {
    const text = element.textContent;
    // Look for negative amount (charge)
    const amountMatch = text.match(/-\$([\d,]+\.\d{2})/);
    if (amountMatch) {
      payment.amount = parseFloat(amountMatch[1].replace(/,/g, ''));
    }
  }

  // Extract Order ID - look for the link with orderID
  const orderLink = element.querySelector('a[href*="orderID="]');
  if (orderLink) {
    const orderIdMatch = orderLink.textContent.match(/Order #(\d{3}-\d{7}-\d{7})/);
    if (orderIdMatch) {
      payment.orderId = orderIdMatch[1];
    }
  }

  // Extract payee/merchant - the last text element
  // Look for spans that might contain merchant name
  const spans = element.querySelectorAll('.a-size-base');
  for (let i = spans.length - 1; i >= 0; i--) {
    const spanText = spans[i].textContent.trim();
    if (spanText && !spanText.includes('****') && !spanText.includes('Order')) {
      payment.payee = spanText;
      break;
    }
  }

  // Extract card info (e.g., "Prime Visa ****8477")
  const cardMatch = text.match(/([A-Za-z\s]+)\s+\*\*\*\*(\d{4})/);
  if (cardMatch) {
    payment.cardLast4 = cardMatch[2];
  }

  return payment;
}

function waitForPageLoad() {
  return new Promise((resolve) => {
    if (document.readyState === 'complete') {
      resolve();
    } else {
      window.addEventListener('load', resolve);
    }
  });
}
