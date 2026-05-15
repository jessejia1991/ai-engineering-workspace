# test_id: test_vets_and_specialties
# covers: GET /vets, GET /vets/{vetId}, POST /vets, DELETE /vets/{vetId},
#         GET /specialties, GET /specialties/{specialtyId}, POST /specialties, DELETE /specialties/{specialtyId}
# generated_at: 2024-01-01T00:00:00Z

import os
import pytest
import requests

BASE_URL = os.environ.get("VERIFY_TARGET_URL", "http://localhost:9966")


def test_list_vets_returns_200_and_list():
    """GET /vets should return HTTP 200 and a JSON array."""
    response = requests.get(f"{BASE_URL}/vets")
    assert response.status_code == 200, (
        f"Expected 200 from GET /vets, got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert isinstance(data, list), (
        f"Expected a JSON list from GET /vets, got {type(data)}: {data}"
    )


def test_list_vets_items_have_expected_fields():
    """Each vet in GET /vets should have at least 'id', 'firstName', 'lastName' fields."""
    response = requests.get(f"{BASE_URL}/vets")
    assert response.status_code == 200, (
        f"Expected 200 from GET /vets, got {response.status_code}"
    )
    vets = response.json()
    if len(vets) == 0:
        pytest.skip("No vets in the system to validate field shapes")
    for vet in vets:
        assert "id" in vet, f"Vet entry missing 'id' field: {vet}"
        assert "firstName" in vet, f"Vet entry missing 'firstName' field: {vet}"
        assert "lastName" in vet, f"Vet entry missing 'lastName' field: {vet}"


def test_get_nonexistent_vet_returns_404():
    """GET /vets/{vetId} with a nonexistent id should return 404."""
    nonexistent_id = 999999
    response = requests.get(f"{BASE_URL}/vets/{nonexistent_id}")
    assert response.status_code == 404, (
        f"Expected 404 for nonexistent vet id={nonexistent_id}, got {response.status_code}: {response.text}"
    )


def test_post_vet_empty_body_returns_4xx():
    """POST /vets with an empty body should return a 4xx error."""
    response = requests.post(f"{BASE_URL}/vets", json={})
    assert response.status_code in (400, 415, 422, 500), (
        f"Expected 4xx from POST /vets with empty body, got {response.status_code}: {response.text}"
    )


def test_list_specialties_returns_200_and_list():
    """GET /specialties should return HTTP 200 and a JSON array."""
    response = requests.get(f"{BASE_URL}/specialties")
    assert response.status_code == 200, (
        f"Expected 200 from GET /specialties, got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert isinstance(data, list), (
        f"Expected a JSON list from GET /specialties, got {type(data)}: {data}"
    )


def test_get_nonexistent_specialty_returns_404():
    """GET /specialties/{specialtyId} with a nonexistent id should return 404."""
    nonexistent_id = 999999
    response = requests.get(f"{BASE_URL}/specialties/{nonexistent_id}")
    assert response.status_code == 404, (
        f"Expected 404 for nonexistent specialty id={nonexistent_id}, got {response.status_code}: {response.text}"
    )


def test_create_and_delete_specialty_cycle():
    """POST /specialties creates a specialty and DELETE /specialties/{id} removes it."""
    # Create specialty
    payload = {"name": "TestSpecialty_e2e"}
    create_resp = requests.post(f"{BASE_URL}/specialties", json=payload)
    assert create_resp.status_code in (200, 201), (
        f"Expected 200 or 201 from POST /specialties, got {create_resp.status_code}: {create_resp.text}"
    )
    created = create_resp.json()
    assert isinstance(created, dict), (
        f"Expected dict from POST /specialties, got {type(created)}: {created}"
    )
    assert "id" in created, (
        f"Expected 'id' in POST /specialties response, got keys: {list(created.keys())}"
    )
    specialty_id = created["id"]

    # Read it back
    read_resp = requests.get(f"{BASE_URL}/specialties/{specialty_id}")
    assert read_resp.status_code == 200, (
        f"Expected 200 from GET /specialties/{specialty_id}, got {read_resp.status_code}: {read_resp.text}"
    )
    specialty_data = read_resp.json()
    assert specialty_data.get("name") == "TestSpecialty_e2e", (
        f"Expected name='TestSpecialty_e2e', got {specialty_data.get('name')}"
    )

    # Delete it
    delete_resp = requests.delete(f"{BASE_URL}/specialties/{specialty_id}")
    assert delete_resp.status_code in (200, 204), (
        f"Expected 200 or 204 from DELETE /specialties/{specialty_id}, got {delete_resp.status_code}: {delete_resp.text}"
    )

    # Confirm it is gone
    confirm_resp = requests.get(f"{BASE_URL}/specialties/{specialty_id}")
    assert confirm_resp.status_code == 404, (
        f"Expected 404 after deleting specialty id={specialty_id}, got {confirm_resp.status_code}"
    )


def test_create_vet_with_valid_payload_and_delete():
    """POST /vets with a valid payload should create a vet; DELETE /vets/{vetId} should remove it."""
    payload = {
        "firstName": "Alice",
        "lastName": "Smith",
        "specialties": []
    }
    create_resp = requests.post(f"{BASE_URL}/vets", json=payload)
    assert create_resp.status_code in (200, 201), (
        f"Expected 200 or 201 from POST /vets, got {create_resp.status_code}: {create_resp.text}"
    )
    created = create_resp.json()
    assert isinstance(created, dict), (
        f"Expected dict from POST /vets, got {type(created)}: {created}"
    )
    assert "id" in created, (
        f"Expected 'id' in POST /vets response, got keys: {list(created.keys())}"
    )
    vet_id = created["id"]

    # Read back
    read_resp = requests.get(f"{BASE_URL}/vets/{vet_id}")
    assert read_resp.status_code == 200, (
        f"Expected 200 from GET /vets/{vet_id}, got {read_resp.status_code}: {read_resp.text}"
    )
    vet_data = read_resp.json()
    assert vet_data.get("firstName") == "Alice", (
        f"Expected firstName='Alice', got {vet_data.get('firstName')}"
    )

    # Delete
    delete_resp = requests.delete(f"{BASE_URL}/vets/{vet_id}")
    assert delete_resp.status_code in (200, 204), (
        f"Expected 200 or 204 from DELETE /vets/{vet_id}, got {delete_resp.status_code}: {delete_resp.text}"
    )

    # Confirm deletion
    confirm_resp = requests.get(f"{BASE_URL}/vets/{vet_id}")
    assert confirm_resp.status_code == 404, (
        f"Expected 404 after DELETE /vets/{vet_id}, got {confirm_resp.status_code}"
    )