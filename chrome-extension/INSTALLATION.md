# YNAB Helper - Installation Guide

## Quick Start

### Step 1: Create Icon Files

Before installing the extension, you need to add icon images. You have two options:

#### Option A: Use the Icon Template (Recommended for Quick Setup)

1. Open `icons/icon-template.html` in your browser
2. Right-click each SVG icon and save as PNG:
   - Save first icon as `icon16.png`
   - Save second icon as `icon48.png`
   - Save third icon as `icon128.png`
3. Save all three files in the `icons` folder

#### Option B: Create Custom Icons

1. Create three PNG images with these exact dimensions:
   - `icon16.png` - 16x16 pixels
   - `icon48.png` - 48x48 pixels
   - `icon128.png` - 128x128 pixels
2. Save them in the `icons` folder

**Note:** If you skip this step, Chrome will show an error when loading the extension.

### Step 2: Load Extension in Chrome

1. Open Google Chrome
2. Navigate to `chrome://extensions/`
3. Enable **Developer mode** (toggle switch in top-right corner)
4. Click **Load unpacked** button
5. Navigate to and select the `ynab_helper` folder
6. The extension should now appear in your extensions list

### Step 3: Pin the Extension (Optional but Recommended)

1. Click the puzzle piece icon in Chrome toolbar (Extensions)
2. Find "YNAB Helper - Amazon Scraper"
3. Click the pin icon to pin it to your toolbar

## First Time Usage

1. Go to [Amazon.com](https://www.amazon.com)
2. Click **Returns & Orders** in the top menu
3. Wait for the order history page to load
4. Click the YNAB Helper extension icon in your toolbar
5. Select date range (default: 90 days)
6. Click **Fetch Orders**
7. Review the results in the preview table
8. Click **Export CSV** to download your data

## Troubleshooting Installation

### Error: "Failed to load extension"
- Make sure all files are present in the correct folder structure
- Check that icon files exist in the `icons` folder
- Try reloading the extension

### Error: "Manifest file is missing or unreadable"
- Verify `manifest.json` exists in the root folder
- Check that the file is valid JSON (no syntax errors)

### Extension icon doesn't appear
- Make sure you've created the icon PNG files
- Reload the extension from `chrome://extensions/`

### Can't find the extension after installation
- Check that it's enabled in `chrome://extensions/`
- Pin it to your toolbar for easy access

## Uninstallation

1. Go to `chrome://extensions/`
2. Find "YNAB Helper - Amazon Scraper"
3. Click **Remove**
4. Confirm removal

All cached data will be removed from Chrome storage.

## Updating the Extension

1. Make changes to the extension files
2. Go to `chrome://extensions/`
3. Click the refresh icon on the YNAB Helper card
4. Changes will take effect immediately

## File Structure Verification

Your folder structure should look like this:

```
ynab_helper/
├── manifest.json
├── README.md
├── INSTALLATION.md
├── popup/
│   ├── popup.html
│   ├── popup.css
│   └── popup.js
├── content/
│   └── amazon-scraper.js
├── background/
│   └── service-worker.js
└── icons/
    ├── icon16.png
    ├── icon48.png
    ├── icon128.png
    └── icon-template.html
```

## Next Steps

- Read the [README.md](README.md) for usage instructions
- Check browser console (F12) for debugging if issues occur
- Customize selectors in `amazon-scraper.js` if Amazon changes their page layout

## Support

If you encounter issues:
1. Check the browser console for error messages (F12 → Console tab)
2. Verify you're on the correct Amazon page
3. Make sure the extension has permissions to run on Amazon.com
4. Try reloading both the extension and the Amazon page
