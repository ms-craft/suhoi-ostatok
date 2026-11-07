from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

HEIC = (Path(__file__).parent / 'fixtures' / 'red.heic').read_bytes()


@pytest.mark.parametrize('content_type', ['image/heic', 'image/heif', 'application/octet-stream'])
def test_heic_upload_keeps_original_and_serves_jpeg(harness, content_type):
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.upload(auth, ('files', ('photo.heic', HEIC, content_type)))
    assert response.status_code == 202, response.text
    submission = response.json()
    path = f"/v1/submissions/{submission['id']}"
    original = harness.client.get(f'{path}/files/1', headers=auth)
    assert original.content == HEIC
    assert original.headers['content-type'] == 'image/heic'
    page = harness.client.get(f'{path}/pages/1', headers=auth)
    assert page.headers['content-type'] == 'image/jpeg'
    with Image.open(BytesIO(page.content)) as image:
        assert image.format == 'JPEG'
        assert image.size == (96, 72)
    harness.process(submission['job_id'])
    assert harness.submission(auth, submission['id'])['status'] == 'completed'


@pytest.mark.parametrize(('data', 'mime', 'status'), [
    (b'\x00\x00\x00\x18ftypheicbroken', 'image/heic', 422),
    (HEIC, 'image/png', 415),
])
def test_invalid_heic_rejected_without_charging(harness, data, mime, status):
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.upload(auth, ('files', ('photo.heic', data, mime)))
    assert response.status_code == status
    assert not harness.app.state.storage.objects
    assert harness.subscription(auth)['checks_remaining'] == 1


def test_heic_pixel_limit_is_enforced(build_harness):
    harness = build_harness(max_image_pixels=1000)
    auth, _ = harness.register()
    harness.subscribe(auth)
    response = harness.upload(auth, ('files', ('photo.heic', HEIC, 'image/heic')))
    assert response.status_code == 413
    assert not harness.app.state.storage.objects


def test_heic_preview_requires_auth_and_does_not_create_work(harness):
    auth, _ = harness.register()
    files = {'file': ('photo.heic', HEIC, 'image/heic')}
    assert harness.client.post('/v1/submissions/preview', files=files).status_code == 401
    response = harness.client.post('/v1/submissions/preview', headers=auth, files=files)
    assert response.status_code == 200
    assert response.headers['content-type'] == 'image/jpeg'
    assert response.headers['cache-control'] == 'private, no-store'
    with Image.open(BytesIO(response.content)) as image:
        assert image.format == 'JPEG'
        assert image.width <= 480 and image.height <= 480
    assert harness.app.state.storage.objects == {}
    assert harness.client.get('/v1/submissions', headers=auth).json()['items'] == []


def test_heic_preview_enforces_limits_and_validates_content(build_harness):
    harness = build_harness(max_image_pixels=1000)
    auth, _ = harness.register()
    response = harness.client.post('/v1/submissions/preview', headers=auth, files={'file': ('photo.heic', HEIC, 'image/heic')})
    assert response.status_code == 413
    assert response.json()['error']['code'] == 'image_pixel_limit'
    response = harness.client.post('/v1/submissions/preview', headers=auth,
                                   files={'file': ('broken.heic', b'\x00\x00\x00\x18ftypheicbroken', 'image/heic')})
    assert response.status_code == 422
    assert harness.app.state.storage.objects == {}


def test_image_over_20_megapixels_is_resized():
    from app.config import Settings
    from app.files import render_image

    source = BytesIO()
    Image.new('RGB', (4600, 4600), 'white').save(source, format='PNG')
    settings = Settings(_env_file=None)
    assert settings.max_image_pixels == 64_000_000
    result = render_image(source.getvalue(), 'PNG', settings)
    with Image.open(BytesIO(result)) as image:
        assert image.format == 'JPEG'
        assert image.size == (2000, 2000)
