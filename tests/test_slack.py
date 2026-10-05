from app.sources.slack import format_text, mpim_label, thread_ts_from_permalink, unreplied

ME = "UME"


def m(ts, user="UOTHER", **kw):
    return {"ts": ts, "user": user, "text": f"msg {ts}", **kw}


def test_unreplied_after_my_last_message():
    msgs = [m("1.0"), m("2.0", ME), m("3.0"), m("4.0")]
    assert [x["ts"] for x in unreplied(msgs, ME)] == ["3.0", "4.0"]


def test_nothing_pending_when_i_replied_last():
    assert unreplied([m("1.0"), m("2.0", ME)], ME) == []


def test_since_limits_to_messages_from_the_mention_on():
    msgs = [m("1.0"), m("2.0"), m("3.0")]
    assert [x["ts"] for x in unreplied(msgs, ME, since=2.0)] == ["2.0", "3.0"]


def test_bots_and_joins_ignored_by_default():
    msgs = [m("1.0", bot_id="B1"), m("2.0", subtype="channel_join"), m("3.0")]
    assert [x["ts"] for x in unreplied(msgs, ME)] == ["3.0"]
    assert len(unreplied(msgs, ME, include_bots=True)) == 2


def test_order_independent():
    msgs = [m("4.0"), m("3.0", ME), m("5.0")]  # history API returns newest first
    assert [x["ts"] for x in unreplied(msgs, ME)] == ["4.0", "5.0"]


def test_permalink_thread_ts():
    url = "https://x.slack.com/archives/C1/p1700000000123456?thread_ts=1700000000.000100&cid=C1"
    assert thread_ts_from_permalink(url) == "1700000000.000100"
    assert thread_ts_from_permalink("https://x.slack.com/archives/C1/p1700000000123456") is None


def test_format_text():
    names = {"U1": "alice"}.get
    out = format_text("hey <@U1> see <https://a.io|docs> in <#C1|general> &amp; <!here>", names)
    assert out == "hey @alice see docs in #general & @here"


def test_mpim_label():
    assert mpim_label("mpdm-alice--bob--carol-1") == "alice, bob, carol"
