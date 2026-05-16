// Venmo Transaction Scraper
// Scrapes the Venmo feed to get payee and memo/note information

console.log('YNAB Helper: Venmo scraper loaded');

// Listen for messages from popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'scrapeVenmo') {
    console.log('YNAB Helper: Received Venmo scrape request');

    scrapeVenmo()
      .then(transactions => {
        console.log('YNAB Helper: Successfully scraped', transactions.length, 'Venmo transactions');
        sendResponse({ success: true, transactions: transactions });
      })
      .catch(error => {
        console.error('YNAB Helper: Error scraping Venmo:', error);
        sendResponse({ success: false, error: error.message });
      });

    return true; // Async response
  }
});

async function scrapeVenmo() {
  const transactions = [];

  // Wait for page to load
  await waitForPageLoad();

  // Find all transaction articles
  const articles = document.querySelectorAll('article[aria-labelledby^="storyHeadlineId-"]');
  console.log('YNAB Helper: Found', articles.length, 'Venmo transaction articles');

  if (articles.length === 0) {
    throw new Error('No Venmo transactions found. Make sure you are on venmo.com and logged in.');
  }

  for (const article of articles) {
    try {
      const transaction = extractVenmoTransaction(article);

      console.log('YNAB Helper: Extracted transaction:', {
        payee: transaction.payee,
        amount: transaction.amount,
        memo: transaction.memo,
        date: transaction.date,
        direction: transaction.direction
      });

      if (transaction.payee && transaction.amount) {
        transactions.push(transaction);
      } else {
        console.warn('YNAB Helper: Skipping transaction - missing required fields:', {
          hasPayee: !!transaction.payee,
          hasAmount: !!transaction.amount
        });
      }
    } catch (error) {
      console.warn('YNAB Helper: Error parsing Venmo transaction:', error);
    }
  }

  return transactions;
}

function extractVenmoTransaction(article) {
  const transaction = {
    payee: null,
    memo: null,
    amount: null,
    date: null,
    direction: null // 'paid', 'charged', 'received'
  };

  // Extract headline to determine payee and direction
  // Look for the div with id starting with "storyHeadlineId-"
  const headlineDiv = article.querySelector('[id^="storyHeadlineId-"]');
  if (headlineDiv) {
    const headlineText = headlineDiv.textContent;

    // Get all links with strong tags
    const strongLinks = headlineDiv.querySelectorAll('a strong');

    // Determine direction and extract payee
    if (headlineText.includes('You paid') || headlineText.includes('You charged')) {
      // You initiated - the recipient is the second strong link
      if (strongLinks.length >= 2) {
        transaction.payee = strongLinks[1].textContent;
        transaction.direction = headlineText.includes('paid') ? 'paid' : 'charged';
      }
    } else if (headlineText.includes('paid you') || headlineText.includes('charged you')) {
      // Someone paid/charged you - the sender is the first strong link
      if (strongLinks.length >= 1 && strongLinks[0].textContent !== 'You') {
        transaction.payee = strongLinks[0].textContent;
        transaction.direction = headlineText.includes('paid you') ? 'received' : 'charged_by';
      }
    } else if (headlineText.includes('You') && headlineText.includes('paid')) {
      // Fallback pattern: extract any name that's not "You"
      for (const strongLink of strongLinks) {
        const name = strongLink.textContent;
        if (name !== 'You') {
          transaction.payee = name;
          transaction.direction = headlineText.indexOf('You') < headlineText.indexOf(name) ? 'paid' : 'received';
          break;
        }
      }
    }
  }

  // Extract amount - try hidden span first, then visible divs
  const hiddenAmount = article.querySelector('[id^="storyHidenAmount-"]');
  if (hiddenAmount) {
    const amountText = hiddenAmount.textContent;
    const amountMatch = amountText.match(/\$?([\d,]+\.?\d*)/);
    if (amountMatch) {
      transaction.amount = parseFloat(amountMatch[1].replace(/,/g, ''));
    }
  }

  // If no hidden amount, try visible amount divs
  if (!transaction.amount) {
    const visibleAmount = article.querySelector('[class*="css-r8i943"], [class*="css-e2b8t1"]');
    if (visibleAmount) {
      const amountText = visibleAmount.textContent;
      const amountMatch = amountText.match(/[+-]?\s*\$?([\d,]+\.?\d*)/);
      if (amountMatch) {
        transaction.amount = parseFloat(amountMatch[1].replace(/,/g, ''));
      }
    }
  }

  // Extract memo/note
  const memoElement = article.querySelector('[class*="storyContent"]');
  if (memoElement) {
    transaction.memo = memoElement.textContent.trim();
  }

  // Extract date - convert relative time to absolute date
  const dateElement = article.querySelector('[aria-label]');
  if (dateElement) {
    const relativeTime = dateElement.getAttribute('aria-label');
    transaction.date = convertRelativeTimeToDate(relativeTime);
  }

  return transaction;
}

function convertRelativeTimeToDate(relativeTime) {
  const now = new Date();

  // Handle "X hours ago" -> "Xh"
  const hoursMatch = relativeTime.match(/(\d+)\s*h/);
  if (hoursMatch) {
    const hours = parseInt(hoursMatch[1]);
    const date = new Date(now.getTime() - (hours * 60 * 60 * 1000));
    return date.toISOString();
  }

  // Handle "X days ago" -> "Xd"
  const daysMatch = relativeTime.match(/(\d+)\s*d/);
  if (daysMatch) {
    const days = parseInt(daysMatch[1]);
    const date = new Date(now.getTime() - (days * 24 * 60 * 60 * 1000));
    return date.toISOString();
  }

  // Handle absolute dates like "Jan 25"
  const absoluteMatch = relativeTime.match(/([A-Z][a-z]{2})\s+(\d{1,2})/);
  if (absoluteMatch) {
    const month = absoluteMatch[1];
    const day = absoluteMatch[2];
    const currentYear = now.getFullYear();
    const currentMonth = now.getMonth();

    const monthNum = new Date(Date.parse(month + " 1, 2000")).getMonth();

    // If the month is in the future, it's from last year
    const year = monthNum > currentMonth ? currentYear - 1 : currentYear;

    const date = new Date(`${month} ${day}, ${year}`);
    return date.toISOString();
  }

  // Default to now if can't parse
  return now.toISOString();
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
