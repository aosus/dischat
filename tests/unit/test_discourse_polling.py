from dischat.discourse.polling import normalize_post_event


def test_normalize_post_event_marks_new_topic() -> None:
    event = normalize_post_event(
        {
            "id": 10,
            "topic_id": 20,
            "category_id": 30,
            "username": "alice",
        }
    )

    assert event.event_type == "new_topic"


def test_normalize_post_event_marks_direct_reply() -> None:
    event = normalize_post_event(
        {
            "id": 10,
            "topic_id": 20,
            "category_id": 30,
            "username": "alice",
            "reply_to_post_number": 2,
            "reply_to_user": {"username": "bob"},
        }
    )

    assert event.event_type == "direct_reply"
    assert event.target_discourse_username == "bob"


def test_normalize_post_event_marks_mention() -> None:
    event = normalize_post_event(
        {
            "id": 10,
            "topic_id": 20,
            "category_id": 30,
            "username": "alice",
            "mentioned_users": [{"username": "bob"}],
        }
    )

    assert event.event_type == "mention"
    assert event.target_discourse_username == "bob"
