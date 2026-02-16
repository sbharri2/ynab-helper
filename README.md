# YNAB Helper - Amazon Scraper Chrome Extension

A Chrome extension that scrapes Amazon order history and exports data to CSV for easy import into YNAB (You Need A Budget).

## Features

- Scrape Amazon order history directly from the browser
- Filter orders by date range (30 days, 90 days, 1 year, or all)
- Preview scraped orders in the popup
- Export to CSV format compatible with YNAB
- Cache results for quick access
- Clean, minimal UI

## Installation

### Development Mode

1. Download or clone this repository to your local machine
2. Open Chrome and navigate to `chrome://extensions/`
3. Enable "Developer mode" (toggle in top-right corner)
4. Click "Load unpacked"
5. Select the `ynab_helper` folder
6. The extension should now appear in your extensions list

### Adding Icons

Before using the extension, you'll need to add icon files to the `icons` folder:
- Create a folder named `icons` in the extension directory
- Add three PNG images:
  - `icon16.png` (16x16 pixels)
  - `icon48.png` (48x48 pixels)
  - `icon128.png` (128x128 pixels)

You can create simple placeholder icons or design custom ones. The icons represent the extension in various places in Chrome.

## Usage

1. Navigate to Amazon Order History:
   - Go to Amazon.com
   - Click "Returns & Orders" in the top navigation
   - You should be on a URL like: `https://www.amazon.com/gp/css/order-history`

2. Click the YNAB Helper extension icon in your Chrome toolbar

3. Select your desired date range from the dropdown (default: 90 days)

4. Click "Fetch Orders" to scrape the current page

5. Preview the first 10 orders in the popup table

6. Click "Export CSV" to download a CSV file with all scraped orders

## CSV Format

The exported CSV includes the following columns:
- **Date**: Order date in MM/DD/YYYY format
- **Order ID**: Amazon order number
- **Items**: Semicolon-separated list of items in the order
- **Item Count**: Number of items in the order
- **Total**: Order total amount
- **Currency**: Currency code (default: USD)

## Importing to YNAB

1. Open the exported CSV in a spreadsheet application
2. Format the data as needed for YNAB import
3. Import into YNAB following their CSV import guidelines

## Limitations

- Only scrapes orders visible on the current page
- Amazon's page structure may change, requiring updates to selectors
- Does not handle pagination automatically (refresh and re-scrape for more orders)
- Works best with US Amazon (.com), but includes support for .ca and .co.uk domains

## Privacy

This extension:
- Only runs on Amazon order history pages
- Does not send data to external servers
- Stores scraped data locally in Chrome storage
- All processing happens locally in your browser

## Troubleshooting

**No orders found:**
- Make sure you're on the Amazon Order History page
- Check that orders are visible on the page (scroll down if needed)
- Try refreshing the page and running the scraper again

**Missing data:**
- Amazon's page structure varies; some orders may not parse correctly
- Check the browser console (F12) for error messages
- The extension tries multiple selectors but may not catch all variations

**Extension not working:**
- Reload the extension from `chrome://extensions/`
- Check that you have the latest version of Chrome
- Verify all extension files are present and properly loaded

## Development

### File Structure

```
ynab_helper/
├── manifest.json              # Extension configuration
├── popup/
│   ├── popup.html            # Popup UI
│   ├── popup.css             # Popup styling
│   └── popup.js              # Popup logic
├── content/
│   └── amazon-scraper.js     # Content script for scraping
├── background/
│   └── service-worker.js     # Background service worker
├── icons/
│   ├── icon16.png
│   ├── icon48.png
│   └── icon128.png
└── README.md
```

### Updating Selectors

If Amazon changes their page structure, you may need to update the CSS selectors in `content/amazon-scraper.js`:

- `findOrderCards()`: Selectors for order card containers
- `extractOrderDate()`: Selectors for order dates
- `extractOrderId()`: Selectors for order IDs
- `extractItems()`: Selectors for product titles
- `extractTotal()`: Selectors for order totals

## License

MIT License - feel free to modify and use as needed.

## Contributing

Contributions welcome! Please feel free to submit pull requests or open issues for bugs and feature requests.
