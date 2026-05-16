// Amazon Order History Scraper
// This content script runs on Amazon order history pages and scrapes order data

console.log('YNAB Helper: Amazon scraper content script loaded');

// Listen for messages from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'scrapeOrders') {
    console.log('YNAB Helper: Received scrape request with date range:', request.dateRange);

    // Scrape orders asynchronously
    scrapeOrders(request.dateRange)
      .then(orders => {
        console.log('YNAB Helper: Successfully scraped', orders.length, 'orders');
        sendResponse({ success: true, orders: orders });
      })
      .catch(error => {
        console.error('YNAB Helper: Error scraping orders:', error);
        sendResponse({ success: false, error: error.message });
      });

    // Return true to indicate async response
    return true;
  }
});

async function scrapeOrders(dateRange) {
  const orders = [];
  const cutoffDate = calculateCutoffDate(dateRange);

  // Wait for page to be fully loaded
  await waitForPageLoad();

  // Find all order cards on the page
  const orderCards = findOrderCards();
  console.log('YNAB Helper: Found', orderCards.length, 'order cards on page');

  if (orderCards.length === 0) {
    throw new Error('No order cards found. Make sure you are on the Amazon Order History page.');
  }

  // Log the first order card's HTML for debugging
  if (orderCards.length > 0) {
    console.log('YNAB Helper: First order card HTML (truncated):', orderCards[0].outerHTML.substring(0, 500));
  }

  for (const card of orderCards) {
    try {
      const orderData = extractOrderData(card);

      // Filter by date if specified
      if (dateRange !== 'all') {
        const orderDate = new Date(orderData.date);
        console.log('YNAB Helper: Checking date filter - orderDate:', orderDate, 'cutoffDate:', cutoffDate, 'passes:', orderDate >= cutoffDate);
        if (orderDate < cutoffDate) {
          console.log('YNAB Helper: Order filtered out (too old)');
          continue; // Skip orders outside date range
        }
      }

      orders.push(orderData);
      console.log('YNAB Helper: Order accepted!');
    } catch (error) {
      console.warn('YNAB Helper: Error parsing order card:', error);
      // Continue with next order
    }
  }

  return orders;
}

function findOrderCards() {
  // Try multiple selectors to find order cards
  const selectors = [
    '.order-card',
    '[data-component="order"]',
    '.order',
    '.a-box-group.a-spacing-base.order',
    '.js-order-card'
  ];

  for (const selector of selectors) {
    const cards = document.querySelectorAll(selector);
    if (cards.length > 0) {
      console.log('YNAB Helper: Found order cards using selector:', selector);
      return Array.from(cards);
    }
  }

  // Fallback: try to find elements that look like order containers
  const possibleOrders = document.querySelectorAll('[class*="order"]');
  console.log('YNAB Helper: Fallback found', possibleOrders.length, 'elements with "order" in class');
  return Array.from(possibleOrders);
}

function extractOrderData(orderCard) {
  const orderData = {
    date: null,
    orderId: null,
    items: [],
    total: 0,
    currency: 'USD'
  };

  // Extract order date
  orderData.date = extractOrderDate(orderCard);
  console.log('YNAB Helper: Extracted date:', orderData.date);

  // Extract order ID
  orderData.orderId = extractOrderId(orderCard);
  console.log('YNAB Helper: Extracted order ID:', orderData.orderId);

  // Extract items
  orderData.items = extractItems(orderCard);
  console.log('YNAB Helper: Extracted items:', orderData.items);

  // Extract total
  orderData.total = extractTotal(orderCard);
  console.log('YNAB Helper: Extracted total:', orderData.total);

  // Validate required fields
  if (!orderData.date || !orderData.orderId) {
    console.warn('YNAB Helper: Validation failed - date:', orderData.date, 'orderId:', orderData.orderId);
    throw new Error('Missing required order data (date or order ID)');
  }

  return orderData;
}

function extractOrderDate(orderCard) {
  // First, try to find "Order placed" date in the text (most accurate)
  const text = orderCard.textContent;
  const orderPlacedMatch = text.match(/Order placed[:\s]+([A-Z][a-z]+ \d{1,2},? \d{4})/i);
  if (orderPlacedMatch) {
    console.log('YNAB Helper: Raw date text from "Order placed" pattern:', orderPlacedMatch[1]);
    const parsedDate = parseAmazonDate(orderPlacedMatch[1]);
    if (parsedDate) {
      console.log('YNAB Helper: Parsed date from "Order placed":', parsedDate);
      return parsedDate;
    }
  }

  // Also try without year (Amazon often omits the year)
  const orderPlacedMatchNoYear = text.match(/Order placed[:\s]+([A-Z][a-z]+ \d{1,2})/i);
  if (orderPlacedMatchNoYear) {
    console.log('YNAB Helper: Raw date text from "Order placed" pattern (no year):', orderPlacedMatchNoYear[1]);
    const parsedDate = parseAmazonDate(orderPlacedMatchNoYear[1]);
    if (parsedDate) {
      console.log('YNAB Helper: Parsed date from "Order placed":', parsedDate);
      return parsedDate;
    }
  }

  // Try multiple selectors for order date
  const dateSelectors = [
    '.order-date-invoice-item',
    '.a-color-secondary.value',
    '[class*="order-date"]',
    '.a-span4 .a-color-secondary',
    '.delivery-box__primary-text' // Fallback: delivery date
  ];

  for (const selector of dateSelectors) {
    const dateElement = orderCard.querySelector(selector);
    if (dateElement) {
      const dateText = dateElement.textContent.trim();
      console.log('YNAB Helper: Raw date text from selector', selector, ':', dateText);
      const parsedDate = parseAmazonDate(dateText);
      if (parsedDate) {
        console.log('YNAB Helper: Parsed date:', parsedDate);
        return parsedDate;
      }
    }
  }

  // Try to find any date-like text in the card (text already declared above)
  const dateMatch = text.match(/(?:Ordered on |Order placed )?([A-Z][a-z]+ \d{1,2},? \d{4})/i);
  if (dateMatch) {
    console.log('YNAB Helper: Raw date text from regex:', dateMatch[1]);
    return parseAmazonDate(dateMatch[1]);
  }

  return null;
}

function extractOrderId(orderCard) {
  // Try multiple selectors for order ID
  const idSelectors = [
    '.order-number',
    '[class*="order-number"]',
    '.yohtmlc-order-id',
    'bdi'
  ];

  for (const selector of idSelectors) {
    const idElement = orderCard.querySelector(selector);
    if (idElement) {
      const text = idElement.textContent.trim();
      // Look for order ID pattern (usually starts with digits and dashes)
      const match = text.match(/(\d{3}-\d{7}-\d{7})/);
      if (match) {
        return match[1];
      }
    }
  }

  // Try to find order ID in the entire card text
  const text = orderCard.textContent;
  const idMatch = text.match(/(?:ORDER #|Order #)?(\d{3}-\d{7}-\d{7})/i);
  if (idMatch) {
    return idMatch[1];
  }

  // Fallback: use a unique identifier from the card
  const fallbackId = orderCard.getAttribute('data-order-id') ||
                     orderCard.id ||
                     'UNKNOWN-' + Math.random().toString(36).substring(7);
  return fallbackId;
}

function extractItems(orderCard) {
  const items = [];

  // Try multiple selectors for product titles
  const itemSelectors = [
    '.yohtmlc-product-title',
    '.a-link-normal[href*="/gp/product/"]',
    '.product-link',
    '[class*="product-title"]',
    'a[href*="/dp/"]'
  ];

  for (const selector of itemSelectors) {
    const itemElements = orderCard.querySelectorAll(selector);
    if (itemElements.length > 0) {
      itemElements.forEach(elem => {
        const itemText = elem.textContent.trim();
        // Avoid duplicate items and empty strings
        if (itemText && !items.includes(itemText) && itemText.length < 200) {
          items.push(itemText);
        }
      });

      if (items.length > 0) {
        break; // Found items, stop searching
      }
    }
  }

  // If no items found, try to extract from images alt text
  if (items.length === 0) {
    const images = orderCard.querySelectorAll('img[alt]');
    images.forEach(img => {
      const alt = img.alt.trim();
      if (alt && alt.length > 5 && alt.length < 200 && !items.includes(alt)) {
        items.push(alt);
      }
    });
  }

  return items.length > 0 ? items : ['Unknown Item'];
}

function extractTotal(orderCard) {
  // Look specifically for "Total" followed by price (most reliable)
  const text = orderCard.textContent;
  const totalMatch = text.match(/Total[:\s]*\$?([\d,]+\.?\d{2})/i);
  if (totalMatch) {
    const amount = parsePrice(totalMatch[1]);
    if (amount > 0) {
      console.log('YNAB Helper: Found total using "Total" pattern:', amount, 'from text:', totalMatch[0]);
      return amount;
    }
  }

  // Try multiple selectors for order total
  const totalSelectors = [
    '.yohtmlc-order-total',
    '.grand-total-price',
    '[class*="order-total"]',
    '[class*="grand-total"]',
    '.a-color-price'
  ];

  for (const selector of totalSelectors) {
    const totalElements = orderCard.querySelectorAll(selector);
    for (const elem of totalElements) {
      const elemText = elem.textContent.trim();
      console.log('YNAB Helper: Checking total selector', selector, 'text:', elemText);
      const amount = parsePrice(elemText);
      if (amount > 0) {
        console.log('YNAB Helper: Found total using selector', selector, ':', amount);
        return amount;
      }
    }
  }

  // Try to find any price in the card (look for $ or currency symbols)
  const priceMatches = text.match(/\$[\d,]+\.?\d{0,2}/g);
  console.log('YNAB Helper: Found price matches:', priceMatches);
  if (priceMatches && priceMatches.length > 0) {
    // Return the largest price found (likely the total)
    const prices = priceMatches.map(parsePrice);
    const maxPrice = Math.max(...prices);
    console.log('YNAB Helper: Using max price from fallback:', maxPrice);
    return maxPrice;
  }

  console.log('YNAB Helper: No total found, returning 0');
  return 0;
}

function parseAmazonDate(dateText) {
  // Remove common prefixes and "Arriving"
  let cleaned = dateText.replace(/^(Ordered on|Order placed|Delivered|Arriving)\s*/i, '').trim();

  // Check if the date includes a year
  const hasYear = /\d{4}/.test(cleaned);

  if (!hasYear) {
    // Date doesn't have a year (e.g., "February 28" or "January 14")
    // Add the current year
    const currentYear = new Date().getFullYear();
    const currentMonth = new Date().getMonth(); // 0-11

    // Parse the month from the date string
    const monthMatch = cleaned.match(/([A-Z][a-z]+)/i);
    if (monthMatch) {
      const monthName = monthMatch[1];
      const monthNum = new Date(Date.parse(monthName + " 1, 2000")).getMonth();

      // If the order month is in the future relative to current month,
      // it's probably from last year (e.g., December order seen in January)
      if (monthNum > currentMonth + 1) {
        cleaned = `${cleaned} ${currentYear - 1}`;
      } else {
        cleaned = `${cleaned} ${currentYear}`;
      }
    } else {
      // Fallback: just add current year
      cleaned = `${cleaned} ${currentYear}`;
    }
  }

  // Try to parse the date
  const date = new Date(cleaned);
  if (!isNaN(date.getTime())) {
    return date.toISOString();
  }

  return null;
}

function parsePrice(priceText) {
  // Remove currency symbols and commas, extract number
  const match = priceText.match(/[\d,]+\.?\d{0,2}/);
  if (match) {
    const cleaned = match[0].replace(/,/g, '');
    return parseFloat(cleaned);
  }
  return 0;
}

function calculateCutoffDate(dateRange) {
  const today = new Date();

  switch (dateRange) {
    case '30':
      return new Date(today.setDate(today.getDate() - 30));
    case '90':
      return new Date(today.setDate(today.getDate() - 90));
    case '365':
      return new Date(today.setFullYear(today.getFullYear() - 1));
    case 'all':
    default:
      return new Date(0); // Beginning of time
  }
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
