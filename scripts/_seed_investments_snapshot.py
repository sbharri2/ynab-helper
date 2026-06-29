"""One-off: build an xlsx that mirrors the user's spreadsheet so we can
smoke-test bot/investments.py without waiting for them to export the
real file. Data is real (pulled via Drive MCP), formatting matches what
File → Download → .xlsx would produce.

Output: G:\\My Drive\\ynabclone\\investments\\snapshot-seed-2026-06-26.xlsx
"""
from __future__ import annotations
from pathlib import Path
import openpyxl

OUT = Path(r"G:\My Drive\ynabclone\investments\snapshot-seed-2026-06-26.xlsx")
OUT.parent.mkdir(parents=True, exist_ok=True)

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Savings"

# ── Holdings table ─────────────────────────────────────────────────────────
HOLDINGS_HEADER = [
    "Account", "Account Type", "Account #", "Primary Owner",
    "End of 2021 Value (12-2-2021)",
    "2022 Value (7/1/22)",
    "End 2022 Value (12-2-2021)",
    "2023 Value (6/12/23)",
    "2024 Value (3-3-24)",
    "2024 Value (11-15-24)",
    "2026 Value (02-15-25)",
    "Password Stored", "Notes",
]

HOLDINGS = [
    ["Marcus Online Bank Account", "Savings", "300008130010", "Steven",
     47700, 32170, 30204.70, 27.03, 18694.41, 2397, 45614, "Steven's Keeper App", ""],
    ["Coastal Federal Credit Union", "Savings/Checkings", "361682", "Steven and Allison",
     22000, 16490, 22754.66, 16842, 27127.72, 26518, 35000, "Steven's Keeper App", ""],
    ["Treasury Direct - US Government", "Savings Bonds", "x-248-399-622", "Steven",
     10000, 20000, 21124, 0, 0, None, None, "Steven's Keeper App",
     "Bonds are 5 years."],
    ["Treasury Direct - US Government", "Savings Bonds", "w-687-774-030", "Allison",
     None, 10000, 10236, 0, 0, None, None, "Steven's Keeper App",
     "Bonds are 5 years."],
    ["Coinbase", "Crypto Exchange Account", "stevens email", "Steven",
     None, 254, 1984.78, 640, 8133.36, 90, 48537, "Steven's Keeper App", ""],
    ["Bitcoin", "Bitcoin Wallet", "", "No Owner - like loose cash",
     17500, 8312, 7342.33, 15400, 37129, 70000, 65115, "Steven's Keeper App",
     "Coins are stored on hard wallet."],
    ["Etherum", "Etherum Wallet", "", "No Owner - like loose cash",
     16316, 3767, 4529.53, 6953, 14637, 0, None, "Steven's Keeper App",
     "Coins are stored on hard wallet."],
    ["Vanguard - Steven", "Roth IRA Savings", "89851192", "Steven",
     42259, 35185, 40704.51, 46270, 53016, 68940, 63986.82, "Steven's Keeper App", ""],
    ["Vanguard - Luke", "529 College Savings", "223642379-01", "Luke",
     15493, 14560, 15873.56, 17280, 20101, 22709, 27751, "Steven's Keeper App", ""],
    ["Vanguard - Josie", "529 College Savings", "223642379-02", "Josie",
     10729, 10364, 11658.16, 13041, 15732, 18465, 23546, "Steven's Keeper App", ""],
    ["Fidelity", "401K Savings - Red Hat", "48253", "Allison",
     62105, 41116.35, 45519.54, 53258, 70104, 82815, 107442.06, "Steven's Keeper App", ""],
    ["Fidelity (Changed to Principal)", "401K Savings - Hanbury", "Fidelity - 58580", "Allison",
     225, 2799.06, 5649.39, None, None, None, None, "Steven's Keeper App", ""],
    ["Principal Financial Group - 401K", "401K Savings - Hanbury", "474866", "Allison",
     None, None, None, 10837, 16437.21, 13400, 16541.32, "",
     "note that we are not fully vested"],
    ["Guideline", "401K Savings - Germano", "", "Allison",
     0, 0, 0, 0, 1432.35, 1532, 1761.13, "t38f8gpR3E-u7M5", "ID is Allison's email"],
    ["Principal Financial Group - ESOP", "ESOP - Hanbury", "", "Allison",
     None, None, None, 2521, 3000, 3435, 0, "Steven's Keeper App", ""],
    ["American Funds - 310", "Simple IRA", "4000368617", "Allison",
     25824, 19744.60, 21067.49, 22935.37, 28759, 32858, 39635.56, "Steven's Keeper App",
     "don't confuse with american funds 401k account"],
    ["OBA Profit Sharing Fund", "Profit Sharing Savings", "N/A", "Steven",
     45000, 45000, 59800, 72735, 72735, 72735, 111000, "Steven's Keeper App", ""],
    ["117 Mayfield Dr", "Home Equity", "1040703074", "Steven and Allison",
     158827, 259234, 218100.47, 229699, 281760, 248420, 246501, "Steven's Keeper App",
     "Loan Depot is Mortgage Company"],
    ["105 7th Ave SE", "Home Equity", "1485942492", "Steven",
     37375, 37375, 39300.65, 40000, 42000, 44000, 42000, "Steven's Keeper App",
     "AmerisBank is Mortgage Company"],
    ["548 Walapai Dr", "Home Equity - 1/4 value of total home", "N/A", "Steven",
     97500, 121750, 109725, 99000, 0, None, None, "", ""],
    ["OBA Stock", "Company Stock", "N/A", "Steven",
     60000, 60000, 60000, 86712, 86712, 86712, 112000, "Firebox", ""],
    ["TD AmeriTrade", "Roth IRA Savings", "494131375", "Steven",
     2400, 1250, 1378.73, None, None, None, None, "Steven's Keeper App", ""],
    ["TD AmeriTrade", "Stock Account", "253695349", "Steven",
     None, 3932, 9731.05, None, None, None, None, "Steven's Keeper App", ""],
    ["Schwab (Transfered from TD)", "Roth IRA Savings", "4354-7636", "Steven",
     None, None, None, 1002, 934.61, 1130, 562, "Steven's Keeper App", ""],
    ["Schwab (Transfered from TD)", "Stock Account", "6997-4203", "Steven",
     None, None, None, 13067, 15225, 18679, 7926, "Steven's Keeper App", ""],
    ["Health Equity", "HSA Savings Account", "22777535", "Allison",
     3559, 5701, 4974.44, 4571, 3019, 1513, 0, "Steven's Keeper App", ""],
    ["Nationwide 401K", "401K Savings - OBA", "436-80582", "Steven",
     None, None, None, 9133, 28787.39, 48081, 62508.16, "Steven's Keeper App", ""],
    ["Treasury Direct - Short Term T-Bills", "Government Securities", "x-248-399-622", "Steven",
     None, None, None, 24000, 132000, 132000, 30000, "Steven's Keeper App", ""],
    ["American Funds", "401K Savings - Smith Sinnett", "IRK130681", "Allison",
     None, None, None, None, None, 24600, 56000, "Steven's Keeper App", ""],
]

ws.append(HOLDINGS_HEADER)
for r in HOLDINGS:
    ws.append(r)

# Blank separator
ws.append([])

# ── Totals section ─────────────────────────────────────────────────────────
ws.append(["Total", 674812, 749004.01, 741658.99, 785923.40, 977476.05, 1021029, 1143427.05])
ws.append(["Minus Home Equity", 515985, 489770.01, 523558.52, 556224.40, 695716.05, 772609, 896926.05])
ws.append(["Annual Change", None, None, "9.91%", "4.93%", "31.80%", "29.91%", "16.98%"])
ws.append(["Target Savings (3x salary at 40, 4x at 45, 5x at 50)",
           600000, 720000, 652500, 652500, 750000, 750000, 800000])
ws.append(["Delta", -84015, -230229.99, -128941.48, -96275.60, -54283.95, 22609, 96926.05])

ws.append([])
ws.append([])

# ── Insurance section ──────────────────────────────────────────────────────
ws.append([
    "Type of Insurance", "Thru Employer?", "Insurance Provider", "Sales Contact",
    "Coverage", "Deductable", "Annual Premium", "Comments",
    "Insurance Research Notes", "Renewal Date",
])

INSURANCE = [
    ["Life - Steven", False, "Mass Mutual", "Domenica",
     "Renewable Term Life 20 - $1,000,000", "", 600.36, "", "", ""],
    ["Life - Allison", False, "Mass Mutual", "Domenica",
     "Renewable Term Life 20 - $750,000", "", 454.20, "", "", ""],
    ["Life - Allison", True, "UNUM - SSA", "", "", "", None, "", "", ""],
    ["Life - Steven", False, "Mass Mutual", "Domenica",
     "Renewable Term Life 20 - $100,000", "", 214.08, "", "", ""],
    ["Life - Steven", False, "Mass Mutual", "Domenica",
     "Renewable Term Life 20 - $100,000", "", 151.44, "", "", ""],
    ["Life - Luke and Josie", True, "Guardian - OBA", "",
     "Check Google Drive", "", None, "Thru Steven's Insurance", "", ""],
    ["Life - Steven", True, "Guardian - OBA", "",
     "Check Google Drive", "", None, "", "", ""],
    ["LT Disability - Steven", False, "Mass Mutual", "Domenica",
     "Disability Income - $4700/mo", "", 1320.12, "", "", ""],
    ["LT Disability - Allison", False, "Mass Mutual", "Domenica",
     "Disability Income - $1245/mo", "", 958.56, "", "", ""],
    ["LT Disability - Steven", True, "Guardian - OBA", "",
     "Check Google Drive", "", None, "", "", ""],
    ["LT Disability - Allison", True, "UNUM - SSA", "", "", "", None, "", "", ""],
    ["Health - Steven", True, "Cigna - OBA", "",
     "Check Google Drive", "", 0, "", "", ""],
    ["Health - Allison", True, "UHC - SSA", "",
     "Check Google Drive", "", 12504, "part of Family premium", "", ""],
    ["Health - Kiddos", True, "UHC - SSA", "",
     "Check Google Drive", "", None, "part of Family premium", "", ""],
    ["Health - Family HSA", True, "HealthEquity - SSA", "", "", "", None, "", "", ""],
    ["Dental - Steven", True, "Guardian - OBA", "",
     "Check Google Drive", "", 0, "", "", ""],
    ["Dental - Allison", True, "Guardian - OBA", "",
     "Check Google Drive", "", 1632, "part of Family premium", "", ""],
    ["Dental - Kiddos", True, "Guardian - OBA", "",
     "Check Google Drive", "", None, "part of Family premium", "", ""],
    ["Vision - Steven", True, "Guardian - OBA", "",
     "Check Google Drive", "", 0, "", "", ""],
    ["Vision - Allison", True, "Guardian - OBA", "",
     "Check Google Drive", "", 312, "part of Family premium", "", ""],
    ["Vision - Kiddos", True, "Guardian - OBA", "",
     "Check Google Drive", "", None, "part of Family premium", "", ""],
    ["Accident - Family", True, "Allstate - OBA", "",
     "Check Google Drive", "", None, "https://mybenefits.allstate.com/", "", ""],
    ["Critical Illness - Family", True, "Allstate - OBA", "",
     "Check Google Drive", "", 139.13, "https://mybenefits.allstate.com/", "", ""],
    ["Home Insurance - 117 Mayfield Dr", False, "Amica", "",
     "Coverage A - $299,000; Coverage B - $29,900; Coverage C - $224,250; Coverage D - $59,800; Liability $300,000",
     "$1,000", 2105, "", "", ""],
    ["Auto Insurance", False, "Amica", "",
     "$100K/$300K Bodily; $100K Property; uninsured motorists",
     "$500 Collision", 2077.16, "", "", ""],
    ["Umbrella Insurance", False, "Amica", "",
     "$1,000,000", "$500", 436, "$60 dividend; LOSS ASSESSMENT $50,000", "", ""],
    ["Rental Property Home Insurance", False, "Fortegra", "Ross Diversified",
     "Coverage A - $170,800", "$2,500", 977.32, "Surplus Lines Policy", "", ""],
    ["Rental Property Flood Insurance", False, "Neptune", "Franklin Flood",
     "Coverage: $170,000", "$10,000", 543.78, "", "", "May 12"],
]
for r in INSURANCE:
    ws.append(r)

wb.save(OUT)
print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
