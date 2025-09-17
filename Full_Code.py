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
from selenium.webdriver.common.keys import Keys
import subprocess
import threading

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
gemini_output = {}
product_rating_info = {"rating": "N/A", "total_ratings": "N/A"}
progress_var = None

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
    if m:
        return m.group(1)
    m2 = re.search(r"asin=([A-Z0-9]{10})", product_url)
    if m2:
        return m2.group(1)
    segments = product_url.split("/")
    for seg in reversed(segments):
        if re.match(r"^[A-Z0-9]{10}$", seg):
            return seg
    safe_print(f"[DEBUG] Could not extract ASIN from URL: {product_url}")
    return None

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

def analyze_reviews_with_gemini(progress_start=80, progress_end=100):
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

        for key in ["Overall Impression", "Summary of Positive Feedbacks", "Summary of Negative Feedbacks"]:
            gemini_output[key] = ""

        current_key = None
        for line in output.splitlines():
            l = line.strip()
            if not l:
                continue
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

        # Smooth progress during Gemini analysis
        for i in range(progress_start, progress_end+1):
            progress_var.set(i)
            root.update_idletasks()
            time.sleep(0.05)

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

from selenium.webdriver.common.keys import Keys  # add near your other selenium imports

def extract_feature_ratings_and_feedback(product_url, asin, target_features=None, wait_timeout=10):
    """
    Clicks feature-aspect chips (Customer Review Highlights) and extracts positive/negative counts.
    By default target_features=None -> it will gather all visible aspects. If you pass a list
    (e.g. ["Quality"]) it will only click & parse those.
    Returns dict: { "Quality": {"positive": "123", "negative": "45", "sentiment": "positive/negative/neutral"}, ... }
    """
    if target_features is not None:
        target_features = [t.lower() for t in target_features]
    else:
        target_features = None

    driver.get(product_url)
    time.sleep(2)

    # Wait for aspects to be present (if present at all)
    try:
        WebDriverWait(driver, wait_timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[data-hook='cr-insights-aspect-link']"))
        )
    except:
        safe_print("[Amazon] No review-insight aspect links found on page.")
        return {}

    # collect aspect elements
    aspects = driver.find_elements(By.CSS_SELECTOR, "a[data-hook='cr-insights-aspect-link']")
    safe_print(f"[Amazon] Found {len(aspects)} aspect chips on page (look for Quality etc).")

    feature_data = {}

    for a in aspects:
        # robustly get the visible label (some chips contain extra whitespace or svg)
        label = (a.text or a.get_attribute("aria-label") or "").strip()
        if not label:
            # try innerText fallback
            label = (a.get_attribute("innerText") or "").strip()

        if not label:
            continue

        safe_print(f"[DEBUG] Aspect label found: '{label}'")

        # only process requested targets if provided
        if target_features and label.lower() not in target_features:
            continue

        # scroll into view and click via JS to avoid overlay/obstruction
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a)
            driver.execute_script("arguments[0].click();", a)
        except Exception as e:
            safe_print(f"[WARN] Click via JS failed for '{label}': {e}. Trying normal click...")
            try:
                a.click()
            except Exception as e2:
                safe_print(f"[ERROR] Could not click aspect '{label}': {e2}")
                continue

        # the anchor should have aria-controls pointing to the modal/bottom-sheet id
        aria_id = a.get_attribute("aria-controls")
        modal_soup = None

        if aria_id:
            try:
                safe_print(f"[Amazon] Waiting for modal/area with id '{aria_id}' to appear...")
                modal_el = WebDriverWait(driver, wait_timeout).until(
                    EC.visibility_of_element_located((By.ID, aria_id))
                )
                # read innerHTML of the modal (more targeted than whole page)
                modal_html = modal_el.get_attribute("innerHTML")
                modal_soup = BeautifulSoup(modal_html, "html.parser")
            except Exception as e:
                safe_print(f"[WARN] Modal with id {aria_id} didn't appear or wasn't visible: {e}")
                # fallback: parse the current page
        else:
            safe_print(f"[WARN] Aspect '{label}' had no aria-controls; falling back to page HTML")

        if modal_soup is None:
            # fallback: parse page source (less reliable)
            modal_soup = BeautifulSoup(driver.page_source, "html.parser")

        # --- Robust extraction of positive/negative counts ---
        # Strategy: find nodes that mention 'positive'/'negative' (case-insensitive),
        # then look for the nearest number/percentage in the same block or adjacent spans.
        pos_node = modal_soup.find(string=re.compile(r"positive", re.I))
        neg_node = modal_soup.find(string=re.compile(r"negative", re.I))

        def find_number_near(node):
            if not node:
                return "N/A"
            # check parent block text
            parent = node.parent
            txt_candidates = []
            if parent:
                txt_candidates.append(parent.get_text(" ", strip=True))
            # previous/next siblings
            prev = parent.find_previous(string=True) if parent else None
            nxt = parent.find_next(string=True) if parent else None
            for t in (txt_candidates + ([prev] if prev else []) + ([nxt] if nxt else [])):
                if not t:
                    continue
                # look for numbers with commas or percentage like 1,234 or 56% or 78
                m = re.search(r"(\d{1,3}(?:[,\d]{0,})%?|\d+%?)", t)
                if m:
                    return m.group(1)
            # final fallback: scan the whole parent block for any digit tokens
            if parent:
                m2 = re.search(r"(\d[\d,]*%?)", parent.get_text(" ", strip=True))
                if m2:
                    return m2.group(1)
            return "N/A"

        pos_count = find_number_near(pos_node)
        neg_count = find_number_near(neg_node)

        # Also attempt to detect whether overall sentiment for the aspect is positive/negative/neutral
        sentiment = "neutral"
        # look for an element indicating a green check or orange minus in the modal HTML
        # (AboutAmazon mentions green check = mostly positive, orange minus = mostly negative)
        if re.search(r'check|tick|✔|green|#067D62', str(modal_soup), re.I):
            sentiment = "positive"
        elif re.search(r'minus|−|–|orange|negative|#f09300', str(modal_soup), re.I):
            sentiment = "negative"

        feature_data[label] = {
            "positive": pos_count,
            "negative": neg_count,
            "sentiment": sentiment
        }

        safe_print(f"[Amazon] {label}: +{pos_count} | -{neg_count} | sentiment: {sentiment}")

        # Try to close the modal to avoid stacking (press ESC)
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.4)
        except:
            # Last resort: click somewhere else
            driver.execute_script("window.scrollBy(0, -200);")

        # if user only asked for one feature (like Quality), break after it
        if target_features:
            # if we were passed a single feature, break after processing it
            if len(target_features) == 1:
                break

        # small pause between clicks
        time.sleep(0.5)

    return feature_data

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

    product_rating_info["rating"] = rating
    product_rating_info["total_ratings"] = total_reviews
    gemini_output["Product Overall Star Rating"] = f"Rating: {rating}/5\nTotal Ratings: {total_reviews}"

    safe_print(f"[Amazon] {product_name} | Rating: {rating}/5 | Total Ratings: {total_reviews}")

    # --- NEW FEATURE-WISE EXTRACTION ---
    feature_data = extract_feature_ratings_and_feedback(product_url, asin)
    feature_text = ""
    if feature_data:
        safe_print("\n=== Feature-wise Ratings & Feedback ===")
        for feature, data in feature_data.items():
            line = f"{feature}: {data['positive']} positive | {data['negative']} negative"
            safe_print(line)
            feature_text += line + "\n"
        gemini_output["Feature Ratings"] = feature_text  # <-- add this
    else:
        safe_print("[Amazon] No feature-wise data found.")
        gemini_output["Feature Ratings"] = ""


    # --- Continue with review scraping ---
    collected = []
    reviews_page_url = f"https://www.amazon.in/product-reviews/{asin}/?pageNumber=1&reviewerType=all_reviews"
    driver.get(reviews_page_url)
    time.sleep(3)

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
        next_li = soup.find("li", {"class": "a-last"})
        if next_li and next_li.find("a"):
            driver.find_element(By.CSS_SELECTOR, "li.a-last a").click()
            time.sleep(3)
            page_count += 1
        else:
            break

    if collected:
        pd.DataFrame(collected).to_csv(CSV_FILE, index=False, encoding="utf-8-sig")
        safe_print(f"[Amazon] Scraped {len(collected)} reviews saved to {CSV_FILE}")
    else:
        safe_print("[Amazon] No reviews found.")
    return collected

# -------------------- Flipkart Functions ---------------------
def scrape_flipkart_category_ratings(driver, product_url):
    """Extract Flipkart category ratings + positive/negative feedback like Sound Quality, Bass, etc."""
    safe_print(f"[Flipkart] Opening product page for feature ratings: {product_url}")
    driver.get(product_url)
    time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    anchors = soup.find_all("a", href=True)

    category_links = {}
    for a in anchors:
        href = a.get("href")
        if "/product-reviews/" not in href:
            continue

        # Collect category names (skip pagination links)
        div_label = a.find("div", class_="NTiEl0")
        if div_label:
            label = div_label.get_text(strip=True)
            if label.lower() == "overall":   # SKIP "Overall"
                continue
            if not re.match(r"^(?:[0-9]+|Next)$", label, re.I):
                category_links[label] = "https://www.flipkart.com" + href
            continue

        span_label = a.find("span", class_=lambda x: x and "AgRA+X" in x)
        if span_label:
            label = span_label.get_text(strip=True)
            if label.lower() == "overall":   # SKIP "Overall"
                continue
            if not re.match(r"^(?:[0-9]+|Next)$", label, re.I):
                category_links[label] = "https://www.flipkart.com" + href

    safe_print(f"[Flipkart] Found {len(category_links)} feature/category links.")

    results = {}
    for idx, (name, link) in enumerate(category_links.items(), start=1):
        safe_print(f"[Flipkart] ({idx}/{len(category_links)}) Extracting {name}")
        driver.get(link)
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "text._2DdnFS"))
            )
            soup = BeautifulSoup(driver.page_source, "html.parser")
            rating_tag = soup.find("text", class_="_2DdnFS")
            rating_val = rating_tag.get_text(strip=True) if rating_tag else "N/A"

            feedback_div = soup.find("div", class_="SmC0g8")
            if feedback_div:
                positive = feedback_div.find("span", class_="WtBCuZ")
                negative = feedback_div.find("span", class_="_9VjbDx")
                positive_val = positive.get_text(strip=True) if positive else "N/A"
                negative_val = negative.get_text(strip=True) if negative else "N/A"
            else:
                positive_val = negative_val = "N/A"

            results[name] = {"rating": rating_val, "positive": positive_val, "negative": negative_val}
            safe_print(f"[Flipkart] {name}: {rating_val} | {positive_val} positive | {negative_val} negative")
        except Exception as e:
            safe_print(f"[Flipkart] Failed to extract {name}: {e}")
            results[name] = {"rating": "N/A", "positive": "N/A", "negative": "N/A"}

    return results


# -------------------- Flipkart Functions ---------------------
def scrape_flipkart_reviews(driver, product_name):
    collected = []
    try:
        try:
            view_all = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, '//*[@id="container"]//span[contains(text(),"All") and contains(text(),"reviews")]'))
            )
            view_all.click()
            time.sleep(2)
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

        product_rating_info["rating"] = rating
        product_rating_info["total_ratings"] = total_reviews
        gemini_output["Product Overall Star Rating"] = f"Rating: {rating}/5\nTotal Ratings & Reviews: {total_reviews}"

        safe_print(f"[Flipkart] {product_name} | Rating: {rating}/5 | Total Ratings: {total_reviews}")

        # --- Flipkart Feature/Category Ratings ---
        feature_data = scrape_flipkart_category_ratings(driver, driver.current_url)
        feature_text = ""
        if feature_data:
            safe_print("\n=== Flipkart Feature Ratings ===")
            for feature, data in feature_data.items():
                line = f"{feature}: {data['rating']} | {data['positive']} positive | {data['negative']} negative"
                safe_print(line)
                feature_text += line + "\n"
        gemini_output["Feature Ratings"] = feature_text
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

            progress_page = 50 * page_count / MAX_PAGES
            progress_var.set(progress_page)
            root.update_idletasks()
            time.sleep(0.1)

            try:
                next_btn = driver.find_element(By.XPATH, "//span[text()='Next']")
                driver.execute_script("arguments[0].click();", next_btn)
                time.sleep(1)
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
    global chat_input_entry, chat_send_btn

    if option == "Chat with Gemini":
        chat_input_entry.grid(row=10, column=0, padx=10, pady=5, sticky="w")
        chat_send_btn.grid(row=11, column=0, padx=10, pady=5, sticky="w")
        output_box.config(state="normal")
        output_box.delete(1.0, tk.END)
        output_box.insert(tk.END, "[Gemini Chat]\nYou can ask questions about the product reviews.\n")
        output_box.config(state="disabled")
    else:
        chat_input_entry.grid_forget()
        chat_send_btn.grid_forget()
        if gemini_output:
            output_box.config(state="normal")
            output_box.delete(1.0, tk.END)

            # Combine Gemini Overall Impression + Feature-wise Ratings
            if option == "Overall Impression":
                text = gemini_output.get("Overall Impression", "")
                # Append feature-wise ratings if available
                if "Feature Ratings" in gemini_output:
                    text += "\n" + gemini_output["Feature Ratings"]
            else:
                text = gemini_output.get(option, "No data")

            output_box.insert(tk.END, text.strip())
            output_box.config(state="disabled")


# -------------------- Chat Function ---------------------
def send_chat_question():
    question = chat_input_var.get().strip()
    if not question:
        return

    output_box.config(state="normal")
    output_box.insert(tk.END, f"\nYou: {question}\n")
    output_box.config(state="disabled")
    output_box.see(tk.END)
    chat_input_var.set("")

    summary_text = "\n".join([
        gemini_output.get("Overall Impression", ""),
        gemini_output.get("Summary of Positive Feedbacks", ""),
        gemini_output.get("Summary of Negative Feedbacks", "")
    ])

    prompt = f"""
    Product Review Summary:
    {summary_text}

    User Question:
    {question}

    Answer based on the above summary.
    """

    response = call_gemini(prompt)
    output_box.config(state="normal")
    output_box.insert(tk.END, f"Gemini: {response}\n")
    output_box.config(state="disabled")
    output_box.see(tk.END)

# -------------------- Submit Thread ---------------------
def run_scraper_thread():
    threading.Thread(target=submit_scraper, daemon=True).start()

def submit_scraper():
    global driver
    user_product = product_entry.get().strip()
    platform = platform_var.get()

    if not user_product:
        messagebox.showerror("Input Error", "Please enter a product name")
        return

    output_box.config(state="normal")
    output_box.delete(1.0, tk.END)
    output_box.config(state="disabled")

    progress_var.set(0)
    safe_print(f"[INFO] Searching for '{user_product}' on {platform}...")

    options = uc.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    )

    driver = uc.Chrome(version_main=139, options=options)
    collected_reviews = []

    try:
        if platform == "Amazon":
            driver.get("https://www.amazon.in/")
            continue_button = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "/html/body/div/div[1]/div[3]/div/div/form/div/div/span/span/button"))
            )
            continue_button.click()
            time.sleep(3)
            search_box = driver.find_element(By.ID, "twotabsearchtextbox")
            search_box.send_keys(user_product)
            search_box.submit()
            time.sleep(5)

            soup = BeautifulSoup(driver.page_source, "html.parser")
            products = soup.find_all("div", {"data-component-type": "s-search-result"})

            matched_url = None
            matched_title = None
            for product in products:
                title_tag = product.find("h2")
                if title_tag:
                    title_span = title_tag.find("span")
                    if title_span:
                        product_title = title_span.get_text(strip=True)
                        if clean_text(user_product) in clean_text(product_title):
                            matched_title = product_title
                            link_tag = product.find("a", href=re.compile(r"/(?:dp|gp/product)/"))
                            if link_tag and "href" in link_tag.attrs:
                                matched_url = "https://www.amazon.in" + link_tag["href"].split("?")[0]
                                break
            if matched_url:
                safe_print(f"[Amazon] Product matched: {matched_title}")
                collected_reviews = scrape_amazon_reviews(matched_url, matched_title)
            else:
                safe_print("[Amazon] No product matched the input.")
        else:
            driver.get(f"https://www.flipkart.com/search?q={user_product}")
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

    if collected_reviews:
        analyze_reviews_with_gemini()
    else:
        safe_print("[INFO] Skipping Gemini analysis since no reviews were found.")

# ----------------- Tkinter UI -----------------
root = tk.Tk()
root.title("Product Review Analyzer")
root.geometry("700x700")
root.minsize(700, 700)

container = tk.Frame(root)
container.place(relx=0.5, rely=0.5, anchor="center")

# Product Name Row
tk.Label(container, text="Product Name:").grid(row=0, column=0, sticky="w", padx=10, pady=5)
product_entry = tk.Entry(container, width=50)
product_entry.grid(row=0, column=1, padx=10, pady=5)

# Platform Row
tk.Label(container, text="E-Commerce Platform:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
platform_var = tk.StringVar()
platform_dropdown = ttk.Combobox(container, textvariable=platform_var, width=40, state="readonly")
platform_dropdown["values"] = ("Amazon", "Flipkart")
platform_dropdown.current(0)
platform_dropdown.grid(row=1, column=1, padx=10, pady=5)

# Select Option Row
tk.Label(container, text="Select Option:").grid(row=2, column=0, sticky="w", padx=10, pady=5)
option_var = tk.StringVar()
option_dropdown = ttk.Combobox(container, textvariable=option_var, width=40, state="readonly")
option_dropdown["values"] = (
    "Product Overall Star Rating",
    "Overall Impression",
    "Summary of Positive Feedbacks",
    "Summary of Negative Feedbacks",
    "Chat with Gemini"
)
option_dropdown.current(0)
option_dropdown.grid(row=2, column=1, padx=10, pady=5)
option_var.trace("w", update_result_box)

# Submit Button Row
submit_btn = tk.Button(container, text="Submit", command=run_scraper_thread, bg="blue", fg="white", width=15)
submit_btn.grid(row=3, column=0, columnspan=2, pady=10)

# Progress Bar Row
progress_var = tk.DoubleVar()
progress_bar = ttk.Progressbar(container, variable=progress_var, maximum=100, length=500)
progress_bar.grid(row=4, column=0, columnspan=2, pady=5)

# Result Box Row
tk.Label(container, text="Analysis Result / Logs:").grid(row=5, column=0, sticky="w", padx=10, pady=5)
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
output_box.grid(row=6, column=0, columnspan=2, padx=10, pady=5)

# Chat input (initially hidden)
chat_input_var = tk.StringVar()
chat_input_entry = tk.Entry(container, textvariable=chat_input_var, width=50)
chat_send_btn = tk.Button(container, text="Send", bg="green", fg="white", command=send_chat_question)


root.mainloop()
