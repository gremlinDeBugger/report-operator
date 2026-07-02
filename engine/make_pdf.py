"""Render the HTML dashboard to a polished PDF using a headless browser."""
import sys
from playwright.sync_api import sync_playwright

def html_to_pdf(html_path: str, pdf_path: str):
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page()
        page.goto(f"file://{html_path}", wait_until="load")
        page.pdf(path=pdf_path, format="A4", print_background=True,
                 margin={"top":"0","bottom":"0","left":"0","right":"0"})
        b.close()

if __name__ == "__main__":
    import os
    html = os.path.abspath(sys.argv[1] if len(sys.argv)>1 else "sample_output/report.html")
    pdf = sys.argv[2] if len(sys.argv)>2 else "sample_output/report.pdf"
    html_to_pdf(html, pdf)
    print(f"wrote {pdf}")
