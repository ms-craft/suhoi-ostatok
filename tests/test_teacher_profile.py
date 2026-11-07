from sqlalchemy import select
from app.accounts import User


def test_registration_profile_and_login(harness):
    credentials = {"email": "teacher@example.test", "password": "a-secure-password-123"}
    assert harness.client.post('/v1/auth/register', json=credentials).status_code == 422
    response = harness.client.post('/v1/auth/register', json={**credentials, "full_name": "  Иван   Иванов  "})
    assert response.status_code == 201
    auth = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert harness.client.get('/v1/auth/profile', headers=auth).json()['full_name'] == 'Иван Иванов'
    assert harness.client.post('/v1/auth/login', json=credentials).status_code == 200
    assert harness.client.get('/v1/auth/profile').status_code == 401


def test_existing_account_can_add_name(harness):
    auth, credentials = harness.register()
    with harness.app.state.session_factory.begin() as db:
        user = db.scalar(select(User).where(User.email == credentials['email']))
        user.full_name = None
    assert harness.client.get('/v1/auth/profile', headers=auth).json()['full_name'] is None
    assert harness.client.patch('/v1/auth/profile', headers=auth, json={'full_name': 'Елена Иванова'}).json()['full_name'] == 'Елена Иванова'
    assert harness.client.post('/v1/auth/login', json=credentials).status_code == 200
