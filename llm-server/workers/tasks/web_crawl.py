from urllib.parse import urlparse, urljoin
import requests
from celery import shared_task

import traceback

from langchain.text_splitter import RecursiveCharacterTextSplitter

from shared.utils.opencopilot_utils.init_vector_store import init_vector_store
from shared.utils.opencopilot_utils.interfaces import StoreOptions
from shared.models.opencopilot_db.website_data_sources import (
    count_crawled_pages,
    create_website_data_source,
    get_website_data_source_by_id,
)
from utils.llm_consts import max_pages_to_crawl
from models.repository.copilot_settings import ChatbotSettingCRUD
from workers.tasks.url_parsers import ParserFactory

from workers.utils.remove_escape_sequences import remove_escape_sequences
from utils.get_logger import CustomLogger

from bs4 import BeautifulSoup

logger = CustomLogger(__name__)


def get_links(url: str) -> list:
    if url.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".mp4", ".avi", ".mkv")):
        return []
    # Send a GET request to the URL
    response = requests.get(url)

    # Check if the request was successful (status code 200)
    if response.status_code == 200:
        # Parse the HTML content using BeautifulSoup
        soup = BeautifulSoup(response.text, "lxml")

        # Extract all links on the page
        links = [a.get("href") for a in soup.find_all("a", href=True)]

        # Filter out relative links and create absolute URLs
        absolute_links = [urljoin(url, link) for link in links if urlparse(link).scheme]

        # Filter out links with a different host or subdomain
        same_host_links = [
            link
            for link in absolute_links
            if urlparse(link).hostname == urlparse(url).hostname
        ]

        # Remove trailing '/' from each link using urlparse
        same_host_links = [
            urlparse(link)._replace(path=urlparse(link).path.rstrip("/")).geturl()
            for link in same_host_links
        ]

        return same_host_links
    else:
        # Print an error message if the request was not successful
        print(f"Failed to retrieve content. Status code: {response.status_code}")
        return []


def scrape_url(url: str):
    try:
        parser = ParserFactory.get_parser(url)
        response = requests.get(url)

        return parser.parse(response.content)
    except ValueError as e:
        # Log an error message if no parser is available for the content type
        logger.error(str(e))
        return None


def scrape_website(url: str, bot_id: str, max_pages: int) -> int:
    """Scrapes a website in breadth-first order, following all of the linked pages.

    Args:
      url: The URL of the website to scrape.
      max_pages: The maximum number of pages to scrape.

    Returns:
      The total number of scraped pages.
    """
    total_pages_scraped = count_crawled_pages(bot_id)
    if total_pages_scraped > max_pages:
        logger.warn(
            "web_crawl_max_pages_reached",
            f"Reached max pages to crawl for chatbot {bot_id}. Stopping crawl.",
        )

    visited_urls = set()

    # Use a queue for breadth-first scraping
    queue = [url]

    while queue and total_pages_scraped < max_pages:
        current_url = queue.pop(0)

        # Skip if the URL has been visited
        if current_url in visited_urls:
            continue

        try:
            # Scrape the content of the current URL
            if current_url.endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".mp4", ".avi", ".mkv")
            ):
                continue

            contents = scrape_url(current_url)

            for content in contents or []:
                # Check if scraping was successful
                if content is not None:
                    # Process the scraped content as needed
                    target_text = remove_escape_sequences(content.target_text)
                    text_splitter = RecursiveCharacterTextSplitter(
                        chunk_size=1000, chunk_overlap=200, length_function=len
                    )
                    docs = text_splitter.create_documents([target_text])
                    init_vector_store(
                        docs,
                        StoreOptions(
                            namespace="knowledgebase",
                            metadata={
                                "bot_id": bot_id,
                                "link": current_url,
                                "title": content.link_text,
                                "scroll_id": content.href,
                            },
                        ),
                    )
                    create_website_data_source(
                        chatbot_id=bot_id, url=current_url, status="SUCCESS"
                    )

                    total_pages_scraped += 1

                    # Get links on the current page
                    links = get_links(current_url)

                    # Add new links to the queue
                    queue.extend(links)

        except Exception as e:
            logger.error(f"Error scraping {current_url}: {e}")

        # Mark the URL as visited
        visited_urls.add(current_url)

    return total_pages_scraped


@shared_task
def web_crawl(url, bot_id: str):
    try:
        # setting = ChatbotSettings.get_chatbot_setting(bot_id)
        setting = ChatbotSettingCRUD.get_chatbot_setting(bot_id)

        if setting is None:
            setting = ChatbotSettingCRUD.create_chatbot_setting(
                max_pages_to_crawl=max_pages_to_crawl, chatbot_id=bot_id
            )

        logger.info(f"chatbot_settings: {setting.max_pages_to_crawl}")
        create_website_data_source(chatbot_id=bot_id, status="PENDING", url=url)

        scrape_website(url, bot_id, setting.max_pages_to_crawl or max_pages_to_crawl)  # type: ignore
    except Exception as e:
        traceback.print_exc()


@shared_task
def resume_failed_website_scrape(website_data_source_id: str):
    """Resumes a failed website scrape.

    Args:
      website_data_source_id: The ID of the website data source to resume scraping.
    """

    # Get the website data source.
    website_data_source = get_website_data_source_by_id(website_data_source_id)

    # Get the URL of the website to scrape.
    url = website_data_source.url

    scrape_website(url, website_data_source.bot_id, max_pages_to_crawl)
