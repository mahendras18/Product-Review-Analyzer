from datetime import datetime
from dateutil.relativedelta import relativedelta
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
import os
from PIL import Image, ImageTk

warnings.filterwarnings("ignore")

# -------------------- Load Config ---------------------
with open("config.json", "r") as f:
    CONFIG = json.load(f)

AMAZON_EMAIL = CONFIG.get("AMAZON_EMAIL", "")
AMAZON_PASSWORD = CONFIG.get("AMAZON_PASSWORD", "")
CSV_FILE = CONFIG["csv_file"]
MAX_PAGES = CONFIG["max_pages"]
REVIEW_COLUMN_NAME = CONFIG["review_column_name"]
COLUMNS_TO_ANALYZE = CONFIG.get("columns_to_analyze", [])
GEMINI_PATH = CONFIG.get("gemini_path")
HEADLESS = CONFIG.get("headless", False)

driver = None
output_box = None
gemini_output = {}
product_rating_info = {"rating": "N/A", "total_ratings": "N/A"}
progress_var = None
chat_progress_running = False
chat_progress_value = 70.0
chat_progress_direction = 1


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


def get_review_date_range():
    """Parse review_date column in CSV and return oldest & newest dates."""
    try:
        df = pd.read_csv(CSV_FILE, encoding="utf-8-sig")
        parsed_dates = []

        for d in df['review_date'].dropna():
            d = str(d).strip()

            # Amazon format
            if "Reviewed in" in d and "on" in d:
                try:
                    date_str = d.split("on")[-1].strip()
                    parsed_dates.append(pd.to_datetime(date_str, format="%d %B %Y"))
                except:
                    continue

            # Flipkart exact month/year
            elif re.match(r"^[A-Za-z]{3}, \d{4}$", d):
                try:
                    parsed_dates.append(pd.to_datetime(d, format="%b, %Y"))
                except:
                    continue

            # Flipkart relative date format
            elif "ago" in d:
                try:
                    num, unit, *_ = d.split()
                    num = int(num)
                    today = datetime.today()
                    if "month" in unit:
                        parsed_dates.append(today - relativedelta(months=num))
                    elif "year" in unit:
                        parsed_dates.append(today - relativedelta(years=num))
                except:
                    continue

        if parsed_dates:
            oldest = min(parsed_dates).strftime('%Y-%m-%d')
            newest = max(parsed_dates).strftime('%Y-%m-%d')
            return oldest, newest
        else:
            return None, None
    except Exception as e:
        safe_print(f"[ERROR] Failed to parse review dates: {e}")
        return None, None


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

        # Get all columns to analyze (including review_body)
        all_columns = COLUMNS_TO_ANALYZE + [REVIEW_COLUMN_NAME]
        safe_print(f"[INFO] Analyzing columns: {all_columns}")

        # Create formatted data for each review
        formatted_reviews = []
        for idx, row in df.iterrows():
            review_data = []
            for col in all_columns:
                if col in df.columns and pd.notna(row[col]):
                    review_data.append(f"{col}: {row[col]}")

            if review_data:  # Only add if there's data
                formatted_reviews.append(f"Review {idx + 1}:\n" + "\n".join(review_data))

        safe_print(f"[INFO] Collected {len(formatted_reviews)} reviews from CSV for analysis...")

        prompt = f"""
        Summarize the following product reviews with all their details. 
        Format: 
        - Product Overall Star Rating 
        - Overall Impression 
        - Summary of Positive Feedbacks 
        - Summary of Negative Feedbacks 

        Reviews Data:
        {' '.join(formatted_reviews)} 
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
                # Capture text after colon right away
                after_colon = re.sub(r"\*+", "", l).split(":", 1)
                if len(after_colon) > 1 and after_colon[1].strip():
                    gemini_output[current_key] += after_colon[1].strip() + "\n\n"
                continue

            elif "negative" in l.lower():
                current_key = "Summary of Negative Feedbacks"
                # Capture text after colon right away
                after_colon = re.sub(r"\*+", "", l).split(":", 1)
                if len(after_colon) > 1 and after_colon[1].strip():
                    gemini_output[current_key] += after_colon[1].strip() + "\n\n"
                continue

            if current_key:
                clean_line = re.sub(r"\*+", "", l).strip()
                if clean_line:
                    if current_key != "Overall Impression" and not clean_line.startswith("-"):
                        gemini_output[current_key] += f"- {clean_line}\n\n"
                    else:
                        gemini_output[current_key] += clean_line + "\n\n"

        # Smooth progress during Gemini analysis
        for i in range(progress_start, progress_end + 1):
            progress_var.set(i)
            root.update_idletasks()
            time.sleep(0.05)

        update_result_box()
    except Exception as e:
        safe_print(f"[ERROR] Gemini analysis failed: {e}")


# -------------------- Amazon Functions ---------------------
def is_on_amazon_signin_page(driver) -> bool:
    try:
        url = driver.current_url or ""
    except Exception:
        url = ""
    if "ap/signin" in url:
        return True
    html = driver.page_source or ""
    return any(token in html for token in ("ap_email", "ap_email_login", "ap_password"))


def amazon_sign_in(driver) -> bool:
    try:
        safe_print("[Amazon] Signing in...")
        # Find email field by any of the common IDs
        try:
            email_field = WebDriverWait(driver, 12).until(
                EC.presence_of_element_located((By.ID, "ap_email_login"))
            )
        except Exception:
            email_field = WebDriverWait(driver, 6).until(
                EC.presence_of_element_located((By.ID, "ap_email"))
            )

        email_field.clear()
        email_field.send_keys(AMAZON_EMAIL)

        # Click Continue/Next
        try:
            continue_btn = driver.find_element(By.ID, "continue")
            continue_btn.click()
        except Exception:
            try:
                driver.find_element(By.XPATH, "//input[@type='submit']").click()
            except Exception:
                pass

        # Wait for password field and submit
        password_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "ap_password"))
        )
        password_field.clear()
        password_field.send_keys(AMAZON_PASSWORD)

        try:
            driver.find_element(By.ID, "signInSubmit").click()
        except Exception:
            try:
                driver.find_element(By.XPATH, "//input[@type='submit']").click()
            except Exception:
                pass

        # Wait until we are no longer on the signin page
        try:
            WebDriverWait(driver, 20).until(lambda d: not is_on_amazon_signin_page(d))
        except Exception:
            pass

        if is_on_amazon_signin_page(driver):
            safe_print("[Amazon] Sign-in did not complete (still on login page).")
            return False

        safe_print("[Amazon] Sign-in completed.")
        return True
    except Exception as e:
        safe_print(f"[Amazon] Sign-in failed: {e}")
        return False


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

    # Get product details first
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
        gemini_output["Feature Ratings"] = feature_text
    else:
        safe_print("[Amazon] No feature-wise data found.")
        gemini_output["Feature Ratings"] = ""

    # --- Reviews scraping ---
    collected = []
    reviews_page_url = f"https://www.amazon.in/product-reviews/{asin}/?pageNumber=1&reviewerType=all_reviews"

    def navigate_to_reviews_with_stealth(url: str, max_attempts: int = 4) -> bool:
        for attempt in range(1, max_attempts + 1):
            safe_print(f"[Amazon] Opening reviews page (attempt {attempt}/{max_attempts})...")
            try:
                # Primary: normal navigation
                driver.get(url)
                time.sleep(2)
                # Success if review list or any review block appears
                try:
                    WebDriverWait(driver, 8).until(
                        EC.any_of(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-hook='review']")),
                            EC.presence_of_element_located((By.ID, "cm_cr-review_list")),
                            EC.presence_of_element_located(
                                (By.CSS_SELECTOR, "a[data-hook='cr-filter-info-review-rating-count']"))
                        )
                    )
                    if not is_on_amazon_signin_page(driver):
                        return True
                except Exception:
                    pass

                # Fallback 1: JS location change
                driver.execute_script("window.location.href = arguments[0];", url)
                time.sleep(2)
                if not is_on_amazon_signin_page(driver):
                    try:
                        WebDriverWait(driver, 6).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-hook='review']"))
                        )
                        return True
                    except Exception:
                        pass

                # Fallback 2: open in a new tab to bypass back-forward cache guards
                driver.execute_script("window.open(arguments[0], '_blank');", url)
                driver.switch_to.window(driver.window_handles[-1])
                time.sleep(2)
                if not is_on_amazon_signin_page(driver):
                    try:
                        WebDriverWait(driver, 6).until(
                            EC.presence_of_element_located((By.CSS_SELECTOR, "div[data-hook='review']"))
                        )
                        return True
                    except Exception:
                        pass

            except Exception as e:
                safe_print(f"[Amazon] Navigation error: {e}")

            # If bounced to login, try sign-in again if not headless or just wait for manual completion
            if is_on_amazon_signin_page(driver):
                safe_print("[Amazon] Detected login page during navigation. Re-attempting sign-in...")
                amazon_sign_in(driver)
                # brief wait before next loop
                time.sleep(2)
        return False

    # Try to reach reviews page using robust navigator
    if not navigate_to_reviews_with_stealth(reviews_page_url):
        # As a last resort, attempt once more after short pause
        time.sleep(3)
        navigate_to_reviews_with_stealth(reviews_page_url)

    # If login is required, sign in and then robustly reload the reviews page
    if is_on_amazon_signin_page(driver):
        signed_in = amazon_sign_in(driver)
        if not signed_in:
            safe_print("[Amazon] Sign-in may require OTP/captcha. Waiting for manual completion...")
            # Open UI if headless to allow manual completion
            try:
                if HEADLESS:
                    safe_print("[Amazon] Please set 'headless': false in config and rerun for manual login.")
            except Exception:
                pass
            # Give user time to complete any manual checks
            for i in range(30):
                if not is_on_amazon_signin_page(driver):
                    break
                time.sleep(2)
        safe_print("[Amazon] Reloading reviews page after login...")
        navigate_to_reviews_with_stealth(reviews_page_url)

    page_count = 1
    while page_count <= MAX_PAGES:
        safe_print(f"[Amazon] Page {page_count}")
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
        except Exception as e:
            safe_print(f"[WARN] Scrolling failed: {e}")

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

        # Go to next page if available
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
            if label.lower() == "overall":  # SKIP "Overall"
                continue
            if not re.match(r"^(?:[0-9]+|Next)$", label, re.I):
                category_links[label] = "https://www.flipkart.com" + href
            continue

        span_label = a.find("span", class_=lambda x: x and "AgRA+X" in x)
        if span_label:
            label = span_label.get_text(strip=True)
            if label.lower() == "overall":  # SKIP "Overall"
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
                EC.presence_of_element_located(
                    (By.XPATH, '//*[@id="container"]//span[contains(text(),"All") and contains(text(),"reviews")]'))
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
                review_text = review_text_tag.get_text(separator=" ", strip=True).replace("READ MORE",
                                                                                          "").strip() if review_text_tag else "N/A"

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


# --- Final update_result_box function ---
# This is slightly modified to manage the chat_frame visibility
def update_result_box(*args):
    """
    Updates the output box based on the selected option in the dropdown.
    Shows or hides the chat frame as needed.
    """
    option = option_var.get()

    # Hide both buttons initially
    save_btn.grid_remove()
    chat_send_btn.grid_remove()
    chat_input_entry.grid_remove()  # Hide input field initially
    chat_frame.grid_remove()

    if option == "Chat with Gemini":
        chat_frame.grid()
        chat_input_entry.grid()  # Show the input field
        chat_send_btn.grid()  # Show Send button
        save_btn.grid_remove()  # Hide Save button
        output_box.config(state="normal")
        output_box.delete(1.0, tk.END)
        output_box.insert(tk.END, "[Gemini CLI Chat]\nYou can ask questions about the product reviews.\n")
        output_box.config(state="disabled")
        return

    elif option == "Executive Summary":
        chat_frame.grid()
        chat_input_entry.grid_remove()  # Hide the input field
        chat_send_btn.grid_remove()  # Hide the send button
        save_btn.grid()  # Show only Save button

    # Prepare output
    output_box.config(state="normal")
    output_box.delete(1.0, tk.END)

    text = ""

    if gemini_output:
        if option == "Product Overall Star Rating":
            # Overall star rating + review date range
            text = gemini_output.get("Product Overall Star Rating", "")
            oldest, newest = get_review_date_range()
            if oldest and newest:
                text += (
                    f"\n\nDATE:"
                    f"\nOldest Review Date: {oldest}"
                    f"\nNewest Review Date: {newest}"
                )

        elif option == "Overall Impression":
            # Combine impression + feature ratings if present
            text = gemini_output.get("Overall Impression", "")
            feature_ratings = gemini_output.get("Feature Ratings")
            if feature_ratings:
                text += f"\n\n--- Feature Ratings ---\n{feature_ratings}"

        elif option == "Executive Summary":
            # --- Get product name ---
            product_name_text = ""
            try:
                df = pd.read_csv(CSV_FILE, encoding="utf-8-sig")
                if "product_name" in df.columns and not df["product_name"].empty:
                    product_name_text = df["product_name"].iloc[0]
            except Exception:
                product_name_text = "[Unknown Product]"

            # --- Build the summary text ---
            product_summary = ""

            # Product rating section
            if "Product Overall Star Rating" in gemini_output:
                product_summary += gemini_output["Product Overall Star Rating"].strip() + "\n\n"

            # Overall impression
            if "Overall Impression" in gemini_output and gemini_output["Overall Impression"].strip():
                product_summary += "Overall Impression:\n" + gemini_output["Overall Impression"].strip() + "\n\n"

            # Feature ratings
            if "Feature Ratings" in gemini_output and gemini_output["Feature Ratings"].strip():
                product_summary += "--- Feature Ratings ---\n" + gemini_output["Feature Ratings"].strip() + "\n\n"

            # Summary of positive feedbacks
            if "Summary of Positive Feedbacks" in gemini_output and gemini_output[
                "Summary of Positive Feedbacks"].strip():
                product_summary += "Summary of Positive Feedbacks:\n" + gemini_output[
                    "Summary of Positive Feedbacks"].strip() + "\n\n"

            # Summary of negative feedbacks
            if "Summary of Negative Feedbacks" in gemini_output and gemini_output[
                "Summary of Negative Feedbacks"].strip():
                product_summary += "Summary of Negative Feedbacks:\n" + gemini_output[
                    "Summary of Negative Feedbacks"].strip() + "\n\n"

            # --- Final formatted executive summary ---
            text = f"""Product Name: {product_name_text}

{product_summary}"""

        else:
            # Default case for other dropdown options
            text = gemini_output.get(option, "")

        # Fallback if text is empty
        if not text.strip():
            text = f"[INFO] No {option} found in analysis."
    else:
        text = "[INFO] No analysis available yet."

    # Update output box
    output_box.insert(tk.END, text.strip())
    output_box.config(state="disabled")
    output_box.see(tk.END)


# -------------------- Chat Function ---------------------
def _animate_chat_progress():
    global chat_progress_running, chat_progress_value, chat_progress_direction
    if not chat_progress_running:
        return
    try:
        # Oscillate between 70 and 95 while waiting
        chat_progress_value += chat_progress_direction * 0.6
        if chat_progress_value >= 95:
            chat_progress_value = 95
            chat_progress_direction = -1
        elif chat_progress_value <= 70:
            chat_progress_value = 70
            chat_progress_direction = 1
        progress_var.set(chat_progress_value)
        root.update_idletasks()
    except Exception:
        pass
    # Schedule next tick
    root.after(80, _animate_chat_progress)


def _run_chat_call(prompt: str):
    global chat_progress_running
    try:
        response = call_gemini(prompt)
    except Exception as e:
        response = f"[ERROR] Chat failed: {e}"
    # Stop animation and finalize progress
    chat_progress_running = False
    try:
        progress_var.set(100)
        root.update_idletasks()
    except Exception:
        pass

    # Append response in UI thread
    def _append():
        output_box.config(state="normal")
        output_box.insert(tk.END, f"Gemini: {response}\n")
        output_box.config(state="disabled")
        output_box.see(tk.END)

    root.after(0, _append)


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

    # Start progress animation
    global chat_progress_running, chat_progress_value, chat_progress_direction
    chat_progress_running = True
    chat_progress_value = 70.0
    chat_progress_direction = 1
    _animate_chat_progress()

    # Run Gemini call in background
    threading.Thread(target=_run_chat_call, args=(prompt,), daemon=True).start()


def save_executive_summary():
    try:
        # Get the content currently displayed
        content = output_box.get("1.0", tk.END).strip()
        if not content:
            messagebox.showinfo("No Content", "There is no summary to save.")
            return

        # Fixed filename
        filename = "executive_summary.txt"

        # Get current date and time
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Append to the file with timestamp
        with open(filename, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"Saved on: {timestamp}\n")
            f.write("=" * 80 + "\n\n")
            f.write(content + "\n\n")

        messagebox.showinfo("Saved", f"Executive summary appended successfully to:\n{filename}")
    except Exception as e:
        messagebox.showerror("Error", f"Failed to save summary:\n{e}")


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

    # Check network connectivity
    safe_print("[INFO] Checking network connectivity...")
    try:
        import urllib.request
        urllib.request.urlopen('https://www.google.com', timeout=5)
        safe_print("[INFO] Network connectivity confirmed.")
    except Exception as e:
        safe_print(f"[WARNING] Network connectivity issue detected: {e}")
        safe_print("[INFO] This may cause Chrome driver initialization to fail.")
        safe_print("[INFO] Please ensure you have a stable internet connection.")

    output_box.config(state="normal")
    output_box.delete(1.0, tk.END)
    output_box.config(state="disabled")

    progress_var.set(0)
    safe_print(f"[INFO] Searching for '{user_product}' on {platform}...")

    options = uc.ChromeOptions()
    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
    )

    # Use a persistent user data dir to keep Amazon session cookies
    try:
        profile_dir = CONFIG.get("chrome_user_data_dir")
        if profile_dir:
            options.add_argument(f"--user-data-dir={profile_dir}")
            options.add_argument("--profile-directory=Default")
    except Exception:
        pass

    try:
        safe_print("[INFO] Initializing Chrome driver...")
        driver = uc.Chrome(options=options)
        driver.set_page_load_timeout(600)  # was 300, increase to 10 minutes
        driver.set_script_timeout(300)  # add this for JS execution
        safe_print("[INFO] Chrome driver initialized successfully.")
    except Exception as e:
        safe_print(f"[ERROR] Failed to initialize undetected Chrome driver: {e}")
        safe_print("[INFO] Trying fallback with regular Selenium WebDriver...")

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.chrome.options import Options as ChromeOptions

            # Create regular Chrome options
            chrome_options = ChromeOptions()
            if HEADLESS:
                chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument(
                "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
            )

            # Try to use regular Chrome driver
            driver = webdriver.Chrome(options=chrome_options)
            driver.set_page_load_timeout(600)
            driver.set_script_timeout(300)
            safe_print("[INFO] Fallback Chrome driver initialized successfully.")

        except Exception as fallback_error:
            safe_print(f"[ERROR] Fallback Chrome driver also failed: {fallback_error}")
            safe_print("[INFO] This might be due to network connectivity issues.")
            safe_print("[INFO] Please check your internet connection and try again.")
            messagebox.showerror("Chrome Driver Error",
                                 f"Failed to initialize Chrome driver.\n\n"
                                 f"Primary Error: {e}\n"
                                 f"Fallback Error: {fallback_error}\n\n"
                                 f"This is usually due to:\n"
                                 f"1. No internet connection\n"
                                 f"2. Firewall blocking the connection\n"
                                 f"3. Chrome browser not installed\n"
                                 f"4. ChromeDriver not in PATH\n\n"
                                 f"Please check your internet connection and try again.")
            return
    # Inject stealth tweaks to reduce bot detection
    try:
        stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter){
          if (parameter === 37445) { return 'Intel Open Source Technology Center'; }
          if (parameter === 37446) { return 'Mesa DRI Intel(R)'; }
          return getParameter.call(this, parameter);
        };
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
          parameters.name === 'notifications' ? Promise.resolve({ state: Notification.permission }) : originalQuery(parameters)
        );
        """
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': stealth_js})
    except Exception:
        pass
    collected_reviews = []

    try:
        if platform == "Amazon":
            driver.get("https://www.amazon.in/")
            try:
                # Optional: Handle "choose location" popup if it appears
                continue_button = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//span/span/button"))
                )
                continue_button.click()
                time.sleep(2)
                safe_print("[Amazon] Clicked continue button (location prompt).")
            except:
                safe_print("[Amazon] No continue button found, proceeding directly.")
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
        # Only close after we have clearly attempted navigation; if still on login, give one last chance when not headless
        try:
            if driver and is_on_amazon_signin_page(driver) and not HEADLESS:
                safe_print("[Amazon] Still on login at shutdown. Pausing 20s for manual completion...")
                time.sleep(20)
        except Exception:
            pass
        if driver:
            driver.quit()
            safe_print("[INFO] Browser closed.")

    if collected_reviews:
        analyze_reviews_with_gemini()
    else:
        safe_print("[INFO] Skipping Gemini analysis since no reviews were found.")


# ----------------- Tkinter UI -----------------
# Theme Colors (boAt style)
PRIMARY_BG = "#0d0d0d"
PRIMARY_FG = "#ffffff"
ACCENT_RED = "#e63946"
SECONDARY_GRAY = "#222222"

FONT_HEADER = ("Arial", 14, "bold")
FONT_TEXT = ("Arial", 11)
FONT_BUTTON = ("Arial", 11, "bold")

root = tk.Tk()
root.title("boAt - Product Review Analyzer")
root.geometry("850x750")
root.configure(bg=PRIMARY_BG)

# Use a container frame for all widgets, placed in the center
container = tk.Frame(root, bg=PRIMARY_BG)
container.pack(pady=20, padx=20, fill="both", expand=True)

# --- Configure Grid layout inside the container ---
container.columnconfigure(1, weight=1)  # Allow the input column to expand

# --- Add Logo ---
# Get the directory where this script is located
script_dir = os.path.dirname(os.path.abspath(__file__))
# Look for the logo image in the same directory as the script
logo_path = os.path.join(script_dir, "boatlogo.png")

try:
    logo_img = Image.open(logo_path)
    logo_img = logo_img.resize((80, 80), Image.Resampling.LANCZOS)  # Resize as needed
    logo_photo = ImageTk.PhotoImage(logo_img)

    logo_label = tk.Label(container, image=logo_photo, bg=PRIMARY_BG)
    logo_label.grid(row=0, column=0, columnspan=2, pady=(5, 10))
except FileNotFoundError:
    # If logo file is not found, create a text label instead
    logo_label = tk.Label(container, text="boAt", font=("Arial", 24, "bold"),
                          fg=ACCENT_RED, bg=PRIMARY_BG)
    logo_label.grid(row=0, column=0, columnspan=2, pady=(5, 10))
except Exception as e:
    # For any other error, create a text label
    logo_label = tk.Label(container, text="boAt", font=("Arial", 24, "bold"),
                          fg=ACCENT_RED, bg=PRIMARY_BG)
    logo_label.grid(row=0, column=0, columnspan=2, pady=(5, 10))

# Wrap the Text widget in a frame
title_frame = tk.Frame(container, bg=PRIMARY_BG)
title_frame.grid(row=1, column=0, columnspan=2, pady=(10, 5), sticky="ew")

# Make the frame expand horizontally
title_frame.columnconfigure(0, weight=1)

# Add Text widget
title_text = tk.Text(title_frame, bg=PRIMARY_BG, font=("Arial", 20, "bold"), height=1, borderwidth=0)
title_text.grid(row=0, column=0, sticky="ew")  # Fill horizontally

# Insert letters with tags
title_text.insert("end", "b", "white")
title_text.insert("end", "o", "white")
title_text.insert("end", "A", "red")
title_text.insert("end", "t - Product Review Analyzer", "white")

# Configure tags
title_text.tag_configure("white", foreground="white")
title_text.tag_configure("red", foreground=ACCENT_RED)

# Center-align text using a tag
title_text.tag_configure("center", justify="center")
title_text.tag_add("center", "1.0", "end")

# Make Text read-only
title_text.config(state="disabled")

subtitle = tk.Label(container, text="Plug into Reviews • Analyze • Summarize", font=("Arial", 12), fg=PRIMARY_FG,
                    bg=PRIMARY_BG)
subtitle.grid(row=2, column=0, columnspan=2, pady=(0, 20))

# --- Input Widgets ---
tk.Label(container, text="Product Name:", font=FONT_TEXT, fg=PRIMARY_FG, bg=PRIMARY_BG).grid(row=3, column=0,
                                                                                             sticky="w", padx=10,
                                                                                             pady=8)
product_entry = tk.Entry(container, font=FONT_TEXT, bg="white", fg=SECONDARY_GRAY, relief="flat",
                         insertbackground=SECONDARY_GRAY)
product_entry.grid(row=3, column=1, padx=10, pady=8, sticky="ew")

tk.Label(container, text="E-Commerce Platform:", font=FONT_TEXT, fg=PRIMARY_FG, bg=PRIMARY_BG).grid(row=4, column=0,
                                                                                                    sticky="w", padx=10,
                                                                                                    pady=8)
platform_var = tk.StringVar()
platform_dropdown = ttk.Combobox(container, textvariable=platform_var, state="readonly", font=FONT_TEXT,
                                 style="Custom.TCombobox")
platform_dropdown["values"] = ("Amazon", "Flipkart")
platform_dropdown.current(0)
platform_dropdown.grid(row=4, column=1, padx=10, pady=8, sticky="ew")

tk.Label(container, text="Select Option:", font=FONT_TEXT, fg=PRIMARY_FG, bg=PRIMARY_BG).grid(row=5, column=0,
                                                                                              sticky="w", padx=10,
                                                                                              pady=8)
option_var = tk.StringVar()
option_dropdown = ttk.Combobox(container, textvariable=option_var, state="readonly", font=FONT_TEXT,
                               style="Custom.TCombobox")
option_dropdown["values"] = ("Executive Summary", "Product Overall Star Rating", "Overall Impression",
                             "Summary of Positive Feedbacks",
                             "Summary of Negative Feedbacks", "Chat with Gemini")
option_dropdown.current(0)
option_dropdown.grid(row=5, column=1, padx=10, pady=8, sticky="ew")
option_var.trace("w", update_result_box)

# --- Submit Button ---
submit_btn = tk.Button(container, text="Analyze Reviews", bg=ACCENT_RED, fg=PRIMARY_FG, font=FONT_BUTTON, relief="flat",
                       width=20, height=2, cursor="hand2", command=run_scraper_thread)
submit_btn.grid(row=6, column=0, columnspan=2, pady=20)

# --- Progress Bar & Results ---
progress_var = tk.DoubleVar()
style = ttk.Style()
style.theme_use("clam")
style.configure("Custom.Horizontal.TProgressbar", troughcolor=SECONDARY_GRAY, bordercolor=PRIMARY_BG,
                background=ACCENT_RED)
progress_bar = ttk.Progressbar(container, variable=progress_var, maximum=100, style="Custom.Horizontal.TProgressbar")
progress_bar.grid(row=7, column=0, columnspan=2, pady=10, padx=10, sticky="ew")

tk.Label(container, text="Analysis Result / Logs:", font=FONT_HEADER, fg=PRIMARY_FG, bg=PRIMARY_BG).grid(row=8,
                                                                                                         column=0,
                                                                                                         columnspan=2,
                                                                                                         pady=(15, 5),
                                                                                                         sticky="w",
                                                                                                         padx=10)
output_box = scrolledtext.ScrolledText(container, wrap=tk.WORD, state="disabled", foreground=PRIMARY_FG,
                                       background=SECONDARY_GRAY, relief="flat", font=("Consolas", 11))
output_box.grid(row=9, column=0, columnspan=2, padx=10, pady=(0, 10), sticky="nsew")

# Make the results box row expandable
container.rowconfigure(9, weight=1)

# --- Chat Input Widgets (Initially Hidden) ---
chat_frame = tk.Frame(container, bg=PRIMARY_BG)
chat_frame.grid(row=10, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
chat_frame.grid_remove()  # Hide initially
chat_frame.columnconfigure(0, weight=1)

chat_input_var = tk.StringVar()
chat_input_entry = tk.Entry(chat_frame, textvariable=chat_input_var, font=FONT_TEXT, bg="white", fg=SECONDARY_GRAY,
                            relief="flat", insertbackground=SECONDARY_GRAY)
chat_input_entry.grid(row=0, column=0, sticky="ew")

chat_send_btn = tk.Button(chat_frame, text="Send", bg=ACCENT_RED, fg=PRIMARY_FG, font=FONT_BUTTON, relief="flat",
                          width=10, cursor="hand2", command=send_chat_question)
chat_send_btn.grid(row=0, column=1, padx=(10, 0))

# --- Save Button for Executive Summary ---
save_btn = tk.Button(chat_frame, text="Save", bg=ACCENT_RED, fg=PRIMARY_FG, font=FONT_BUTTON,
                     relief="flat", width=10, cursor="hand2", command=save_executive_summary)
save_btn.grid(row=0, column=2, padx=(10, 0))
save_btn.grid_remove()  # Hide initially

# --- Override Combobox styling ---
style.configure('Custom.TCombobox',
                fieldbackground='white',
                foreground=SECONDARY_GRAY,
                background=SECONDARY_GRAY,
                bordercolor=PRIMARY_BG,
                lightcolor=PRIMARY_BG,
                darkcolor=PRIMARY_BG)
style.map('Custom.TCombobox',
          fieldbackground=[('readonly', 'white'), ('!readonly', 'white')],
          foreground=[('readonly', SECONDARY_GRAY)],
          selectbackground=[('readonly', 'white')],
          selectforeground=[('readonly', SECONDARY_GRAY)])

option_var.set("Executive Summary")  # Set initial value
option_var.trace("w", update_result_box)

root.mainloop()
