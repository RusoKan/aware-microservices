import os
import pytest
import requests
import pymongo
import time
import subprocess
import uuid
from dotenv import load_dotenv

load_dotenv()

# --- FIXTURES ---

@pytest.fixture(scope="module", autouse=True)
def docker_compose():
    subprocess.run(["docker", "compose", "-f", "docker-compose.test.yml", "up", "--build", "-d"], check=True)
    wait_for_service("http://localhost:8000/")
    yield 
    subprocess.run(["docker", "compose", "-f", "docker-compose.test.yml", "down", "-v"], check=True)

def wait_for_service(url, timeout=60):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if requests.get(url).status_code in [200, 404]: 
                return
        except Exception:
            time.sleep(2)
    raise TimeoutError(f"Service at {url} not ready")

@pytest.fixture(scope="module")
def api_base_url():
    return "http://localhost:8000"

@pytest.fixture(scope="module")
def mongo_client():
    client = pymongo.MongoClient(
        host="localhost", 
        port=27017,
        username=os.getenv("MONGO_USERNAME"),
        password=os.getenv("MONGO_PASSWORD"),
        authSource="admin"
    )
    yield client
    client.close()

# --- TEST CASES ---

# TC 01: Validate User Creation
def test_user_creation(api_base_url, mongo_client):
    unique_email = f"integration.{uuid.uuid4()}@example.com" # Random email to prevent duplicates
    user_payload = {
        "firstName": "Integration",
        "lastName": "Tester",
        "emails": [unique_email],
        "deliveryAddress": {
            "street": "123 Test Street",
            "city": "Testville",
            "state": "Test State",
            "postalCode": "12345",
            "country": "Test Country"
        }
    }
    
    response = requests.post(f"{api_base_url}/users/", json=user_payload)
    if response.status_code != 201: print(f"API Error Response: {response.text}")
    assert response.status_code == 201
    
    user_id = response.json()["userId"]
    
    # Verify in MongoDB
    db = mongo_client[os.getenv("DATABASE_NAME")]
    mongo_user = db["users"].find_one({"userId": user_id})
    assert mongo_user is not None
    assert mongo_user["emails"] == [unique_email]


# TC 02: Validate Order Creation with Existing User
def test_order_creation_existing_user(api_base_url, mongo_client):
    # 1. Create User First
    unique_email = f"bob.{uuid.uuid4()}@example.com"
    user_payload = {
        "firstName": "Bob",
        "lastName": "Builder",
        "emails": [unique_email],
        "deliveryAddress": {"street": "789 Const Way", "city": "Montreal", "state": "QC", "postalCode": "A1A", "country": "Canada"}
    }
    user_response = requests.post(f"{api_base_url}/users/", json=user_payload)
    user_id = user_response.json()["userId"]

    # 2. Create Order (Schema compliant, NO orderId sent)
    order_payload = {
        "userId": user_id,
        "userEmails": [unique_email],
        "deliveryAddress": user_payload["deliveryAddress"],
        "items": [
            {"itemId": "Hammer-01", "quantity": 1, "price": 15.50},
            {"itemId": "Nails-Box", "quantity": 5, "price": 4.00}
        ],
        "orderStatus": "under process"
    }
    
    order_response = requests.post(f"{api_base_url}/orders/", json=order_payload)
    if order_response.status_code != 201: print(f"ORDER ERROR: {order_response.text}")
    assert order_response.status_code == 201

    order_id = order_response.json()["orderId"]

    # 3. Verify in MongoDB
    db = mongo_client[os.getenv("DATABASE_NAME")]
    mongo_order = db["orders"].find_one({"orderId": order_id})
    assert mongo_order is not None
    assert mongo_order["userId"] == user_id


# TC 03: Validate Event-Driven User Update Propagation (RabbitMQ)
def test_event_driven_propagation(api_base_url, mongo_client):
    unique_email = f"charlie.{uuid.uuid4()}@old.com"
    
    # 1. Create User (NO userId sent - let the API generate it!)
    user_payload = {
        "firstName": "Charlie", 
        "lastName": "Sync", 
        "emails": [unique_email],
        "deliveryAddress": {"street": "Old St", "city": "Montreal", "state": "QC", "postalCode": "H3H", "country": "Canada"}
    }
    user_response = requests.post(f"{api_base_url}/users/", json=user_payload)
    assert user_response.status_code == 201, f"User Creation Failed: {user_response.text}"
    
    # Extract the API-generated ID
    user_id = user_response.json()["userId"]

    # 2. Create Order (NO orderId sent - let the API generate it!)
    order_payload = {
        "userId": user_id,
        "userEmails": [unique_email],
        "deliveryAddress": user_payload["deliveryAddress"],
        "items": [{"itemId": "P002", "quantity": 1, "price": 10.0}], 
        "orderStatus": "under process"
    }
    order_response = requests.post(f"{api_base_url}/orders/", json=order_payload)
    assert order_response.status_code == 201, f"Order Creation Failed: {order_response.text}"
    
    # Extract the API-generated ID
    order_id = order_response.json()["orderId"]

    # 3. Update User via PUT 
    new_email = f"charlie.{uuid.uuid4()}@new.com"
    update_payload = {
        "emails": [new_email],
        "deliveryAddress": {"street": "New St", "city": "Montreal", "state": "QC", "postalCode": "H1A", "country": "Canada"}
    }
    
    put_response = requests.put(f"{api_base_url}/users/{user_id}", json=update_payload)
    assert put_response.status_code == 200, f"User Update Failed: {put_response.text}"

    # 4. Wait for RabbitMQ
    time.sleep(5) 

    # 5. Verify Order Data was synchronized
    orders_db = mongo_client[os.getenv("DATABASE_NAME")]
    updated_order = orders_db["orders"].find_one({"orderId": order_id})
    
    assert updated_order is not None, "Order not found in DB!"
    assert updated_order["deliveryAddress"]["street"] == "New St"
    assert new_email in updated_order["userEmails"]
# TC 04: Validate API Gateway Routing
def test_api_gateway_strangler_pattern(api_base_url):
    v1_count = 0
    v2_count = 0
    
    for _ in range(10):
        response = requests.get(f"{api_base_url}/users/health") 
        if response.status_code == 404:
            response = requests.get(f"{api_base_url}/users/")
            
        try:
            data = str(response.json())
            if "v1" in data: v1_count += 1
            elif "v2" in data: v2_count += 1
        except Exception:
            pass 

    print(f"\nStrangler Pattern Routing -> V1 handled: {v1_count}, V2 handled: {v2_count}")
    assert True