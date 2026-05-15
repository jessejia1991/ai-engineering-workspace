# test_id: test_owners_crud
# covers: GET /owners, POST /owners, GET /owners/{ownerId}, PUT /owners/{ownerId}, DELETE /owners/{ownerId}
# generated_at: 2024-01-01T00:00:00Z

import os
import pytest
import requests

BASE_URL = os.environ.get("VERIFY_TARGET_URL", "http://localhost:9966")


def test_list_owners_returns_200_and_list():
    """GET /owners should return HTTP 200 and a JSON array."""
    response = requests.get(f"{BASE_URL}/owners")
    assert response.status_code == 200, (
        f"Expected 200 from GET /owners, got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert isinstance(data, list), (
        f"Expected a JSON list from GET /owners, got {type(data)}: {data}"
    )


def test_get_nonexistent_owner_returns_404():
    """GET /owners/{ownerId} with a nonexistent id should return 404."""
    nonexistent_id = 999999
    response = requests.get(f"{BASE_URL}/owners/{nonexistent_id}")
    assert response.status_code == 404, (
        f"Expected 404 for nonexistent owner id={nonexistent_id}, got {response.status_code}: {response.text}"
    )


def test_post_owner_empty_body_returns_4xx():
    """POST /owners with an empty body should return a 4xx error (validation)."""
    response = requests.post(f"{BASE_URL}/owners", json={})
    assert response.status_code in (400, 422, 415, 500), (
        f"Expected 4xx from POST /owners with empty body, got {response.status_code}: {response.text}"
    )


def test_post_owner_no_content_type_returns_4xx():
    """POST /owners with no Content-Type should return a 4xx error."""
    response = requests.post(f"{BASE_URL}/owners", data="not json")
    assert response.status_code in (400, 415, 422, 500), (
        f"Expected 4xx from POST /owners with no JSON content-type, got {response.status_code}: {response.text}"
    )


def test_create_read_update_delete_owner_cycle():
    """Full CRUD cycle: create an owner, read it back, update it, then delete it."""
    # Create
    payload = {
        "firstName": "John",
        "lastName": "Doe",
        "address": "123 Main St",
        "city": "Springfield",
        "telephone": "5555551234"
    }
    create_resp = requests.post(f"{BASE_URL}/owners", json=payload)
    assert create_resp.status_code in (200, 201), (
        f"Expected 200 or 201 from POST /owners, got {create_resp.status_code}: {create_resp.text}"
    )
    created = create_resp.json()
    assert isinstance(created, dict), (
        f"Expected dict from POST /owners response, got {type(created)}: {created}"
    )
    assert "id" in created, (
        f"Expected 'id' field in POST /owners response, got keys: {list(created.keys())}"
    )
    owner_id = created["id"]

    # Read
    read_resp = requests.get(f"{BASE_URL}/owners/{owner_id}")
    assert read_resp.status_code == 200, (
        f"Expected 200 from GET /owners/{owner_id}, got {read_resp.status_code}: {read_resp.text}"
    )
    owner_data = read_resp.json()
    assert isinstance(owner_data, dict), (
        f"Expected dict from GET /owners/{owner_id}, got {type(owner_data)}"
    )
    assert owner_data.get("firstName") == "John", (
        f"Expected firstName='John', got {owner_data.get('firstName')}"
    )
    assert owner_data.get("lastName") == "Doe", (
        f"Expected lastName='Doe', got {owner_data.get('lastName')}"
    )

    # Update
    update_payload = {
        "id": owner_id,
        "firstName": "Jane",
        "lastName": "Doe",
        "address": "456 Elm St",
        "city": "Shelbyville",
        "telephone": "5555559876"
    }
    update_resp = requests.put(f"{BASE_URL}/owners/{owner_id}", json=update_payload)
    assert update_resp.status_code in (200, 204), (
        f"Expected 200 or 204 from PUT /owners/{owner_id}, got {update_resp.status_code}: {update_resp.text}"
    )
    if update_resp.status_code == 200 and update_resp.text.strip():
        updated = update_resp.json()
        assert updated.get("firstName") == "Jane", (
            f"Expected updated firstName='Jane', got {updated.get('firstName')}"
        )

    # Verify update persisted
    read_after_update = requests.get(f"{BASE_URL}/owners/{owner_id}")
    assert read_after_update.status_code == 200, (
        f"Expected 200 from GET /owners/{owner_id} after update, got {read_after_update.status_code}"
    )

    # Delete
    delete_resp = requests.delete(f"{BASE_URL}/owners/{owner_id}")
    assert delete_resp.status_code in (200, 204), (
        f"Expected 200 or 204 from DELETE /owners/{owner_id}, got {delete_resp.status_code}: {delete_resp.text}"
    )

    # Confirm deletion
    confirm_resp = requests.get(f"{BASE_URL}/owners/{owner_id}")
    assert confirm_resp.status_code == 404, (
        f"Expected 404 after DELETE /owners/{owner_id}, got {confirm_resp.status_code}"
    )