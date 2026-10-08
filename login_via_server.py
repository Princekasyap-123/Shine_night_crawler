from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, proxy={"server": "socks5://127.0.0.1:1080"})
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://recruiter.shine.com/recruiter/")
    input("Browser mein Shine par login karo, phir yahan Enter dabao... ")
    context.storage_state(path="shine_storage_state.json")
    browser.close()