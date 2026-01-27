"""
Tata Motors Dealer Locator - Fetches and parses dealer data from the locator page.
"""

import requests
import json
import re
from bs4 import BeautifulSoup
from typing import Optional
from urllib.parse import quote


def fetch_dealers_html(search_query: str) -> str:
    """Fetch the dealer locator HTML page for a given search query."""
    
    encoded_query = quote(search_query)
    url = f"https://dealer-locator.cars.tatamotors.com/?search={encoded_query}"
    
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "priority": "u=0, i",
        "referer": "https://dealer-locator.cars.tatamotors.com/",
        "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    }
    
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def extract_dealer_json_from_html(html: str) -> list[dict]:
    """Extract dealer data from hidden input fields in the HTML."""
    
    soup = BeautifulSoup(html, "html.parser")
    
    # Find all dealer cards (store-info-box containers)
    dealer_cards = soup.find_all("div", class_="store-info-box")
    dealer_objects = []
    
    for card in dealer_cards:
        dealer = {}
        
        # Extract hidden input values by class
        field_mappings = {
            "outlet-latitude": "latitude",
            "outlet-longitude": "longitude",
            "business_name": "name",
            "business_city": "city",
            "business_email": "email",
            "address": "address",
            "phone": "phone",
            "state": "state",
        }
        
        for input_class, field_name in field_mappings.items():
            input_elem = card.find("input", class_=input_class)
            if input_elem and input_elem.get("value"):
                dealer[field_name] = input_elem.get("value").strip()
        
        # Extract additional data from visible elements
        # Dealer name from h2
        name_elem = card.find("h2")
        if name_elem and not dealer.get("name"):
            dealer["name"] = name_elem.get_text(strip=True)
        
        # Address from paragraph
        addr_elem = card.find("p", class_="address")
        if addr_elem:
            dealer["full_address"] = addr_elem.get_text(strip=True)
        
        # Phone links
        phone_links = card.find_all("a", href=re.compile(r"^tel:"))
        if phone_links:
            phones = [link.get("href").replace("tel:", "") for link in phone_links]
            dealer["phones"] = phones
        
        # Website link
        website_link = card.find("a", {"data-track-event-category": True})
        if website_link and website_link.get("href"):
            dealer["website"] = website_link.get("href")
        
        if dealer.get("name") or dealer.get("latitude"):
            dealer_objects.append(dealer)
    
    return dealer_objects


def normalize_dealer(d: dict) -> dict:
    """Normalize a raw dealer object into a clean schema."""
    
    # Extract pincode from address (6-digit Indian pincode)
    address_text = d.get("address") or d.get("full_address") or ""
    pincode = None
    pincode_match = re.search(r'\b(\d{6})\b', address_text)
    if pincode_match:
        pincode = pincode_match.group(1)
    
    return {
        "dealer_id": d.get("store_id") or d.get("id"),
        "name": d.get("name"),
        "brand": "Tata Motors",
        "address": {
            "line1": address_text,
            "city": d.get("city"),
            "state": d.get("state"),
            "pincode": pincode
        },
        "location": {
            "lat": float(d["latitude"]) if d.get("latitude") else None,
            "lng": float(d["longitude"]) if d.get("longitude") else None
        },
        "phone": d.get("phones") or ([d.get("phone")] if d.get("phone") else None),
        "email": d.get("email"),
        "website": d.get("website"),
        "source": "tatamotors-storelocator"
    }


def get_dealers(search_query: str) -> list[dict]:
    """
    Main function: fetch dealers for a location search query.
    
    Args:
        search_query: Location string (e.g., "Mumbai, Maharashtra 400018")
    
    Returns:
        List of normalized dealer dictionaries
    """
    
    html = fetch_dealers_html(search_query)
    raw_dealers = extract_dealer_json_from_html(html)
    
    # Deduplicate by name + location
    seen = set()
    clean_dealers = []
    
    for d in raw_dealers:
        normalized = normalize_dealer(d)
        
        # Create unique key from name + coordinates
        name = normalized.get("name", "")
        lat = normalized.get("location", {}).get("lat")
        lng = normalized.get("location", {}).get("lng")
        key = f"{name}|{lat}|{lng}"
        
        if key in seen:
            continue
        
        seen.add(key)
        clean_dealers.append(normalized)
    
    return clean_dealers


if __name__ == "__main__":
    # Example usage
    search = "Mumbai, Maharashtra 400018"
    dealers = get_dealers(search)
    print(json.dumps(dealers, indent=2, ensure_ascii=False))
