from datetime import timedelta

import app.billing as billing
from tests.support import photograph


def create_checkout(harness, auth, plan):
    response = harness.client.post('/v1/subscription/checkout', headers=auth, json={'plan_code': plan})
    assert response.status_code == 201, response.text
    return response.json()


def confirm(harness, auth, checkout):
    response = harness.client.post(checkout['checkout_url'] + '/confirm', headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def test_free_quota_counts_pages_and_is_not_refreshed(build_harness):
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    status = harness.subscription(auth)
    assert (status['free_remaining'], status['checks_remaining']) == (30, 30)
    submission = harness.queued_submission(auth, photograph(), photograph(), photograph())
    assert harness.subscription(auth)['free_remaining'] == 27
    harness.process(submission['job_id'])
    assert harness.subscription(auth)['free_remaining'] == 27
    other, _ = harness.register()
    assert harness.subscription(other)['free_remaining'] == 30


def test_multipage_pdf_charges_per_page(build_harness):
    from io import BytesIO
    from PIL import Image
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    with BytesIO() as output:
        Image.new('RGB', (40, 40), 'white').save(output, format='PDF', save_all=True,
                                                    append_images=[Image.new('RGB', (40, 40), 'blue')])
        response = harness.upload(auth, ('files', ('test.pdf', output.getvalue(), 'application/pdf')))
    assert response.status_code == 202, response.text
    assert response.json()['page_count'] == 2
    assert harness.subscription(auth)['free_remaining'] == 28


def test_prices_topup_idempotence_and_no_expiry(build_harness, monkeypatch):
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    plans = {p['code']: p for p in harness.client.get('/v1/plans').json()}
    assert (plans['monthly1000']['price_minor'], plans['monthly1000']['currency'], plans['monthly1000']['checks_limit']) == (1990, 'BYN', 1000)
    assert (plans['topup140']['price_minor'], plans['topup140']['currency'], plans['topup140']['checks_limit']) == (390, 'BYN', 140)
    denied = harness.client.post('/v1/subscription/checkout', headers=auth, json={'plan_code': 'topup140'})
    assert denied.status_code == 403
    confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))
    checkout = create_checkout(harness, auth, 'topup140')
    assert confirm(harness, auth, checkout)['applied'] is True
    assert confirm(harness, auth, checkout)['duplicate'] is True
    assert harness.subscription(auth)['extra_remaining'] == 140
    monkeypatch.setattr(billing, 'utcnow', lambda: billing.as_utc(billing.datetime(2030, 1, 1)))
    assert harness.subscription(auth)['extra_remaining'] == 140


def test_monthly_first_then_free_and_topup_survive_expiry(build_harness, monkeypatch):
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))
    confirm(harness, auth, create_checkout(harness, auth, 'topup140'))
    status = harness.subscription(auth)
    assert (status['monthly_remaining'], status['free_remaining'], status['extra_remaining']) == (1000, 30, 140)
    submission = harness.queued_submission(auth, photograph(), photograph())
    harness.process(submission['job_id'])
    status = harness.subscription(auth)
    assert (status['monthly_remaining'], status['free_remaining'], status['extra_remaining']) == (998, 30, 140)
    future = billing.as_utc(billing.datetime.fromisoformat(status['period_end'])) + timedelta(seconds=1)
    monkeypatch.setattr(billing, 'utcnow', lambda: future)
    status = harness.subscription(auth)
    assert (status['monthly_remaining'], status['free_remaining'], status['extra_remaining']) == (0, 30, 140)


def test_failed_check_releases_sheet_reservation(build_harness):
    from app.ai import AIError, FakeAIProvider
    class Failing(FakeAIProvider):
        def recognize(self, pages, clarification):
            raise AIError('ai_unavailable')
    harness = build_harness(ai_provider=Failing(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    submission = harness.queued_submission(auth, photograph(), photograph())
    assert harness.subscription(auth)['free_remaining'] == 28
    harness.process(submission['job_id'])
    assert harness.submission(auth, submission['id'])['status'] == 'failed'
    assert harness.subscription(auth)['free_remaining'] == 30


def test_free_exhaustion_and_nonexpiring_topup_are_counted_atomically(build_harness):
    from app.core import ApiError
    import pytest
    harness = build_harness(sheet_billing_enabled=True)
    auth, credentials = harness.register()
    confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))
    confirm(harness, auth, create_checkout(harness, auth, 'topup140'))
    from app.accounts import User
    from sqlalchemy import select
    with harness.app.state.session_factory.begin() as db:
        user = db.scalar(select(User).where(User.email == credentials['email']))
        monthly = billing.reserve_sheets(db, user.id, 1000)
        billing.settle_check(db, monthly, True)
        first = billing.reserve_sheets(db, user.id, 30)
        billing.settle_check(db, first, True)
        second = billing.reserve_sheets(db, user.id, 2)
        billing.settle_check(db, second, True)
    status = harness.subscription(auth)
    assert (status['free_remaining'], status['extra_remaining']) == (0, 138)
    with pytest.raises(ApiError) as error:
        with harness.app.state.session_factory.begin() as db:
            user = db.scalar(select(User).where(User.email == credentials['email']))
            billing.reserve_sheets(db, user.id, 139)
    assert error.value.code == 'quota_exhausted'
    assert harness.subscription(auth)['extra_remaining'] == 138


def test_topup_rejected_when_monthly_allowance_exhausted(build_harness):
    from app.accounts import User
    from sqlalchemy import select
    harness = build_harness(sheet_billing_enabled=True)
    auth, credentials = harness.register()
    confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))
    with harness.app.state.session_factory.begin() as db:
        user = db.scalar(select(User).where(User.email == credentials['email']))
        billing.settle_check(db, billing.reserve_sheets(db, user.id, 1000), True)
    assert harness.subscription(auth)['monthly_remaining'] == 0
    response = harness.client.post('/v1/subscription/checkout', headers=auth, json={'plan_code': 'topup140'})
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'topup_unavailable'


def test_topup_rejected_after_subscription_expiration(build_harness, monkeypatch):
    harness = build_harness(sheet_billing_enabled=True)
    auth, _ = harness.register()
    confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))
    end = billing.datetime.fromisoformat(harness.subscription(auth)['period_end'])
    monkeypatch.setattr(billing, 'utcnow', lambda: billing.as_utc(end) + timedelta(seconds=1))
    response = harness.client.post('/v1/subscription/checkout', headers=auth, json={'plan_code': 'topup140'})
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'topup_unavailable'


def test_topup_cannot_be_confirmed_after_remaining_monthly_sheets_are_used(build_harness):
    from app.accounts import User
    from sqlalchemy import select
    harness = build_harness(sheet_billing_enabled=True)
    auth, credentials = harness.register()
    confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))
    checkout = create_checkout(harness, auth, 'topup140')
    with harness.app.state.session_factory.begin() as db:
        user = db.scalar(select(User).where(User.email == credentials['email']))
        billing.settle_check(db, billing.reserve_sheets(db, user.id, 1000), True)
    result = harness.client.post(checkout['checkout_url'] + '/confirm', headers=auth)
    assert result.status_code == 409
    assert harness.subscription(auth)['extra_remaining'] == 0


def test_more_than_ten_pages_are_allowed(build_harness):
    harness = build_harness(sheet_billing_enabled=True, max_pages=100)
    auth, _ = harness.register()
    response = harness.upload(auth, *[photograph() for _ in range(11)])
    assert response.status_code == 202, response.text
    assert response.json()['page_count'] == 11
    assert harness.subscription(auth)['free_remaining'] == 19


def test_upgrade_from_existing_demo_subscription(build_harness):
    harness = build_harness(sheet_billing_enabled=False)
    auth, _ = harness.register()
    harness.subscribe(auth)
    harness.app.state.settings = harness.app.state.settings.model_copy(update={'sheet_billing_enabled': True})
    assert harness.subscription(auth)['checks_remaining'] == 30
    assert confirm(harness, auth, create_checkout(harness, auth, 'monthly1000'))['applied']
    assert harness.subscription(auth)['monthly_remaining'] == 1000


def test_parallel_uploads_cannot_overspend_last_free_sheet(build_harness):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.accounts import User
    from sqlalchemy import select
    harness = build_harness(sheet_billing_enabled=True)
    auth, credentials = harness.register()
    with harness.app.state.session_factory.begin() as db:
        user = db.scalar(select(User).where(User.email == credentials['email']))
        billing.settle_check(db, billing.reserve_sheets(db, user.id, 29), True)
    barrier = Barrier(2, timeout=10)
    def upload(_):
        barrier.wait()
        return harness.upload(auth)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(upload, range(2)))
    assert sorted(response.status_code for response in responses) == [202, 429]
    assert harness.subscription(auth)['free_remaining'] == 0


def test_unbilled_added_page_is_charged_after_failed_clarification(build_harness):
    from app.ai import AIError, FakeAIProvider
    class FailsSecondRecognition(FakeAIProvider):
        calls = 0
        def recognize(self, pages, clarification):
            self.calls += 1
            if self.calls == 2:
                raise AIError('ai_unavailable')
            return super().recognize(pages, clarification)
    harness = build_harness(ai_provider=FailsSecondRecognition(), sheet_billing_enabled=True)
    auth, _ = harness.register()
    initial = harness.queued_submission(auth)
    harness.process(initial['job_id'])
    assert harness.subscription(auth)['free_remaining'] == 29
    path = f"/v1/submissions/{initial['id']}"
    added = harness.client.post(path + '/clarifications', headers=auth, files=[photograph()])
    assert added.status_code == 202
    harness.process(added.json()['job_id'])
    assert harness.subscription(auth)['free_remaining'] == 29
    task_id = harness.results(auth, initial['id'])['tasks'][0]['id']
    recheck = harness.client.post(f'{path}/tasks/{task_id}/recheck', headers=auth)
    assert recheck.status_code == 202
    harness.process(recheck.json()['job_id'])
    assert harness.subscription(auth)['free_remaining'] == 29
    retry = harness.client.post(path + '/clarifications', headers=auth, data={'text': 'Проверить лист'})
    assert retry.status_code == 202
    harness.process(retry.json()['job_id'])
    assert harness.subscription(auth)['free_remaining'] == 28
