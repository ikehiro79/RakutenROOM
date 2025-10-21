"""Automation script for posting reviews to Rakuten ROOM.

This module fetches product metadata from a Rakuten product page, generates a
concise review, and posts it to Rakuten ROOM via the "ROOMへ投稿" link that is
present on the product page.  The workflow intentionally separates the
responsibilities of scraping, review generation, and browser automation so that
it can be easily customised for different product layouts or future UI changes.

Environment variables
---------------------
RAKUTEN_ROOM_USERNAME
    Rakuten credentials used when the ROOM posting flow requires a login.
RAKUTEN_ROOM_PASSWORD
    Password for the Rakuten account.

These can also be supplied explicitly from the command line if you do not wish
 to store them in the environment.

Usage
-----
python room_auto_poster.py "<product_url>"

The script launches a headless Chrome session by default.  Remove the
``--headless`` flag to see the browser window for debugging purposes.
"""
from __future__ import annotations

import argparse
import os
import textwrap
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import requests
from requests import RequestException
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


# Rakuten product pages frequently expose the title, price and shop name using
# schema.org markup.  These CSS selectors work well today but are intentionally
# written so they can be easily updated should the HTML structure change.
DEFAULT_TITLE_SELECTORS: tuple[str, ...] = (
    "meta[property='og:title']",
    "h1[itemprop='name']",
    "title",
)
DEFAULT_PRICE_SELECTORS: tuple[str, ...] = (
    "span[itemprop='price']",
    ".price2",
    "span[class*='price']",
)
DEFAULT_SHOP_SELECTORS: tuple[str, ...] = (
    "a[itemprop='seller']",
    "a[class*='ShopName']",
)

# Rakuten ROOM currently renders the review input as a textarea with a distinct
# name attribute.  Because selectors may change we keep a list of fallbacks.
DEFAULT_ROOM_REVIEW_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.CSS_SELECTOR, "textarea[name='comment']"),
    (By.CSS_SELECTOR, "textarea[class*='comment']"),
)
DEFAULT_ROOM_SUBMIT_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.CSS_SELECTOR, "button[type='submit']"),
    (By.CSS_SELECTOR, "button[class*='submit']"),
)

# Rakuten login forms use a variety of input names depending on the flow.  These
# selectors cover the common cases for username (user ID) and password fields.
LOGIN_USERNAME_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.ID, "loginInner_u"),
    (By.NAME, "u"),
    (By.NAME, "login_id"),
)
LOGIN_PASSWORD_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.ID, "loginInner_p"),
    (By.NAME, "p"),
    (By.NAME, "passwd"),
)
LOGIN_SUBMIT_SELECTORS: tuple[tuple[str, str], ...] = (
    (By.ID, "loginInner_y"),
    (By.NAME, "submit"),
    (By.CSS_SELECTOR, "button[type='submit']"),
)


@dataclass
class ProductInfo:
    """Basic metadata extracted from the product page."""

    title: str
    price: Optional[str] = None
    shop_name: Optional[str] = None


@dataclass
class RoomPosterConfig:
    """Configuration for the automation workflow."""

    username: Optional[str]
    password: Optional[str]
    headless: bool = True
    review_selectors: tuple[tuple[str, str], ...] = DEFAULT_ROOM_REVIEW_SELECTORS
    submit_selectors: tuple[tuple[str, str], ...] = DEFAULT_ROOM_SUBMIT_SELECTORS


def fetch_product_info(url: str, *, retries: int = 3, timeout: int = 20) -> ProductInfo:
    """Fetch product metadata from a Rakuten item page."""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        " AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/123.0.0.0 Safari/537.36"
    }

    last_error: Optional[Exception] = None
    response = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            break
        except RequestException as exc:
            last_error = exc
            if attempt == retries:
                raise RuntimeError(
                    "商品ページの取得中にタイムアウトまたは通信エラーが発生しました。"
                ) from exc
            time.sleep(2 * attempt)
    else:  # pragma: no cover - safety net
        raise RuntimeError("商品ページの取得に失敗しました。")

    if response is None:  # pragma: no cover - safety net
        raise RuntimeError("商品ページの取得に失敗しました。")

    soup = BeautifulSoup(response.text, "html.parser")

    title = _first_text_match(soup, DEFAULT_TITLE_SELECTORS) or "楽天の商品"
    price = _first_text_match(soup, DEFAULT_PRICE_SELECTORS)
    shop_name = _first_text_match(soup, DEFAULT_SHOP_SELECTORS)

    # ``meta`` tags store their data in the ``content`` attribute.
    if title and "\n" in title:
        title = " ".join(title.split())

    return ProductInfo(title=title.strip(), price=_clean_text(price), shop_name=_clean_text(shop_name))


def _first_text_match(soup: BeautifulSoup, selectors: Iterable[str]) -> Optional[str]:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            if node.has_attr("content"):
                return node["content"]
            return node.get_text(strip=True)
    return None


def _clean_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return " ".join(value.split())


def generate_review(info: ProductInfo) -> str:
    """Generate a concise review capped at 400 Japanese characters."""

    key_point_parts = [info.title]
    if info.price:
        key_point_parts.append(f"{info.price}で手に入る")
    if info.shop_name:
        key_point_parts.append(f"{info.shop_name}の人気アイテム")
    key_point_summary = "・".join(key_point_parts)

    bullet_points = [
        f"デザイン: {info.title}の魅力を活かした上質な仕上がり。",
        "使い勝手: 日常から特別なシーンまで幅広く活躍。",
        "満足度: 口コミでも高評価で贈り物にもおすすめ。",
    ]

    lines = [
        f"要点: {key_point_summary}",
        "",
        *[f"・{point}" for point in bullet_points],
    ]

    review = "\n".join(lines)
    if len(review) > 400:
        review = review[:397] + "…"
    return review


def create_driver(headless: bool = True) -> WebDriver:
    """Initialise a Selenium WebDriver instance for Chrome."""

    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,720")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def navigate_to_room(driver: WebDriver, product_url: str) -> None:
    """Open the product page and follow the ROOMへ投稿 link."""

    driver.get(product_url)
    wait = WebDriverWait(driver, 15)

    room_link = wait.until(
        EC.element_to_be_clickable((By.PARTIAL_LINK_TEXT, "ROOMへ投稿"))
    )
    room_link.click()

    # The ROOM flow opens in a new tab or window.  Switch context if needed.
    time.sleep(1)
    if len(driver.window_handles) > 1:
        driver.switch_to.window(driver.window_handles[-1])


def login_if_required(driver: WebDriver, config: RoomPosterConfig) -> None:
    """Attempt to log into Rakuten if a login form is detected."""

    if not config.username or not config.password:
        return

    wait = WebDriverWait(driver, 15)

    try:
        username_input = _find_first_element(driver, wait, LOGIN_USERNAME_SELECTORS)
        password_input = _find_first_element(driver, wait, LOGIN_PASSWORD_SELECTORS)
    except TimeoutException:
        return  # No login form detected.

    username_input.clear()
    username_input.send_keys(config.username)
    password_input.clear()
    password_input.send_keys(config.password)

    try:
        login_button = _find_first_element(driver, wait, LOGIN_SUBMIT_SELECTORS)
    except TimeoutException as exc:
        raise RuntimeError("Unable to locate login submit button.") from exc

    login_button.click()
    # Wait for the login process to complete by observing a navigation.
    wait.until(EC.number_of_windows_to_be(1))
    time.sleep(1)


def post_review(driver: WebDriver, review: str, config: RoomPosterConfig) -> None:
    """Populate the ROOM review form and submit it."""

    wait = WebDriverWait(driver, 20)

    try:
        textarea = _find_first_element(driver, wait, config.review_selectors)
    except TimeoutException as exc:
        raise RuntimeError("Unable to locate ROOM review textarea.") from exc

    textarea.clear()
    textarea.send_keys(review)

    try:
        submit_button = _find_first_element(driver, wait, config.submit_selectors)
    except TimeoutException as exc:
        raise RuntimeError("Unable to locate ROOM submit button.") from exc

    submit_button.click()
    # Allow some time for the submission to complete.
    time.sleep(3)


def _find_first_element(
    driver: WebDriver,
    wait: WebDriverWait,
    selectors: Iterable[tuple[str, str]],
):
    last_error: Optional[Exception] = None
    for by, value in selectors:
        try:
            return wait.until(EC.presence_of_element_located((by, value)))
        except (TimeoutException, NoSuchElementException) as exc:  # pragma: no cover - selenium
            last_error = exc
    if last_error:
        raise TimeoutException(str(last_error))
    raise TimeoutException("No selectors provided")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(
            """\
            楽天の商品URLからレビューを自動生成し、ROOMへ投稿するスクリプトです。

            実行例:
                python room_auto_poster.py "https://item.rakuten.co.jp/..."
            """
        ),
    )
    parser.add_argument("url", help="投稿したい楽天商品のURL")
    parser.add_argument("--username", default=os.getenv("RAKUTEN_ROOM_USERNAME"), help="楽天ID")
    parser.add_argument("--password", default=os.getenv("RAKUTEN_ROOM_PASSWORD"), help="楽天パスワード")
    parser.add_argument("--no-headless", action="store_true", help="ヘッドレスモードを無効化します")

    args = parser.parse_args()

    config = RoomPosterConfig(
        username=args.username,
        password=args.password,
        headless=not args.no_headless,
    )

    try:
        product_info = fetch_product_info(args.url)
    except RuntimeError as exc:
        raise SystemExit(
            "商品情報の取得に失敗しました。ネットワーク環境を確認し、再度お試しください。"
        ) from exc

    review = generate_review(product_info)

    driver = create_driver(headless=config.headless)
    try:
        navigate_to_room(driver, args.url)
        login_if_required(driver, config)
        post_review(driver, review, config)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()