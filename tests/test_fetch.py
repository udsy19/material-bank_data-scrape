from material_bank.fetch import Fetcher


class FakeResponse:
    def __init__(self, status=200, text="ok", headers=None, url=None):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.url = url


class FakeSession:
    """Returns queued responses; records every get() call + kwargs."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        resp = self._responses.pop(0)
        if resp.url is None:
            resp.url = url
        return resp


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, d):
        self.sleeps.append(d)
        self.t += d  # sleeping advances time


def make_fetcher(session, clock, **kw):
    return Fetcher(session=session, clock=clock.now, sleep=clock.sleep,
                   raw_dir=None, **kw)


def test_impersonate_chrome131_passed():
    clock = Clock()
    sess = FakeSession([FakeResponse()])
    make_fetcher(sess, clock).get("https://a.com/x")
    assert sess.calls[0][1]["impersonate"] == "chrome131"


def test_per_domain_2s_spacing():
    clock = Clock()
    sess = FakeSession([FakeResponse(), FakeResponse()])
    f = make_fetcher(sess, clock, min_interval=2.0)
    f.get("https://a.com/one")   # first: no wait
    f.get("https://a.com/two")   # second, no time elapsed: must wait ~2s
    assert 2.0 in clock.sleeps


def test_different_domains_not_throttled():
    clock = Clock()
    sess = FakeSession([FakeResponse(), FakeResponse()])
    f = make_fetcher(sess, clock, min_interval=2.0)
    f.get("https://a.com/x")
    f.get("https://b.com/y")     # different host: no forced wait
    assert clock.sleeps == []


def test_backoff_on_429_then_success():
    clock = Clock()
    sess = FakeSession([FakeResponse(status=429), FakeResponse(status=200, text="done")])
    f = make_fetcher(sess, clock, min_interval=0, backoff_base=1.0, max_retries=3)
    res = f.get("https://a.com/x")
    assert res.status_code == 200 and res.text == "done"
    assert 1.0 in clock.sleeps          # 2**0 backoff between attempts
    assert len(sess.calls) == 2


def test_retry_after_header_honored():
    clock = Clock()
    sess = FakeSession([
        FakeResponse(status=503, headers={"Retry-After": "5"}),
        FakeResponse(status=200),
    ])
    f = make_fetcher(sess, clock, min_interval=0, max_retries=2)
    f.get("https://a.com/x")
    assert 5.0 in clock.sleeps


def test_redirect_final_host_captured():
    clock = Clock()
    sess = FakeSession([FakeResponse(url="https://roca.in/in/en/")])
    f = make_fetcher(sess, clock)
    res = f.get("https://roca.in/")
    assert res.final_host == "roca.in"
    assert res.redirected_host is None   # same host, no cross-host redirect

    clock2 = Clock()
    sess2 = FakeSession([FakeResponse(url="https://in.roca.com/")])
    res2 = make_fetcher(sess2, clock2).get("https://roca.in/")
    assert res2.redirected_host == "in.roca.com"


def test_network_error_returns_error_result():
    class BoomSession:
        def get(self, *a, **k):
            raise ConnectionError("dns fail")

    f = Fetcher(session=BoomSession(), raw_dir=None)
    res = f.get("https://nope.invalid/")
    assert res.error is not None and not res.ok
    assert res.status_code is None
