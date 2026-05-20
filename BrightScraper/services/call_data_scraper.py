import requests
import json

def scrape_instagram_profile(username:str):
    url = "http://localhost:8000/profiles/scrape"
    
    payload = {
        "username": username,
        "max_posts": 24,
        "max_comments": 50,
        "post_workers": 2,
        "force_refresh": False,
        "cache_max_age_days": 7
    }
    
    headers = {
        "accept": "application/json",
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()  # Raise an exception for HTTP errors (4xx, 5xx)
        
        print("Status Code:", response.status_code)
        print("Response JSON:")
        print(json.dumps(response.json(), indent=2))
        
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    username='masoomminawala'
    scrape_instagram_profile(username=username)