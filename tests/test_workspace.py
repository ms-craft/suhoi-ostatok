def test_workspace_is_owner_scoped_and_revision_checked(harness):
    first, _ = harness.register()
    other, _ = harness.register()
    path = '/v1/workspace'
    assert harness.client.get(path).status_code == 401
    assert harness.client.get(path, headers=first).json() == {'revision': 0, 'workspace': None}
    data = {'classes': [{'id': 'class1', 'name': '7 А'}],
            'tests': [{'id': 'test1', 'name': 'Работа', 'questions': [{'question': '2+2', 'answer': '4'}]}],
            'links': {'submission-id': {'classId': 'class1', 'testId': 'test1'}}}
    created = harness.client.put(path, headers=first, json={'revision': 0, 'workspace': data})
    assert created.status_code == 200
    assert created.json()['revision'] == 1
    assert harness.client.get(path, headers=first).json() == {'revision': 1, 'workspace': data}
    assert harness.client.get(path, headers=other).json() == {'revision': 0, 'workspace': None}
    conflict = harness.client.put(path, headers=first, json={'revision': 0, 'workspace': {'classes': [], 'tests': [], 'links': {}}})
    assert conflict.status_code == 409
    assert harness.client.get(path, headers=first).json()['workspace'] == data
    assert harness.client.put(path, headers=first, json={'revision': 1, 'workspace': {'classes': [], 'tests': [], 'links': {}}}).json()['revision'] == 2


def test_workspace_rejects_invalid_shape(harness):
    auth, _ = harness.register()
    response = harness.client.put('/v1/workspace', headers=auth, json={'revision': 0, 'workspace': {'classes': 'invalid'}})
    assert response.status_code == 422


import pytest


@pytest.mark.parametrize('use_postgres', [False, True], ids=['sqlite', 'postgres'])
def test_two_devices_cannot_silently_overwrite_each_other(build_harness, request, use_postgres):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    db_url = request.getfixturevalue('postgres_url') if use_postgres else None
    harness = build_harness(db_url=db_url)
    auth, _ = harness.register()
    path = '/v1/workspace'
    empty = {'classes': [], 'tests': [], 'links': {}}
    assert harness.client.put(path, headers=auth, json={'revision': 0, 'workspace': empty}).status_code == 200
    barrier = Barrier(2, timeout=10)
    def edit(name):
        data = {'classes': [{'id': name, 'name': name}], 'tests': [], 'links': {}}
        barrier.wait()
        return harness.client.put(path, headers=auth, json={'revision': 1, 'workspace': data})
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(edit, ['first', 'second']))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert harness.client.get(path, headers=auth).json()['revision'] == 2
