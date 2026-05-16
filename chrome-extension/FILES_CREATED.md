# YNAB Helper Extension - Files Created

All Chrome extension files have been successfully created at:
`C:\Users\SHarris\_claudeprojects\ynab_helper`

## File Structure

```
ynab_helper/
├── manifest.json                 # Chrome extension manifest (v3)
├── README.md                     # Main documentation
├── INSTALLATION.md              # Installation guide
├── FILES_CREATED.md            # This file
│
├── popup/
│   ├── popup.html              # Extension popup UI
│   ├── popup.css               # Popup styling
│   └── popup.js                # Popup logic and CSV export
│
├── content/
│   └── amazon-scraper.js       # Content script for scraping Amazon
│
├── background/
│   └── service-worker.js       # Background service worker
│
├── icons/
│   └── icon-template.html      # Template for creating icon PNGs
│
└── test/
    └── mock-amazon-page.html   # Test page for development
```

## Core Features Implemented

### 1. manifest.json
- Chrome Manifest v3 compliant
- Permissions: activeTab, storage, scripting
- Host permissions for Amazon (.com, .ca, .co.uk)
- Content scripts auto-inject on order history pages
- Action popup configured

### 2. popup/popup.html
- Clean UI with 350px width
- Date range selector (30/90/365 days, all)
- Fetch Orders button
- Status area with progress indicator
- Results preview table (scrollable, first 10 orders)
- Export CSV button
- Error display area

### 3. popup/popup.css
- Modern gradient header
- Responsive button styling with hover effects
- Scrollable table with custom scrollbar
- Status indicators (loading/success/error)
- Progress bar animation
- Clean typography and spacing

### 4. popup/popup.js
- Tab querying for Amazon pages
- Message passing to content script
- Chrome storage integration for caching
- Results preview display
- CSV export with proper formatting
- Error handling and user feedback
- Load cached data on popup open

### 5. content/amazon-scraper.js
- Multiple selector strategies for robustness
- Extracts: date, order ID, items, total
- Date filtering based on range
- Price parsing with currency symbols
- Handles various Amazon page layouts
- Comprehensive error handling
- Console logging for debugging

### 6. background/service-worker.js
- Installation event handling
- Message listener for cache management
- Context menu integration
- Keep-alive mechanism
- Storage initialization

## Next Steps

1. **Create Icon Files**
   - Open `icons/icon-template.html` in browser
   - Save the SVG icons as PNG files (16x16, 48x48, 128x128)
   - Or create custom icons using design tools

2. **Load Extension**
   - Open Chrome: `chrome://extensions/`
   - Enable Developer mode
   - Click "Load unpacked"
   - Select the `ynab_helper` folder

3. **Test the Extension**
   - Option A: Use the mock page at `test/mock-amazon-page.html`
   - Option B: Go to actual Amazon order history
   - Click extension icon and try scraping

4. **Export Data**
   - Click "Fetch Orders"
   - Review preview
   - Click "Export CSV"
   - Import to YNAB

## Technical Details

### Scraping Strategy
The extension uses multiple CSS selector strategies to handle Amazon's varying page structures:
- Order cards: `.order-card`, `[data-component="order"]`
- Dates: `.order-date-invoice-item`, various date selectors
- IDs: `.order-number`, order ID patterns
- Items: `.yohtmlc-product-title`, product links
- Totals: `.yohtmlc-order-total`, price selectors

### Data Format
CSV Export includes:
- Date (MM/DD/YYYY)
- Order ID (Amazon format: 123-4567890-1234567)
- Items (semicolon-separated, quoted)
- Item Count
- Total (decimal format)
- Currency (default: USD)

### Storage
- Uses `chrome.storage.local` for caching
- Stores: `cachedOrders` array and `cacheTimestamp`
- Auto-loads cached data on popup open
- Cache persists until new scrape or extension reload

## Debugging

### Browser Console
Press F12 on the popup or Amazon page to view console logs:
- Content script logs: "YNAB Helper: ..."
- Service worker logs: Check service worker console
- Popup logs: Check popup inspection console

### Common Issues
1. **No orders found**: Verify Amazon page structure matches selectors
2. **Missing data**: Check console for parsing errors
3. **Extension not loading**: Verify icon files exist

## Customization

### Update Selectors
Edit `content/amazon-scraper.js` functions:
- `findOrderCards()` - Order container selectors
- `extractOrderDate()` - Date selectors
- `extractOrderId()` - Order ID selectors
- `extractItems()` - Product title selectors
- `extractTotal()` - Price selectors

### Styling
Edit `popup/popup.css` to customize:
- Colors and gradients
- Button styles
- Table appearance
- Spacing and layout

### Export Format
Edit `handleExportCSV()` in `popup/popup.js` to:
- Add/remove CSV columns
- Change date format
- Modify delimiter
- Adjust file naming

## Security & Privacy

- No external server communication
- All processing happens locally
- No data sent outside the browser
- Only runs on Amazon order history pages
- Uses minimal required permissions

## License

MIT License - Free to use and modify
