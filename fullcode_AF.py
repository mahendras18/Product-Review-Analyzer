import re
import time
import json
import warnings
import pandas as pd
from bs4 import BeautifulSoup
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import subprocess

warnings.filterwarnings("ignore")

# -------------------- Load Config ---------------------
with open("config.json", "r") as f:
    CONFIG = json.load(f)

AMAZON_EMAIL = CONFIG.get("AMAZON_EMAIL", "")
AMAZON_PASSWORD = CONFIG.get("AMAZON_PASSWORD", "")
CSV_FILE = CONFIG["csv_file"]
MAX_PAGES = CONFIG["max_pages"]
REVIEW_COLUMN_NAME = CONFIG["review_column_name"]
GEMINI_PATH = CONFIG.get("gemini_path")

driver = None
output_box = None
gemini_output = {}  # Store Gemini analysis
product_rating_info = {"rating": "N/A", "total_ratings": "N/A"}  # Store rating info for dropdown


# -------------------- Helper Functions ---------------------
def safe_text(tag):
    return tag.text.strip() if tag else ""


def clean_text(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", "", text).lower().strip()


def safe_print(*args, **kwargs):
    text = " ".join([str(a) for a in args])
    print(text, **kwargs)
    global output_box
    if output_box:
        output_box.config(state="normal")
        output_box.insert(tk.END, text + "\n")
        output_box.see(tk.END)
        output_box.config(state="disabled")


def extract_asin(product_url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", product_url)
    return m.group(1) if m else None


# -------------------- Gemini CLI ---------------------
def call_gemini(prompt: str) -> str:
    try:
        result = subprocess.run(
            [GEMINI_PATH],
            input=prompt,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"Error: {result.stderr.strip()}"
    except Exception as e:
        return f"CLI call failed: {e}"


def analyze_reviews_with_gemini():
    global gemini_output
    try:
        df = pd.read_csv(CSV_FILE, encoding="utf-8-sig")
        reviews = df[REVIEW_COLUMN_NAME].dropna().tolist()
        safe_print(f"[INFO] Collected {len(reviews)} reviews from CSV for analysis...")

        prompt = f"""
        Summarize the following product reviews. 
        Format: 
        - Product Overall Star Rating 
        - Overall Impression 
        - Summary of Positive Feedbacks 
        - Summary of Negative Feedbacks 

        Reviews: {' '.join(reviews)} 
        """

        output = call_gemini(prompt)
        safe_print("\n[Gemini CLI Output]:\n" + output)

        # Initialize dropdown storage
        for key in ["Overall Impression", "Summary of Positive Feedbacks", "Summary of Negative Feedbacks"]:
            gemini_output[key] = ""

        current_key = None
        for line in output.splitlines():
            l = line.strip()
            if not l:
                continue

            # Detect section headers
            if "overall impression" in l.lower():
                current_key = "Overall Impression"
                after_colon = re.sub(r"\*+", "", l).split(":", 1)
                if len(after_colon) > 1 and after_colon[1].strip():
                    gemini_output[current_key] += after_colon[1].strip() + "\n\n"
                continue
            elif "positive" in l.lower():
                current_key = "Summary of Positive Feedbacks"
                continue
            elif "negative" in l.lower():
                current_key = "Summary of Negative Feedbacks"
                continue

            if current_key:
                clean_line = re.sub(r"\*+", "", l).strip()
                if clean_line:
                    if current_key != "Overall Impression" and not clean_line.startswith("-"):
                        gemini_output[current_key] += f"- {clean_line}\n\n"
                    else:
                        gemini_output[current_key] += clean_line + "\n\n"

        update_result_box()
    except Exception as e:
        safe_print(f"[ERROR] Gemini analysis failed: {e}")


# -------------------- Amazon Functions ---------------------
def amazon_sign_in(driver):
    try:
        safe_print("[Amazon] Signing in...")
        email_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ap_email_login"))
        )
        email_field.clear()
        email_field.send_keys(AMAZON_EMAIL)
        driver.find_element(By.XPATH, '//input[@type="submit"]').click()
        time.sleep(2)

        password_field = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "ap_password"))
        )
        password_field.clear()
        password_field.send_keys(AMAZON_PASSWORD)
        driver.find_element(By.ID, "signInSubmit").click()
        time.sleep(3)

        safe_print("[Amazon] Sign-in completed.")
    except:
        safe_print("[Amazon] Sign-in not required or failed.")


def scrape_amazon_reviews(product_url, product_name):
    asin = extract_asin(product_url)
    if not asin:
        safe_print("[Amazon] Could not extract ASIN.")
        return []

    driver.get(product_url)
    time.sleep(3)
    soup = BeautifulSoup(driver.page_source, "html.parser")

    rating_span = soup.find("span", {"class": "a-icon-alt"})
    rating = rating_span.get_text(strip=True).split()[0] if rating_span else "N/A"

    reviews_span = soup.find("span", {"id": "acrCustomerReviewText"})
    total_reviews = reviews_span.get_text(strip=True) if reviews_span else "N/A"

    # Store for dropdown display
    product_rating_info["rating"] = rating
    product_rating_info["total_ratings"] = total_reviews
    gemini_output["Product Overall Star Rating"] = f"Rating: {rating}/5\nTotal Ratings: {total_reviews}"

    safe_print(f"[Amazon] {product_name} | Rating: {rating}/5 | Total Ratings: {total_reviews}")

    collected = []
    reviews_page_url = f"https://www.amazon.in/product-reviews/{asin}/?pageNumber=1&reviewerType=all_reviews"
    driver.get(reviews_page_url)
    time.sleep(3)

    # Trigger sign-in if required
    soup = BeautifulSoup(driver.page_source, "html.parser")
    if soup.find(id="ap_email_login"):
        amazon_sign_in(driver)

    page_count = 1
    while page_count <= MAX_PAGES:
        safe_print(f"[Amazon] Page {page_count}")
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)

        soup = BeautifulSoup(driver.page_source, "html.parser")
        review_blocks = soup.find_all(attrs={"data-hook": "review"})
        if not review_blocks:
            break

        safe_print(f"Found {len(review_blocks)} reviews on this page")
        for review in review_blocks:
            collected.append({
                "product_name": product_name,
                "overall_rating": rating,
                "total_ratings": total_reviews,
                "reviewer_name": safe_text(review.find("span", {"class": "a-profile-name"})) or "Anonymous",
                "star_rating": safe_text(review.find("span", {"class": "a-icon-alt"})) or "N/A",
                "review_date": safe_text(review.find("span", {"data-hook": "review-date"})) or "N/A",
                "review_body": safe_text(review.find("span", {"data-hook": "review-body"})) or "No Content"
            })

        update_progress(page_count, MAX_PAGES)

        next_li = soup.find("li", {"class": "a-last"})
        if next_li and next_li.find("a"):
            try:
                driver.find_element(By.CSS_SELECTOR, "li.a-last a").click()
                time.sleep(2)
                page_count += 1
            except:
                break
        else:
            break

    if collected:
        pd.DataFrame(collected).to_csv(CSV_FILE, index=False, encoding="utf-8-sig")
        safe_print(f"[Amazon] Scraped {len(collected)} reviews saved to {CSV_FILE}")
    else:
        safe_print("[Amazon] No reviews found.")

    return collected


# -------------------- Flipkart Functions ---------------------
def scrape_flipkart_reviews(driver, product_name):
    collected = []
    try:
        try:
            view_all = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="container"]//span[contains(text(),"All") and contains(text(),"reviews")]'))
            )
            view_all.click()
            time.sleep(3)
            safe_print("[Flipkart] Clicked 'All reviews' button.")
        except:
            safe_print("[Flipkart] No 'All reviews' button found.")

        soup = BeautifulSoup(driver.page_source, "html.parser")
        rating_tag = soup.find("div", class_="ipqd2A")
        total_tag = None
        for s in soup.find_all("span"):
            if "Ratings" in s.text:
                total_tag = s
                break

        rating = safe_text(rating_tag) or "N/A"
        total_reviews = safe_text(total_tag) or "N/A"

        # Store for dropdown display
        product_rating_info["rating"] = rating
        product_rating_info["total_ratings"] = total_reviews
        gemini_output["Product Overall Star Rating"] = f"Rating: {rating}/5\nTotal Ratings & Reviews: {total_reviews}"

        safe_print(f"[Flipkart] {product_name} | Rating: {rating}/5 | Total Ratings: {total_reviews}")

        page_count = 1
        while page_count <= MAX_PAGES:
            safe_print(f"[Flipkart] Page {page_count}")
            soup = BeautifulSoup(driver.page_source, "html.parser")
            review_blocks = soup.find_all("div", class_="EKFha-")
            if not review_blocks:
                break

            for review in review_blocks:
                star_rating_tag = review.find("div", class_=re.compile(r"^XQDdHH"))
                star_rating = safe_text(star_rating_tag) or "N/A"

                reviewer_name_tag = review.find("p", class_="_2NsDsF AwS1CA")
                reviewer_name = safe_text(reviewer_name_tag) or "Anonymous"

                date_tag = None
                user_info_container = review.find('div', class_='gHqwa8')
                if user_info_container:
                    date_tags = user_info_container.find_all("p", class_="_2NsDsF")
                    if date_tags:
                        date_tag = date_tags[-1]

                review_date = safe_text(date_tag) or "N/A"

                review_text_tag = review.find("div", {"class": re.compile(r"^ZmyHeo")})
                review_text = review_text_tag.get_text(separator=" ", strip=True).replace("READ MORE", "").strip() if review_text_tag else "N/A"

                collected.append({
                    "product_name": product_name,
                    "overall_rating": rating,
                    "total_ratings": total_reviews,
                    "reviewer_name": reviewer_name,
                    "star_rating": star_rating,
                    "review_date": review_date,
                    "review_body": review_text
                })

            update_progress(page_count, MAX_PAGES)

            try:
                next_btn = driver.find_element(By.XPATH, "//span[text()='Next']")
                driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(2)
                page_count += 1
            except:
                break
    except:
        pass

    if collected:
        pd.DataFrame(collected).to_csv(CSV_FILE, index=False, encoding="utf-8-sig")
        safe_print(f"[Flipkart] Scraped {len(collected)} reviews saved to {CSV_FILE}")
    else:
        safe_print("[Flipkart] No reviews found.")

    return collected


# -------------------- Result Box Update ---------------------
def update_result_box(*args):
    option = option_var.get()
    if not gemini_output:
        return

    output_box.config(state="normal")
    output_box.delete(1.0, tk.END)
    text = gemini_output.get(option, "No data")
    output_box.insert(tk.END, text.strip())
    output_box.config(state="disabled")


# -------------------- Progress Update ---------------------
progress_var = None
progress_label = None
def update_progress(page, total_pages):
    global progress_var, progress_label
    percent = int((page / total_pages) * 100)
    progress_var.set(percent)
    progress_label.config(text=f"Progress: {percent}%")
    root.update_idletasks()


# -------------------- Submit ---------------------
def submit():
    global driver
    user_product = product_entry.get().strip()
    platform = platform_var.get()

    if not user_product:
        messagebox.showerror("Input Error", "Please enter a product name")
        return

    # Clear previous results before new run
    output_box.config(state="normal")
    output_box.delete(1.0, tk.END)
    output_box.config(state="disabled")

    progress_var.set(0)
    progress_label.config(text="Progress: 0%")

    safe_print(f"[INFO] Searching for '{user_product}' on {platform}...")

    query = user_product.replace(" ", "+")
    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    driver = uc.Chrome(version_main=139, options=options)

    collected_reviews = []

    try:
        if platform == "Amazon":
            # --- Amazon search ---
            driver.get(f"https://www.amazon.in/s?k={query}")
            time.sleep(3)
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            products = soup.find_all('div', {'data-component-type': 's-search-result'})
            matched_url, matched_title = None, None

            for product in products:
                title_tag = product.find("h2")
                if title_tag:
                    title_span = title_tag.find("span")
                    if title_span:
                        product_title = title_span.get_text(strip=True)
                        if clean_text(user_product) in clean_text(product_title):
                            matched_title = product_title
                            link_tag = product.find("a", {"class": "a-link-normal"})
                            if link_tag and "href" in link_tag.attrs:
                                matched_url = "https://www.amazon.in" + link_tag["href"].split("?")[0]
                            break

            if matched_url:
                safe_print(f"[Amazon] Product matched: {matched_title}")
                collected_reviews = scrape_amazon_reviews(matched_url, matched_title)
            else:
                safe_print("[Amazon] No product matched the input.")

        else:
            # --- Flipkart search ---
            driver.get(f"https://www.flipkart.com/search?q={query}")
            time.sleep(3)

            try:
                close_btn = driver.find_element(By.XPATH, "//button[contains(text(),'✕')]")
                close_btn.click()
                time.sleep(1)
            except:
                pass

            product_elements = driver.find_elements(By.CSS_SELECTOR, "a.wjcEIp")
            matched_title, matched_href = None, None

            for elem in product_elements:
                title = elem.get_attribute("title") or ""
                if title and clean_text(user_product) in clean_text(title):
                    matched_title = title
                    matched_href = elem.get_attribute("href")
                    break

            if matched_title and matched_href:
                safe_print(f"[Flipkart] Product matched: {matched_title}")
                driver.get(matched_href)
                time.sleep(2)
                collected_reviews = scrape_flipkart_reviews(driver, matched_title)
            else:
                safe_print("[Flipkart] No product matched the input.")

    finally:
        if driver:
            driver.quit()
            safe_print("[INFO] Browser closed.")

    # Run Gemini only if reviews exist
    if collected_reviews:
        analyze_reviews_with_gemini()
    else:
        safe_print("[INFO] Skipping Gemini analysis since no reviews were found.")


# ----------------- Tkinter UI -----------------
root = tk.Tk()
root.title("Product Review Analyzer")
root.geometry("700x600")
root.minsize(700, 600)

# Create a container frame in the center
container = tk.Frame(root)
container.place(relx=0.5, rely=0.5, anchor="center")

# ---------------- Widgets inside container ----------------
tk.Label(container, text="Product Name:").grid(row=0, column=0, sticky="w", padx=10, pady=5)
product_entry = tk.Entry(container, width=60)
product_entry.grid(row=1, column=0, padx=10, pady=5)

tk.Label(container, text="E-Commerce Platform:").grid(row=2, column=0, sticky="w", padx=10, pady=5)
platform_var = tk.StringVar()
platform_dropdown = ttk.Combobox(container, textvariable=platform_var, width=40, state="readonly")
platform_dropdown["values"] = ("Amazon", "Flipkart")
platform_dropdown.current(0)
platform_dropdown.grid(row=3, column=0, padx=10, pady=5)

tk.Label(container, text="Select Option:").grid(row=4, column=0, sticky="w", padx=10, pady=5)
option_var = tk.StringVar()
option_dropdown = ttk.Combobox(container, textvariable=option_var, width=40, state="readonly")
option_dropdown["values"] = (
    "Product Overall Star Rating",
    "Overall Impression",
    "Summary of Positive Feedbacks",
    "Summary of Negative Feedbacks",
)
option_dropdown.current(0)
option_dropdown.grid(row=5, column=0, padx=10, pady=5)
option_var.trace("w", update_result_box)

submit_btn = tk.Button(container, text="Submit", command=submit, bg="blue", fg="white", width=15)
submit_btn.grid(row=6, column=0, pady=5)

# ---------------- Progress Bar ----------------
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(container, variable=progress_var, maximum=100, length=400)
progress_bar.grid(row=7, column=0, pady=5)

progress_label = tk.Label(container, text="Progress: 0%", fg="green", font=("Arial", 10, "bold"))
progress_label.grid(row=8, column=0, pady=5)

tk.Label(container, text="Analysis Result / Logs:").grid(row=9, column=0, sticky="w", padx=10, pady=5)
output_box = scrolledtext.ScrolledText(
    container,
    wrap=tk.WORD,
    width=80,
    height=20,
    state="disabled",
    foreground="#111111",
    background="#f0f0f0",
    font=("Arial", 10)
)
output_box.grid(row=10, column=0, padx=10, pady=5)

# ---------------- Window close handling ----------------
root.protocol("WM_DELETE_WINDOW", lambda: (driver.quit() if driver else None, root.destroy()))
root.mainloop()
