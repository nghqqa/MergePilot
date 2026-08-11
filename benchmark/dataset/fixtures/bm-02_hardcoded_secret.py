import requests

API_KEY = "sk-7928cff15f944ca185c92f7da21c591c"
BASE_URL = "https://api.example.com/v1"

def get_user_data(user_id):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    resp = requests.get(f"{BASE_URL}/users/{user_id}", headers=headers)
    return resp.json()

def update_profile(user_id, data):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    resp = requests.put(f"{BASE_URL}/users/{user_id}", json=data, headers=headers)
    return resp.json()
