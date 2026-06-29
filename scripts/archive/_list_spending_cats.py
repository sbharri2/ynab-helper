from bot import storage
for c in storage.list_categories_for_spending("ynab_helper.db"):
    print(f"  {c['group_name']:<30} | {c['name']}")
