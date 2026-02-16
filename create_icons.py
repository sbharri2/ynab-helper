#!/usr/bin/env python3
"""Create placeholder PNG icons for the YNAB Helper extension."""

from PIL import Image, ImageDraw, ImageFont
import os

def create_icon(size, filename):
    """Create a simple gradient icon with 'Y' text."""
    # Create image with gradient background
    img = Image.new('RGB', (size, size), color='#667eea')
    draw = ImageDraw.Draw(img)

    # Draw gradient effect (simple two-tone)
    for y in range(size):
        r1, g1, b1 = 102, 126, 234  # #667eea
        r2, g2, b2 = 118, 75, 162   # #764ba2
        ratio = y / size
        r = int(r1 + (r2 - r1) * ratio)
        g = int(g1 + (g2 - g1) * ratio)
        b = int(b1 + (b2 - b1) * ratio)
        draw.rectangle([(0, y), (size, y+1)], fill=(r, g, b))

    # Draw rounded rectangle effect
    draw.rounded_rectangle([(0, 0), (size-1, size-1)], radius=max(3, size//8), outline='#764ba2', width=2)

    # Add 'Y' text
    try:
        # Try to use a nice font
        font_size = int(size * 0.6)
        font = ImageFont.truetype("arial.ttf", font_size)
    except:
        # Fallback to default font
        font = ImageFont.load_default()

    text = "Y"
    # Get text bounding box
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Center the text
    x = (size - text_width) // 2 - bbox[0]
    y = (size - text_height) // 2 - bbox[1]

    draw.text((x, y), text, fill='white', font=font)

    # Add small dollar sign badge for larger icons
    if size >= 48:
        badge_size = size // 4
        badge_x = size - badge_size - 5
        badge_y = 5
        draw.ellipse(
            [(badge_x, badge_y), (badge_x + badge_size, badge_y + badge_size)],
            fill='#48bb78'
        )
        try:
            badge_font = ImageFont.truetype("arial.ttf", badge_size // 2)
        except:
            badge_font = ImageFont.load_default()

        dollar_bbox = draw.textbbox((0, 0), "$", font=badge_font)
        dollar_width = dollar_bbox[2] - dollar_bbox[0]
        dollar_height = dollar_bbox[3] - dollar_bbox[1]
        dollar_x = badge_x + (badge_size - dollar_width) // 2 - dollar_bbox[0]
        dollar_y = badge_y + (badge_size - dollar_height) // 2 - dollar_bbox[1]
        draw.text((dollar_x, dollar_y), "$", fill='white', font=badge_font)

    # Save the image
    img.save(filename, 'PNG')
    print(f"Created: {filename} ({size}x{size})")

if __name__ == "__main__":
    icons_dir = "icons"

    # Create icons directory if it doesn't exist
    os.makedirs(icons_dir, exist_ok=True)

    # Create the three required icons
    create_icon(16, os.path.join(icons_dir, "icon16.png"))
    create_icon(48, os.path.join(icons_dir, "icon48.png"))
    create_icon(128, os.path.join(icons_dir, "icon128.png"))

    print("\nAll icons created successfully!")
    print("You can now load the extension in Chrome.")
